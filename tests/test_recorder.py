"""MEC-57: recorder tests - 100% offline (api monkeypatched, tmp data dir).

Covers:
  [T1] write_snapshot append + dedupe on (ts_utc, symbol)
  [T2] roll_daily OHLC + volume=MAX (not sum) + idempotent re-run
  [T3] missing pyarrow -> SystemExit with the [recorder] extra hint
  [T4] universe() filters ACTIVE + market-tradable only

No socket may open: arcus_mcp.api.assets/prices are monkeypatched, and
tests/conftest.py has already pointed ARCUS_GATEWAY_DATA at a tmp dir.

scripts/ is not a package, so the module is loaded by path
(spec_from_file_location).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "recorder", REPO / "scripts" / "recorder.py")
assert _spec is not None and _spec.loader is not None
recorder = importlib.util.module_from_spec(_spec)
sys.modules["recorder"] = recorder  # so helpers resolve if re-imported
_spec.loader.exec_module(recorder)

from arcus_mcp import api  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_data_dir(tmp_path, monkeypatch):
    """Per-test history dir. conftest.py points ARCUS_GATEWAY_DATA at
    ONE session-wide tmp dir; parquet files would otherwise leak
    between tests (append + glob across months). paths.data_dir()
    re-reads the env on every call, so setenv is enough."""
    monkeypatch.setenv("ARCUS_GATEWAY_DATA", str(tmp_path))


def _asset(sym, *, status="ASSET_STATUS_ACTIVE", tradable=True, mult="1"):
    whole = "TRADING_STATUS_TRADABLE" if tradable else "TRADING_STATUS_UNTRADABLE"
    return {
        "tokenSymbol": sym,
        "tokenName": f"{sym} Inc",
        "status": status,
        "currentMultiplier": mult,
        "pendingMultiplier": "",
        "tradingCapabilities": {
            "market": {"whole": whole, "fractional": whole},
            "extended": {"whole": whole, "fractional": whole},
            "overnight": {"whole": whole, "fractional": whole},
        },
    }


def _quote(bid="10.00", ask="12.00", vol="100", halted=False) -> dict:
    return {
        "tokenSymbol": "X",
        "bid": bid,
        "ask": ask,
        "currency": "USD",
        "dailyTradingVolume": vol,
        "isTradingHalt": bool(halted),
        "generatedAt": "2026-09-03T19:43:54Z",
    }


@pytest.fixture
def patched_api(monkeypatch):
    """Point api.assets/prices at canned data; returns the stores so a
    test can tweak them before running a tick."""
    assets = [_asset("AAA"), _asset("BBB")]
    quotes = {"AAA": _quote(), "BBB": _quote(bid="20.00", ask="22.00")}
    monkeypatch.setattr(api, "assets", lambda: assets)
    monkeypatch.setattr(api, "prices", lambda s: quotes.get(s))
    return {"assets": assets, "quotes": quotes}


def _read(path):
    import pyarrow.parquet as pq
    return pq.read_table(path).to_pylist()


def _daily_path():
    from arcus_mcp import paths
    return paths.data_dir() / "history" / "daily.parquet"


# ------------------------------------------------------------------- [T1]


def test_t1_write_snapshot_append_and_dedupe(patched_api):
    ts = "2026-09-04T12:00:00Z"
    assets = patched_api["assets"]
    rows = [r for a in assets
            if (r := recorder.snap_row(ts, a["tokenSymbol"],
                                       patched_api["quotes"][a["tokenSymbol"]],
                                       recorder._to_f(a["currentMultiplier"])))
            is not None]
    assert len(rows) == 2
    n = recorder.write_snapshot(rows, ts)
    assert n == 2
    # same tick written again (e.g. crashed tick re-run): no duplicates
    n2 = recorder.write_snapshot(rows, ts)
    assert n2 == 2
    snap = paths_snap = recorder.snapshot_file(ts)
    assert snap.exists()
    got = _read(snap)
    assert len(got) == 2  # dedupe on (ts_utc, symbol): still one row each
    assert {(r["symbol"], r["bid_raw"], r["mid_adjusted"]) for r in got} == {
        ("AAA", 10.0, 11.0), ("BBB", 20.0, 21.0)}
    # a NEW tick appends: month file now holds 4 rows
    ts2 = "2026-09-04T12:05:00Z"
    rows2 = [dict(r, ts_utc=ts2) for r in rows]
    assert recorder.write_snapshot(rows2, ts2) == 4
    assert len(_read(snap)) == 4


# ------------------------------------------------------------------- [T2]


def test_t2_roll_daily_ohlc_max_volume_idempotent(patched_api):
    day = "2026-09-03"
    # 3 ticks, growing cumulative daily_volume, mid_adjusted 10 -> 12 -> 11
    ticks = [
        ("2026-09-03T14:00:00Z", {"bid": "10.00", "ask": "10.00", "vol": "100"}),
        ("2026-09-03T15:00:00Z", {"bid": "12.00", "ask": "12.00", "vol": "150"}),
        ("2026-09-03T16:00:00Z", {"bid": "11.00", "ask": "11.00", "vol": "200"}),
    ]
    rows = []
    for ts, q in ticks:
        r = recorder.snap_row(ts, "AAA", _quote(**q), 1.0)
        assert r is not None
        rows.append(r)
    assert recorder.write_snapshot(rows, ticks[-1][0]) == 3
    written = recorder.roll_daily(day)
    assert written == 1
    got = _read(_daily_path())
    assert len(got) == 1
    row = got[0]
    assert row["date"] == day and row["symbol"] == "AAA"
    assert row["open"] == 10.0 and row["close"] == 11.0
    assert row["high"] == 12.0 and row["low"] == 10.0
    assert row["volume"] == 200  # MAX of cumulative volumes, NOT the sum 450
    # idempotent: re-running the same day must not double rows
    assert recorder.roll_daily(day) == 1
    got2 = _read(_daily_path())
    assert len(got2) == 1
    assert got2[0] == row
    # snapshots untouched by the rollup
    assert len(_read(recorder.snapshot_file(ticks[-1][0]))) == 3


# ------------------------------------------------------------------- [T3]


def test_t3_missing_pyarrow_exits_with_extra_hint(monkeypatch):
    real_import = __import__

    def no_pyarrow(name, *args, **kwargs):
        if name == "pyarrow" or name.startswith("pyarrow."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", no_pyarrow)
    with pytest.raises(SystemExit) as exc:
        recorder._pq()
    assert "arcus-agent-gateway[recorder]" in str(exc.value)


# ------------------------------------------------------------------- [T4]


def test_t4_universe_filters_active_tradable(monkeypatch):
    assets = [
        _asset("GOOD"),                                   # ACTIVE + tradable
        _asset("UNTRADABLE", tradable=False),             # ACTIVE, not tradable
        _asset("GONE", status="ASSET_STATUS_INACTIVE"),   # not ACTIVE
    ]
    monkeypatch.setattr(api, "assets", lambda: assets)
    out = recorder.universe()
    assert [a["tokenSymbol"] for a in out] == ["GOOD"]


def test_t4_universe_limit_and_injected_assets(monkeypatch):
    assets = [_asset(s) for s in ("CCC", "AAA", "BBB")]
    monkeypatch.setattr(api, "assets", lambda: assets)
    assert [a["tokenSymbol"] for a in recorder.universe()] == \
        ["AAA", "BBB", "CCC"]  # alphabetical
    assert [a["tokenSymbol"] for a in recorder.universe(limit=2)] == \
        ["AAA", "BBB"]
    # assets= injection (collect_rows path) skips api.assets entirely
    assert [a["tokenSymbol"] for a in recorder.universe(assets=assets[:1])] == \
        ["CCC"]


# ------------------------------------------------------------- integration


def test_tick_once_end_to_end(patched_api, capsys, monkeypatch):
    """--once path: rollup of yesterday is attempted (no data: silent),
    one tick timestamp, all rows land in the month file."""
    monkeypatch.setattr(recorder, "_now_iso",
                        lambda: "2026-09-04T12:00:00Z")
    recorder.tick()
    snap = recorder.snapshot_file("2026-09-04T12:00:00Z")
    got = _read(snap)
    assert {r["symbol"] for r in got} == {"AAA", "BBB"}
    assert {r["ts_utc"] for r in got} == {"2026-09-04T12:00:00Z"}
    out = capsys.readouterr().out
    assert "universe=2" in out and "written=2" in out


def test_tick_skips_symbol_without_quote(patched_api, capsys, monkeypatch):
    patched_api["quotes"]["BBB"] = None  # prices() -> None: no row
    monkeypatch.setattr(recorder, "_now_iso",
                        lambda: "2026-09-04T12:00:00Z")
    recorder.tick()
    got = _read(recorder.snapshot_file("2026-09-04T12:00:00Z"))
    assert [r["symbol"] for r in got] == ["AAA"]
    assert "skipped=1 (no quote)" in capsys.readouterr().out


def test_tick_survives_symbol_network_error(patched_api, capsys, monkeypatch):
    def boom(sym):
        if sym == "AAA":
            raise RuntimeError("rhj unreachable after 3 tries: x - y")
        return patched_api["quotes"][sym]
    monkeypatch.setattr(api, "prices", boom)
    monkeypatch.setattr(recorder, "_now_iso",
                        lambda: "2026-09-04T12:00:00Z")
    recorder.tick()  # must not raise: one dead symbol != dead tick
    got = _read(recorder.snapshot_file("2026-09-04T12:00:00Z"))
    assert [r["symbol"] for r in got] == ["BBB"]
    out = capsys.readouterr().out
    assert "ERROR AAA" in out and "errors=1" in out


def test_mid_adjusted_uses_multiplier(monkeypatch):
    monkeypatch.setattr(api, "assets", lambda: [])
    r = recorder.snap_row("2026-09-04T12:00:00Z", "SPL",
                          _quote(bid="9.00", ask="11.00"), 2.0)
    assert r["mid_adjusted"] == 20.0  # (9+11)/2 * 2.0, 6 dp
    # mid with a missing side or multiplier stays None, never a crash
    assert recorder.snap_row("t", "X", _quote(bid="", ask="11.00"),
                             2.0)["mid_adjusted"] is None
    assert recorder.snap_row("t", "X", _quote(), None)["mid_adjusted"] is None
    assert recorder.snap_row("t", "X", None, 1.0) is None
