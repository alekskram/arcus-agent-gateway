"""v0.2.1: optional price-history recorder (standalone, no fastmcp).

Snapshot loop over the tradable universe into monthly-rotated parquet
files, plus an idempotent daily rollup:

    python -m arcus_mcp.recorder               # loop, $ARCUS_INTERVAL_SEC (default 300s)
    python -m arcus_mcp.recorder --once        # single tick, then exit (systemd timer)
    python -m arcus_mcp.recorder --limit 5     # debug: only the first 5 symbols

Requires pyarrow - install the optional extra:
    pip install 'arcus-agent-gateway[recorder]'

Shipped inside the wheel since 0.2.1 (MEC-69): the unit's
`python -m scripts.recorder` failed on pip installs because scripts/
is not packaged. scripts/recorder.py stays behind as a thin shim for
git-checkout users.

Design notes:
  * Reads ONLY arcus_mcp.api (cached get/prices/assets) and
    arcus_mcp.paths - never arcus_mcp.server, so this process never
    pulls in fastmcp.
  * api's caches do the heavy lifting: assets() TTL 300s covers one
    tick at the default 300s interval (universe + multipliers are read
    once per tick); prices() TTL 15s never bites inside a tick because
    each symbol is requested exactly once per tick.
  * Parquet writes are atomic (tmp file + os.replace). Appends dedupe
    on (ts_utc, symbol) with last-write-wins, so a crashed tick can be
    re-run safely.
  * Logging is print(flush=True) - systemd Journal picks it up.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arcus_mcp import api, paths

TRADABLE = "TRADING_STATUS_TRADABLE"
ACTIVE = "ASSET_STATUS_ACTIVE"
DEFAULT_INTERVAL_SEC = 300.0  # env ARCUS_INTERVAL_SEC overrides; --interval wins


# ---------------------------------------------------------------- helpers


def _to_f(s) -> float | None:
    """float(s) for rhj numeric strings ("327.77", "" -> None); None
    otherwise. Local twin of server._f - duplicated (not imported) so
    the recorder never loads arcus_mcp.server and its fastmcp.
    """
    if s is None:
        return None
    if isinstance(s, str) and not s.strip():
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _tradable_whole_and_fractional(a: dict) -> bool:
    """market whole AND fractional both tradable. Local copy of
    server._tradable_whole_and_fractional (kept in sync by hand) - not
    imported to avoid pulling fastmcp into the recorder process.
    """
    tc = a.get("tradingCapabilities") or {}
    mkt = tc.get("market") or {}
    return mkt.get("whole") == TRADABLE and mkt.get("fractional") == TRADABLE


def _now_iso() -> str:
    """UTC now as '2026-09-04T12:00:00Z' (fixed-width, sorts as text)."""
    return (datetime.now(timezone.utc).isoformat(timespec="seconds")
            .replace("+00:00", "Z"))


def universe(limit: int | None = None, assets: list | None = None) -> list[dict]:
    """ACTIVE + market-tradable (whole AND fractional) assets, sorted
    by symbol. assets=None -> api.assets() (300s cache: fetched once
    per tick by the caller; the cache handles refresh across ticks).
    limit truncates to the first N symbols (debug).
    """
    src = api.assets() if assets is None else assets
    out = [a for a in src
           if a.get("status") == ACTIVE and _tradable_whole_and_fractional(a)]
    out.sort(key=lambda a: a.get("tokenSymbol") or "")
    if limit is not None:
        out = out[:max(0, int(limit))]
    return out


def snap_row(ts: str, symbol: str, quote: dict | None,
             multiplier: float | None) -> dict | None:
    """One snapshot row from a /prices quote; None when there is no
    quote (caller counts that symbol as skipped). mid_adjusted =
    (bid+ask)/2 * multiplier, 6 dp; None-propagating like server._adj.
    """
    if quote is None:
        return None
    bid = _to_f(quote.get("bid"))
    ask = _to_f(quote.get("ask"))
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    if mid is not None and multiplier is not None:
        mid = round(mid * multiplier, 6)
    else:
        mid = None
    return {
        "ts_utc": ts,
        "symbol": symbol,
        "bid_raw": bid,
        "ask_raw": ask,
        "mid_adjusted": mid,
        "daily_volume": _to_f(quote.get("dailyTradingVolume")),
        "multiplier": multiplier,
        "is_halted": bool(quote.get("isTradingHalt")),
    }


def collect_rows(ts: str, assets: list[dict]) -> tuple[list[dict], int, int]:
    """Rows for one tick: one api.prices(sym) per symbol (15s cache is
    a no-op inside a tick - one request per symbol). Multipliers come
    from the SAME assets() payload the caller already holds - identical
    to api.asset(sym)['currentMultiplier'] but without a second lookup.
    Returns (rows, skipped_no_quote, errors). One broken symbol never
    kills the tick: network/API errors are logged and counted.
    """
    rows: list[dict] = []
    skipped = 0
    errors = 0
    for a in assets:
        sym = a.get("tokenSymbol") or ""
        mult = _to_f(a.get("currentMultiplier"))
        try:
            quote = api.prices(sym)
        except (ValueError, RuntimeError) as e:
            errors += 1
            print(f"[tick {ts}] ERROR {sym}: {e}", flush=True)
            continue
        row = snap_row(ts, sym, quote, mult)
        if row is None:
            skipped += 1  # no live quote right now: count, don't write
            continue
        rows.append(row)
    return rows, skipped, errors


# ---------------------------------------------------------------- parquet


def _pq():
    """(pyarrow, pyarrow.parquet); exits with the extra's install hint
    when pyarrow is missing. Imported lazily so importing this module
    (e.g. in tests) never requires pyarrow.
    """
    try:
        import pyarrow
        import pyarrow.parquet
    except ImportError:
        sys.exit("pyarrow is not installed - the recorder needs the "
                 "optional extra: pip install 'arcus-agent-gateway[recorder]'")
    return pyarrow, pyarrow.parquet


def _snapshot_schema(pa):
    return pa.schema([
        ("ts_utc", pa.string()),
        ("symbol", pa.string()),
        ("bid_raw", pa.float64()),
        ("ask_raw", pa.float64()),
        ("mid_adjusted", pa.float64()),
        ("daily_volume", pa.float64()),
        ("multiplier", pa.float64()),
        ("is_halted", pa.bool_()),
    ])


def _daily_schema(pa):
    return pa.schema([
        ("date", pa.string()),
        ("symbol", pa.string()),
        ("open", pa.float64()),
        ("close", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("volume", pa.float64()),
        ("multiplier", pa.float64()),
    ])


def _history_dir() -> Path:
    d = paths.data_dir() / "history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write(table, path: Path) -> None:
    """Write to a tmp sibling, then os.replace - readers never see a
    half-written parquet file."""
    _, pq = _pq()
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp)
    os.replace(tmp, path)


def snapshot_file(ts: str) -> Path:
    """snapshots_YYYYMM.parquet for a tick timestamp (monthly rotation;
    the month comes from the tick's UTC time)."""
    ym = ts[:7].replace("-", "")  # '2026-09-04T...' -> '202609'
    return _history_dir() / f"snapshots_{ym}.parquet"


def write_snapshot(rows: list[dict], ts: str) -> int:
    """Append this tick's rows to the month file chosen by `ts`,
    deduping on (ts_utc, symbol) - last write wins - then atomically
    replace the file. Returns total rows in the file after the write.
    Re-running the same tick is a no-op update, never a duplicate.
    """
    pa, pq = _pq()
    path = snapshot_file(ts)
    if not rows and not path.exists():
        return 0
    merged: dict[tuple[str, str], dict] = {}
    if path.exists():
        for r in pq.read_table(path).to_pylist():
            merged[(r["ts_utc"], r["symbol"])] = r
    for r in rows:  # applied after the old rows: new values win clashes
        merged[(r["ts_utc"], r["symbol"])] = r
    out = [merged[k] for k in sorted(merged)]
    _atomic_write(pa.Table.from_pylist(out, schema=_snapshot_schema(pa)), path)
    return len(out)


def roll_daily(day: str) -> int:
    """Roll one COMPLETED UTC day ('YYYY-MM-DD') from the snapshot
    files into history/daily.parquet and return rows written.

    OHLC over mid_adjusted (open = first by ts, close = last, high/low
    = max/min). volume = MAX(daily_volume), NOT a sum: the API's
    dailyTradingVolume is the cumulative day volume AT SNAPSHOT TIME,
    so the day's highest observed value IS the day total - summing
    snapshots would multiply-count the same shares.

    Idempotent: the day is rewritten whole (existing rows for `day` are
    dropped, then re-added), so a re-run after a crash - or a second
    tick in the same day - cannot double rows. Only call this for
    completed days (yesterday and older); today keeps growing.
    """
    pa, pq = _pq()
    per_symbol: dict[str, list[dict]] = {}
    for f in sorted(_history_dir().glob("snapshots_*.parquet")):
        for r in pq.read_table(f).to_pylist():
            if str(r.get("ts_utc", "")).startswith(day):
                per_symbol.setdefault(r["symbol"], []).append(r)
    new_rows = []
    for sym in sorted(per_symbol):
        snaps = sorted(per_symbol[sym], key=lambda r: r["ts_utc"])
        mids = [r["mid_adjusted"] for r in snaps
                if r["mid_adjusted"] is not None]
        vols = [r["daily_volume"] for r in snaps
                if r["daily_volume"] is not None]
        mults = [r["multiplier"] for r in snaps
                 if r["multiplier"] is not None]
        new_rows.append({
            "date": day,
            "symbol": sym,
            "open": mids[0] if mids else None,
            "close": mids[-1] if mids else None,
            "high": max(mids) if mids else None,
            "low": min(mids) if mids else None,
            "volume": max(vols) if vols else None,
            "multiplier": mults[-1] if mults else None,
        })
    if not new_rows:
        return 0
    daily = _history_dir() / "daily.parquet"
    keep: list[dict] = []
    if daily.exists():
        keep = [r for r in pq.read_table(daily).to_pylist()
                if r.get("date") != day]
    _atomic_write(pa.Table.from_pylist(keep + new_rows,
                                       schema=_daily_schema(pa)), daily)
    return len(new_rows)


# ------------------------------------------------------------------- loop


def tick(limit: int | None = None) -> None:
    """One pass: roll up yesterday (completed day), then snapshot the
    whole universe with a single tick timestamp."""
    ts = _now_iso()
    yesterday = (datetime.now(timezone.utc)
                 - timedelta(days=1)).date().isoformat()
    try:
        n = roll_daily(yesterday)
        if n:
            print(f"[tick {ts}] rollup {yesterday}: {n} symbol-day "
                  f"rows -> daily.parquet", flush=True)
    except Exception as e:  # a broken rollup must not kill recording
        print(f"[tick {ts}] rollup {yesterday} failed: {e}", flush=True)
    assets = universe(limit=limit)
    rows, skipped, errors = collect_rows(ts, assets)
    total = write_snapshot(rows, ts) if rows else 0
    msg = (f"[tick {ts}] universe={len(assets)} written={len(rows)} "
           f"skipped={skipped} (no quote) file={snapshot_file(ts).name} "
           f"rows_total={total}")
    if errors:
        msg += f" errors={errors}"
    print(msg, flush=True)


def main(argv: list[str] | None = None) -> None:
    """Entry point. --once for systemd timers, otherwise loop with
    --interval / $ARCUS_INTERVAL_SEC (default 300s) sleep between ticks.
    """
    ap = argparse.ArgumentParser(
        prog="arcus-recorder",
        description="Arcus price-history recorder (v0.2.1): snapshots "
        "the tradable universe to monthly parquet + daily rollup. "
        "Needs the optional extra: pip install 'arcus-agent-gateway[recorder]'")
    ap.add_argument("--once", action="store_true",
                    help="run a single tick and exit (systemd timer mode)")
    ap.add_argument("--interval", type=float, default=None, metavar="N",
                    help="seconds between ticks (default: "
                         "$ARCUS_INTERVAL_SEC or 300)")
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="debug: record only the first N symbols")
    args = ap.parse_args(argv)
    _pq()  # fail fast with the install hint, before any network I/O
    interval = (args.interval if args.interval is not None else float(
        os.environ.get("ARCUS_INTERVAL_SEC", DEFAULT_INTERVAL_SEC)))
    print(f"[recorder] start interval={interval:g}s once={args.once} "
          f"limit={args.limit if args.limit is not None else 'all'}",
          flush=True)
    while True:
        tick(limit=args.limit)
        if args.once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
