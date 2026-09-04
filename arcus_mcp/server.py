"""Arcus Agent Gateway - MCP server (Robinhood Chain / rhj tokenized equities).

Read-only, keyless market-data tools per the design spec: token_list, quote(s),
token_detail, market_status, corporate_actions, search, sector_view,
onchain_info; price_history (v0.1.1) reading the OPTIONAL local
recorder's parquet files (honest degradation, no pyarrow -> no crash);
and the v0.2 on-chain trio holder_snapshot / wallet_holdings /
transfer_history reading the public RPC (rpc.py) and the Blockscout
explorer (explorer.py) with per-field source tags and honest
degradation. Sectors come from the static validated map in sectors.py.

Style follows dydx_mcp/server.py: plain functions registered via
build_server() -> FastMCP, all annotated read-only. All upstream access
goes through the `api` / `rpc` / `explorer` module namespaces
(api.assets() etc.) so tests can monkeypatch them - never
`from .api import x`.
"""
from __future__ import annotations

import asyncio
import re
import threading
import time

from . import api
from . import explorer
from . import paths
from . import rpc
from . import sectors

TRADABLE = "TRADING_STATUS_TRADABLE"
DEFAULT_PORT = 8902

_QUOTES_CONCURRENCY = 8  # v0.1.1: in-flight quote() cap (see _quotes_async)
_QUOTES_BATCH = 20       # max symbols per quotes() call (spec invariant)

# Tool-level result caches (monotonic ts -> payload). The explorer client
# caches its own pages (600s); these cover the joined/derived payloads.
_HOLDINGS_TTL = 120.0    # wallet_holdings join of balances x universe
_TRANSFERS_TTL = 60.0    # transfer_history normalized rows
# Walk-back floor: latest - 800 blocks. The free RPC can only serve a
# floating ~45-60-block window anyway; this cap just keeps the walker's
# "budget" mode from pretending it might reach genesis.
_TRANSFERS_LOOKBACK = 800
_HOLDINGS_CACHE: dict[str, tuple[float, dict]] = {}
_TRANSFERS_CACHE: dict[str, tuple[float, dict]] = {}
_TOOL_CACHE_LOCK = threading.Lock()

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TOTAL_SUPPLY_DATA = "0x18160ddd"  # keccak("totalSupply()") selector


# ------------------------------------------------------------------ helpers


def _f(s) -> float | None:
    """float(s) for numeric strings ("327.77", "" -> None); None otherwise.

    Every rhj numeric is a STRING; pendingMultiplier is "" when nothing
    is queued (API_NOTES.md #1-2). Never raises.
    """
    if s is None:
        return None
    if isinstance(s, str) and not s.strip():
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _adj(x: float | None, m: float | None) -> float | None:
    """Multiplier-adjusted price, 6 dp. None-propagating. Adjusted prices
    are ALWAYS returned next to the multiplier block, never instead of
    the raw ones."""
    if x is None or m is None:
        return None
    return round(x * m, 6)


def _caps(a: dict) -> dict:
    """tradingCapabilities -> {fractional, all_day, extended_hours}.

    fractional      = market.fractional tradable (fractional shares in
                      regular hours)
    all_day         = overnight whole AND fractional tradable (24h)
    extended_hours  = extended.fractional tradable
    """
    tc = a.get("tradingCapabilities") or {}

    def ok(bucket: str, kind: str) -> bool:
        return (tc.get(bucket) or {}).get(kind) == TRADABLE

    return {
        "fractional": ok("market", "fractional"),
        "all_day": ok("overnight", "whole") and ok("overnight", "fractional"),
        "extended_hours": ok("extended", "fractional"),
    }


def _tradable_whole_and_fractional(a: dict) -> bool:
    """token_list 'tradable': market whole AND fractional both tradable."""
    tc = a.get("tradingCapabilities") or {}
    mkt = tc.get("market") or {}
    return (mkt.get("whole") == TRADABLE and mkt.get("fractional") == TRADABLE)


def _ts(v):
    """Normalize a timestamp value: ISO strings pass through; protobuf
    Timestamp objects ({year, month, day, ...} dicts, seen LIVE on
    /corporate-actions effectiveTime 2026-09-03) become ISO dates."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict) and isinstance(v.get("year"), int):
        return (f"{v['year']:04d}-{v.get('month', 0):02d}"
                f"-{v.get('day', 0):02d}")
    return None


def _pending_effective_time(actions: list[dict]) -> str | None:
    """First effectiveTime/processDate found in a symbol's actions."""
    for act in actions or []:
        for key in ("effectiveTime", "processDate", "process_date",
                    "effective_time"):
            v = act.get(key)
            if v:
                return _ts(v)
    return None


def _multiplier_block(a: dict, effective_time: str | None = None) -> dict:
    """{current, pending, effective_time}: current/pending as floats,
    pending None while pendingMultiplier is "" (nothing queued).
    effective_time is filled by callers that already hold the symbol's
    corporate actions (token_detail / quote); None otherwise."""
    current = _f(a.get("currentMultiplier"))
    pending = _f(a.get("pendingMultiplier"))  # "" -> None, never parsed blind
    return {
        "current": current if current is not None else 1.0,
        "pending": pending,
        "effective_time": effective_time,
    }


def _require_symbol(symbol: str) -> dict:
    """Guard: resolve symbol to its asset row or raise an actionable
    ValueError pointing the agent at token_list() (+ 5 examples)."""
    a = api.asset(symbol)
    if a is None:
        examples = ", ".join(
            sorted(x.get("tokenSymbol", "") for x in api.assets()
                   if x.get("status") == "ASSET_STATUS_ACTIVE")[:5])
        raise ValueError(
            f"unknown symbol {symbol!r}. Call token_list() for the full "
            f"set; examples: {examples}")
    return a


def _cached_quotes() -> dict[str, dict]:
    """Snapshot of api's price cache (opportunistic - no requests)."""
    with api._CACHE_LOCK:
        return {sym: entry[1]
                for sym, entry in getattr(api, "_PRICES_CACHE", {}).items()}


def _count_cold_prices(symbols) -> int:
    """How many of `symbols` lack a fresh (TTL-valid) cached quote.

    Mirrors api.prices()' own staleness check (monotonic timestamp +
    api._PRICES_TTL) under api._CACHE_LOCK. This is the honest upper
    bound on network requests a warm pass will make: every cache miss
    means one api.prices() call, every hit means none (negative answers
    never enter the cache, so they stay misses). No requests are made
    here - pure cache inspection.
    """
    now = time.monotonic()
    ttl = getattr(api, "_PRICES_TTL", 0.0)
    syms = list(symbols)
    with api._CACHE_LOCK:
        cache = getattr(api, "_PRICES_CACHE", {})
        hits = 0
        for s in syms:
            entry = cache.get((s or "").strip().upper())
            if entry and now - entry[0] <= ttl:
                hits += 1
    return len(syms) - hits


# ------------------------------------------------------------------- tools


def token_list(status: str = "ACTIVE", limit: int = 100) -> dict:
    """Tokenized equities on Robinhood Chain, one row per token:
    symbol, name, status, multiplier (float) and tradable (market whole
    AND fractional tradable). status filters on the ASSET_STATUS_*
    prefix - pass 'ACTIVE' (default, hides inactive) or another status
    short name; 'ALL' disables filtering. Alphabetical, capped at limit.
    Returns {"count", "total_matching", "tokens"}. Start here for valid
    symbols; feed them into quote/token_detail.
    Example: token_list(status="ACTIVE", limit=50)"""
    want = (status or "").strip().upper()
    prefix = None if want in ("", "ALL") else (
        want if want.startswith("ASSET_STATUS_") else f"ASSET_STATUS_{want}")
    rows = []
    for a in api.assets():
        st = a.get("status", "")
        if prefix and not st.startswith(prefix):
            continue
        rows.append({
            "symbol": a.get("tokenSymbol"),
            "name": a.get("tokenName"),
            "status": st,
            "multiplier": _f(a.get("currentMultiplier")),
            "tradable": _tradable_whole_and_fractional(a),
        })
    rows.sort(key=lambda r: r["symbol"] or "")
    n = max(0, min(int(limit), 500))
    return {"count": len(rows[:n]), "total_matching": len(rows),
            "tokens": rows[:n]}


def quote(symbol: str) -> dict:
    """One token's live quote joined with its asset metadata:
    bid/ask/spread raw, multiplier-adjusted (bid_adjusted, ask_adjusted,
    mid_adjusted - always read them next to multiplier), daily_volume,
    is_halted, trading_capabilities {fractional, all_day,
    extended_hours}, multiplier {current, pending (None while nothing
    is queued), effective_time} and generated_at. Unknown symbol raises
    an error (MCP isError) - call token_list() for the valid set. A
    token with no current quote returns metadata with bid/ask None and
    a note. Example: quote(symbol="AAPL")"""
    a = _require_symbol(symbol)
    sym = a.get("tokenSymbol") or ""
    p = api.prices(sym)
    m = _multiplier_block(a)["current"]
    eff = None
    if _f(a.get("pendingMultiplier")) is not None:  # only spend a
        # corporate-actions call when a multiplier change is queued
        try:
            eff = _pending_effective_time(
                api.corporate_actions(sym, limit=5))
        except (ValueError, RuntimeError):
            eff = None
    mult = _multiplier_block(a, effective_time=eff)
    out = {
        "symbol": sym,
        "name": a.get("tokenName"),
        "bid_raw": None, "ask_raw": None, "spread_raw": None,
        "bid_adjusted": None, "ask_adjusted": None, "mid_adjusted": None,
        "daily_volume": None,
        "is_halted": None,
        "trading_capabilities": _caps(a),
        "multiplier": mult,
        "generated_at": None,
    }
    if p is None:
        out["note"] = ("no live quote right now (prices returned nothing) - "
                       "metadata only")
        return out
    bid, ask = _f(p.get("bid")), _f(p.get("ask"))
    spread = round(ask - bid, 6) if bid is not None and ask is not None \
        else None
    mid = (bid + ask) / 2 if (bid is not None and ask is not None) else None
    out.update({
        "bid_raw": bid, "ask_raw": ask, "spread_raw": spread,
        "bid_adjusted": _adj(bid, m), "ask_adjusted": _adj(ask, m),
        "mid_adjusted": _adj(mid, m),
        "daily_volume": _f(p.get("dailyTradingVolume")),
        "is_halted": bool(p.get("isTradingHalt")),
        "generated_at": p.get("generatedAt"),
    })
    return out


def _quotes_serial(syms: list[str]) -> dict:
    """Order-preserving serial pass (fallback + reference semantics)."""
    out, errors = [], []
    for s in syms:
        try:
            out.append(quote(s))
        except ValueError as e:  # unknown symbol: collect, don't fail batch
            errors.append({"symbol": s, "reason": str(e)})
    return {"quotes": out, "errors": errors}


async def _quotes_async(syms: list[str]) -> dict:
    """Parallel fan-out of quote() over `syms`, order-preserving.

    Runs inside asyncio.run() from a plain worker thread (FastMCP runs
    sync tools in a threadpool, so no loop is running here). Each
    quote() is blocking, so it goes to the default ThreadPoolExecutor
    via run_in_executor, capped at _QUOTES_CONCURRENCY (8) in flight by
    a semaphore. gather(..., return_exceptions=True) keeps results
    aligned with the input order: per-symbol ValueErrors (unknown
    symbols) land in errors[], anything else re-raises and fails the
    batch - exactly the serial semantics.
    """
    sem = asyncio.Semaphore(_QUOTES_CONCURRENCY)
    loop = asyncio.get_running_loop()

    async def _fetch(s: str):
        async with sem:
            return await loop.run_in_executor(None, quote, s)

    results = await asyncio.gather(
        *(asyncio.create_task(_fetch(s)) for s in syms),
        return_exceptions=True)
    out, errors = [], []
    for s, res in zip(syms, results):
        if isinstance(res, BaseException):
            if isinstance(res, ValueError):  # unknown symbol: collect,
                errors.append({"symbol": s, "reason": str(res)})  # go on
            else:
                raise res  # same rule as the serial pass: fail the batch
        else:
            out.append(res)
    return {"quotes": out, "errors": errors}


def quotes(symbols: list[str]) -> dict:
    """Batch of quote() rows, up to 20 per call (more raises), fetched
    in PARALLEL (v0.1.1): 8 quote() calls in flight at once, so 10
    cold symbols complete in roughly one RTT instead of ten. Unknown
    symbols do not fail the batch - they land in
    errors: [{symbol, reason}] while the rest return normally, and the
    output order matches the input order.
    Example: quotes(symbols=["AAPL", "MSFT", "NVDA"])"""
    syms = [s for s in (symbols or []) if s and s.strip()]
    if len(syms) > _QUOTES_BATCH:
        raise ValueError(
            f"quotes: {len(syms)} symbols > {_QUOTES_BATCH} per call - "
            "split the batch")
    coro = _quotes_async(syms)
    try:
        # FastMCP runs sync tools in a threadpool: no loop in this
        # thread, so asyncio.run is the way to fan out.
        return asyncio.run(coro)
    except RuntimeError as e:
        if "running event loop" not in str(e):
            raise  # a genuine upstream RuntimeError - let it through
        coro.close()  # never started; silence "never awaited"
        # Only if some host ever calls this sync tool on a loop thread:
        # degrade to the serial pass rather than crash.
        return _quotes_serial(syms)


def token_detail(symbol: str) -> dict:
    """Full dossier for one token: metadata (contract, chain 4663,
    decimals, isin, logo), its quote() view, the last 5 corporate
    actions, the multiplier block with history_note, trading
    capabilities (normalized + raw market/extended/overnight statuses)
    and warnings - a pending multiplier change surfaces as
    'pending split: 1->X on DATE'. Unknown symbol raises an error
    (MCP isError). Example: token_detail(symbol="AAPL")"""
    a = _require_symbol(symbol)
    sym = a.get("tokenSymbol") or ""
    acts = api.corporate_actions(sym, limit=5)
    eff = _pending_effective_time(acts)
    mult = _multiplier_block(a, effective_time=eff)
    mult["history_note"] = (
        "multiplier is the corporate-action adjustment factor for the "
        "token contract (1.0 = untouched); a pending value applies at "
        "effective_time per the token's corporate actions")
    warnings: list[str] = []
    if mult["pending"] is not None and mult["pending"] != mult["current"]:
        ratio = (round(mult["pending"] / mult["current"], 6)
                 if mult["current"] else None)
        when = eff or "unknown date"
        warnings.append(f"pending split: 1\u2192{ratio} on {when}")
    dep = (a.get("deployments") or [{}])[0]
    return {
        "metadata": {
            "symbol": sym,
            "name": a.get("tokenName"),
            "contract": dep.get("contractAddress"),
            "chain_id": dep.get("chainId"),
            "network": dep.get("networkName"),
            "decimals": a.get("tokenDecimals"),
            "isin": a.get("isin"),
            "logo": a.get("logoUrl"),
        },
        "quote": quote(sym),
        "corporate_actions": [_action_row(x) for x in acts],
        "multiplier": mult,
        "trading_capabilities": {
            **_caps(a),
            "raw": a.get("tradingCapabilities"),
        },
        "warnings": warnings,
    }


def market_status() -> dict:
    """Market-wide health from assets() only - never fetches 194 prices.
    total_tokens, active (ASSET_STATUS_ACTIVE), untradable (fractional
    untradable among active), halted (only tokens whose quote is ALREADY
    in the price cache - opportunistic; call quotes() on suspect symbols
    for a real halt scan), extended_hours_open and market_hours_status.
    Example: market_status()"""
    assets = api.assets()
    active = [a for a in assets if a.get("status") == "ASSET_STATUS_ACTIVE"]
    untradable = sum(1 for a in active if not _caps(a)["fractional"])
    ext_open = any(_caps(a)["extended_hours"] for a in active)
    cached = _cached_quotes()
    halted = sorted(sym for sym, q in cached.items()
                    if q.get("isTradingHalt"))
    return {
        "total_tokens": len(assets),
        "active": len(active),
        "untradable": untradable,
        "halted": halted,
        "extended_hours_open": ext_open,
        "market_hours_status": "estimated from tradingCapabilities "
                               "(no clock API)",
        "note": "halted is opportunistic (only cached quotes are scanned, "
                "no requests made); for a full halt scan call quotes() on "
                "the symbols you care about (max 20 per call)",
    }


def _action_row(act: dict) -> dict:
    """Normalize one corporate action; tolerant to the field-name variants
    seen in the wild (type|actionType|kind, processDate|effectiveTime,
    *From/*To/splitFrom/splitTo for the rates). Cash dividends carry
    details.cashDividend.rate (per-share amount, STRING) - surfaced as
    details.rate; the API exposes no total cash amount (API_NOTES #5).
    `raw` keeps the original untouched."""
    typ = next((act[k] for k in ("type", "actionType", "kind")
                if act.get(k)), None)
    when = next((act[k] for k in ("processDate", "effectiveTime",
                                  "process_date", "effective_time")
                 if act.get(k)), None)
    old_rate = next((act[k] for k in sorted(act)
                     if k.endswith("From") and _f(act[k]) is not None), None)
    new_rate = next((act[k] for k in sorted(act)
                     if k.endswith("To") and _f(act[k]) is not None), None)
    cash = ((act.get("details") or {}).get("cashDividend") or {})
    rate = cash.get("rate")
    return {
        "symbol": act.get("tokenSymbol"),
        "type": typ,
        "status": act.get("status"),
        "process_date": _ts(when),
        "details": {"old_rate": _f(old_rate), "new_rate": _f(new_rate),
                    "rate": _f(rate)},
        "raw": act,
    }


def corporate_actions(symbol: str | None = None, limit: int = 10) -> dict:
    """Corporate actions (splits, dividends) across all tokens, or
    filtered to one symbol. Each row: symbol, type, status, process_date,
    details {old_rate, new_rate} (floats when the source carries rate
    fields) and raw (the original record, untouched). Tolerant to the
    API's field-name variants. Example:
    corporate_actions(symbol="AAPL", limit=5)"""
    rows = [_action_row(x) for x in api.corporate_actions(symbol, limit)]
    return {"actions": rows, "count": len(rows)}


def search(query: str, limit: int = 10) -> dict:
    """Local fuzzy search over the token list - no HTTP beyond the cached
    assets(). Ranking: exact symbol > symbol prefix > name-word prefix >
    substring in name/symbol (case-insensitive); top `limit` (default
    10, cap 50) with score.
    'apple' -> AAPL. Results carry symbol, name, status, sector, score.
    Example: search(query="apple", limit=3)"""
    q = (query or "").strip().lower()
    if not q:
        return {"query": query, "results": [], "count": 0}
    scored = []
    for a in api.assets():
        sym = a.get("tokenSymbol") or ""
        name = a.get("tokenName") or ""
        sym_l, name_l = sym.lower(), name.lower()
        if sym_l == q:
            score = 100
        elif sym_l.startswith(q):
            score = 80
        elif any(w.startswith(q) for w in name_l.split()):
            score = 60
        elif q in name_l or q in sym_l:
            score = 40
        else:
            continue
        scored.append((score, len(sym), sym, {
            "symbol": sym,
            "name": name,
            "status": a.get("status"),
            "sector": sectors.sector_of(sym),
            "score": score,
        }))
    n = max(0, min(int(limit), 50))
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    top = [row for _, _, _, row in scored[:n]]
    return {"query": query, "results": top, "count": len(top)}


def _sector_rows(names: list[str]) -> dict[str, dict]:
    """Per-sector size + cached-quote averages for the given sector
    names (no requests - pure cache/assets join)."""
    cached = _cached_quotes()
    out: dict[str, dict] = {}
    for name in names:
        syms = sectors.SECTORS.get(name, [])
        bids, asks, halted = [], [], []
        for s in syms:
            q = cached.get(s)
            if q is None:
                continue
            if q.get("isTradingHalt"):
                halted.append(s)
            a = api.asset(s)
            m = _multiplier_block(a)["current"] if a else None
            b, k = _f(q.get("bid")), _f(q.get("ask"))
            if m and b is not None:
                bids.append(_adj(b, m))
            if m and k is not None:
                asks.append(_adj(k, m))
        out[name] = {
            "symbols": len(syms),
            "quoted": len(set(syms) & cached.keys()),
            "avg_bid_adjusted": round(sum(bids) / len(bids), 6)
            if bids else None,
            "avg_ask_adjusted": round(sum(asks) / len(asks), 6)
            if asks else None,
            "halted": halted,
        }
    return out


def sector_view(warm: bool = False, sector: str | None = None) -> dict:
    """Sector map (13 sectors, static validated classification) with
    per-sector size and live averages.

    warm=False (default): cheap snapshot - averages appear only for
    quotes ALREADY cached ('warmed': false; no requests made).
    warm=True: fan out quotes() first (parallel batches of 20, upstream
    rate-limit safe), then report - averages are live. With sector=
    'Name' only that sector is fetched and returned; an unknown name
    raises with the valid list. requests_made counts the price-cache
    misses at the start of the warm pass (each miss = one upstream
    request; fresh cache hits and cached-again symbols cost nothing).
    Example: sector_view(warm=True, sector="Crypto/Digital Assets")"""
    if warm:
        if sector is not None:
            syms = sectors.sector_symbols(sector)
            if not syms:
                valid = ", ".join(sectors.SECTORS)
                raise ValueError(
                    f"unknown sector {sector!r}. Valid sectors: {valid}")
            names = [next(n for n in sectors.SECTORS
                          if n.lower() == sector.strip().lower())]
            targets = syms
        else:
            names = list(sectors.SECTORS)
            targets = [s for syms in sectors.SECTORS.values() for s in syms]
        requests_made = _count_cold_prices(targets)
        for i in range(0, len(targets), _QUOTES_BATCH):
            quotes(targets[i:i + _QUOTES_BATCH])
        out = _sector_rows(names)
        return {
            "sectors": out,
            "warmed": True,
            "requests_made": requests_made,
            "note": "warm pass fetched quotes in parallel batches of "
                    f"{_QUOTES_BATCH} (requests_made = price-cache misses "
                    "at start); averages are multiplier-adjusted",
        }
    out = _sector_rows(list(sectors.SECTORS))
    return {
        "sectors": out,
        "warmed": False,
        "note": "call quotes([...sector symbols...]) first to populate "
                "live averages (max 20 per call), or sector_view("
                "warm=True) to fetch them now; averages are "
                "multiplier-adjusted",
    }


# ------------------------------------------------------------- on-chain v0.2


def _err_dict(kind: str, message: str, hint: str | None = None) -> dict:
    """Flat error row for warnings[]/error fields (kind + hint, never silent)."""
    return {"kind": kind, "message": message, "hint": hint or ""}


def _contract_of(a: dict) -> str | None:
    """First deployment contract address, or None."""
    dep = (a.get("deployments") or [{}])[0]
    return dep.get("contractAddress")


def _rpc_total_supply(contract: str) -> float | None:
    """totalSupply() via eth_call /1e18; None on any failure (caller warns)."""
    try:
        raw = rpc.call(contract, _TOTAL_SUPPLY_DATA)
        return int(raw, 16) / 10**18
    except (rpc.RpcError, ValueError, TypeError):
        return None


def _explorer_token_row(contract: str, warnings: list) -> dict | None:
    """explorer.token() row or None + independent-failure warning."""
    try:
        return explorer.token(contract)
    except explorer.ExplorerError as e:
        warnings.append(_err_dict(
            "explorer", f"holders/mcap unavailable: {e}",
            hint=e.hint or "explorer is the only source for these fields"))
        return None


def onchain_info(symbol: str) -> dict:
    """On-chain footprint of a token, joined from three independent
    sources (each fails to a warning, never silently):

      * REST metadata: contract, chain_id, network, decimals, isin
      * RPC (publicnode eth_call): total_supply = totalSupply()/1e18
      * explorer (Blockscout): holders_count, circulating_market_cap

    supply_crosscheck compares the REST-implied capitalization
    (total_supply * multiplier * quote mid) with the explorer's
    circulating_market_cap; a >1% divergence lands in warnings[] (both
    values still returned). Every derived field carries a per-field
    "source" tag ("rpc" | "explorer" | "rest"). Example:
    onchain_info(symbol="AAPL")"""
    a = _require_symbol(symbol)
    dep = (a.get("deployments") or [{}])[0]
    contract = dep.get("contractAddress")
    chain_id = dep.get("chainId")
    if isinstance(chain_id, str):  # numeric-string drift -> float, else raw
        chain_id = _f(chain_id)
    warnings: list = []
    out: dict = {
        "symbol": a.get("tokenSymbol"),
        "contract": contract,
        "chain_id": chain_id,
        "network": dep.get("networkName"),
        "decimals": a.get("tokenDecimals"),
        "isin": a.get("isin"),
    }

    # ---- rpc: total_supply (source "rpc")
    total_supply: float | None = None
    if contract:
        total_supply = _rpc_total_supply(contract)
        if total_supply is None:
            warnings.append(_err_dict(
                "rpc", "total_supply unavailable: eth_call totalSupply() "
                "failed on the public RPC",
                hint="retry, or read the explorer-supplied supply"))
        if total_supply is not None:
            out["total_supply"] = round(total_supply, 6)
            out["total_supply_source"] = "rpc"

    # ---- explorer: holders_count + circulating_market_cap
    tok = _explorer_token_row(contract, warnings) if contract else None
    if tok is not None:
        holders = _f(tok.get("holders_count"))
        mcap = _f(tok.get("circulating_market_cap"))
        if holders is not None:
            out["holders_count"] = int(holders)
            out["holders_count_source"] = "explorer"
        else:
            warnings.append(_err_dict(
                "explorer", "holders_count missing in explorer token row"))
        if mcap is not None:
            out["circulating_market_cap"] = mcap
            out["circulating_market_cap_source"] = "explorer"
        else:
            warnings.append(_err_dict(
                "explorer", "circulating_market_cap missing in explorer "
                "token row"))
    elif not contract:
        warnings.append(_err_dict(
            "rest", "no deployment/contract address on the asset row",
            hint="on-chain fields need a contract; call token_detail()"))

    # ---- supply crosscheck: REST price x multiplier vs explorer mcap
    out["supply_crosscheck"] = _supply_crosscheck(
        a, total_supply, tok, warnings)
    out["warnings"] = warnings
    return out


def _supply_crosscheck(a: dict, total_supply: float | None,
                       tok: dict | None, warnings: list) -> dict:
    """Cross-check REST-implied capitalization vs the explorer's.

    rest_implied = total_supply * multiplier * quote.mid (rpc + REST);
    explorer_implied = circulating_market_cap, or total_supply *
    exchange_rate when the cap is missing. A >1% relative divergence
    lands in warnings[] (both values still returned for inspection).
    """
    row: dict = {"checked": False}
    if total_supply is None:
        row["note"] = "no rpc total_supply - nothing to cross-check"
        return row
    row["total_supply"] = round(total_supply, 6)
    mult = _multiplier_block(a)["current"]
    row["multiplier"] = mult
    sym = a.get("tokenSymbol") or ""
    mid = None
    try:
        p = api.prices(sym)
        if p is not None:
            bid, ask = _f(p.get("bid")), _f(p.get("ask"))
            if bid is not None and ask is not None:
                mid = (bid + ask) / 2
    except (ValueError, RuntimeError):
        mid = None
    if mid is None:
        row["note"] = "no REST quote - cross-check skipped (not an error)"
        return row
    row["mid_price"] = mid
    rest_implied = total_supply * mult * mid
    row["rest_implied_usd"] = round(rest_implied, 2)
    expl_implied = None
    if tok is not None:
        cap = _f(tok.get("circulating_market_cap"))
        rate = _f(tok.get("exchange_rate"))
        expl_implied = cap if cap is not None else (
            total_supply * rate if rate is not None else None)
        if expl_implied is not None:
            row["explorer_implied_usd"] = round(expl_implied, 2)
    if expl_implied is None:
        row["note"] = ("explorer cap unavailable - one-sided check only "
                       "(rest_implied_usd present)")
        return row
    if expl_implied <= 0:
        row["note"] = "explorer implied value non-positive - check skipped"
        return row
    divergence = abs(rest_implied - expl_implied) / expl_implied
    row["divergence_pct"] = round(divergence * 100, 4)
    row["checked"] = True
    if divergence > 0.01:
        warnings.append(
            f"supply cross-check divergence {divergence * 100:.2f}% "
            f"(REST-implied ${rest_implied:,.0f} vs explorer-implied "
            f"${expl_implied:,.0f}) - >1%: rest price, multiplier or "
            f"explorer cap may be stale")
    return row


def holder_snapshot(symbol: str, limit: int = 20) -> dict:
    """Top holders of a token's contract from the explorer (one page,
    max 50 rows, cached 600s inside the explorer client). Each row:
    address, value (float token units, raw/1e18), share_pct
    (= value / total_supply * 100) and is_contract. total_supply comes
    from the RPC totalSupply() call, falling back to the explorer's own
    token row when the RPC is unavailable (source-tagged either way).
    Explorer/RPC failures return an error dict with kind + hint, never
    a silent empty list. Example: holder_snapshot(symbol="AAPL", limit=10)
    """
    a = _require_symbol(symbol)
    contract = _contract_of(a)
    if not contract:
        return {"error": _err_dict(
            "rest", f"asset {a.get('tokenSymbol')!r} has no deployed "
            "contract address", hint="on-chain tools need a contract")}
    try:
        page = explorer.token_holders(contract, limit=limit)
    except explorer.ExplorerError as e:
        return {"error": _err_dict(e.kind, f"holders unavailable: {e}",
                                   hint=e.hint)}
    if page is None:
        return {"error": _err_dict(
            "explorer", "token unknown to the explorer (404)",
            hint="contract may not be indexed; try onchain_info() first")}
    warnings: list = []
    supply = _rpc_total_supply(contract)
    supply_source = "rpc"
    if supply is None:
        warnings.append(_err_dict(
            "rpc", "totalSupply() eth_call failed - falling back to the "
            "explorer supply"))
        try:
            tok = explorer.token(contract)
            raw_supply = _f(tok.get("total_supply")) if tok is not None \
                else None
            supply = (raw_supply / 10**18
                      if raw_supply is not None else None)
        except explorer.ExplorerError as e:
            warnings.append(_err_dict(
                "explorer", f"explorer supply fallback failed: {e}",
                hint=e.hint))
            supply = None
        supply_source = "explorer" if supply is not None else "none"
    n = max(1, min(50, int(limit)))
    rows: list = []
    for it in (page.get("items") or [])[:n]:
        raw = _f(it.get("value"))
        value = raw / 10**18 if raw is not None else None
        share = (round(value / supply * 100, 6)
                 if value is not None and supply else None)
        rows.append({
            "address": it.get("address"),
            "value": round(value, 6) if value is not None else None,
            "share_pct": share,
            "is_contract": bool(it.get("is_contract")),
        })
    out: dict = {
        "symbol": a.get("tokenSymbol"),
        "contract": contract,
        "count": len(rows),
        "holders": rows,
        "total_supply": round(supply, 6) if supply else None,
        "total_supply_source": supply_source,
    }
    if supply is None:
        warnings.append(_err_dict(
            "rpc", "no total_supply from either source - share_pct is "
            "null on every row",
            hint="retry later; explorer holders themselves are fresh"))
    if page.get("next_page_params") is not None:
        out["next_page"] = True
        out["note"] = ("page capped at 50 rows by design (no pagination "
                       "loops); more holders exist beyond this page")
    if warnings:
        out["warnings"] = warnings
    return out


def wallet_holdings(address: str) -> dict:
    """Which tokenized equities (out of the assets() universe) a wallet
    holds, from the explorer's address token-balances. The universe
    join is case-insensitive on the contract address; rows: symbol,
    name, value (float token units, raw/1e18). est_position_usd and
    portfolio_usd_total are computed ONLY from quotes already in the
    price cache (no quote fan-out); when quotes are missing or stale a
    note says so instead of faking numbers. Results cached 120s.
    Example: wallet_holdings(
        address="0x8366a39CC670B4001A1121B8F6A443A643e40951")"""
    addr = (address or "").strip()
    if not _ADDRESS_RE.match(addr):
        raise ValueError(
            f"not a wallet address: {address!r} - expected 0x + 40 hex "
            "chars")
    key = addr.lower()
    with _TOOL_CACHE_LOCK:
        cached = _HOLDINGS_CACHE.get(key)
        if cached and time.monotonic() - cached[0] <= _HOLDINGS_TTL:
            return cached[1]
    try:
        balances = explorer.address_token_balances(addr)
    except explorer.ExplorerError as e:
        return {"error": _err_dict(e.kind, f"balances unavailable: {e}",
                                   hint=e.hint)}
    if balances is None:
        return {"error": _err_dict(
            "explorer", "address unknown to the explorer (404)",
            hint="check the address on the explorer site")}
    universe = {
        ((a.get("deployments") or [{}])[0].get("contractAddress") or "")
        .lower(): a
        for a in api.assets()
        if (a.get("deployments") or [{}])[0].get("contractAddress")}
    cached_quotes = _cached_quotes()
    now = time.monotonic()
    ttl = getattr(api, "_PRICES_TTL", 0.0)
    with api._CACHE_LOCK:  # snapshot ts freshness per symbol
        entry_ts = {sym: e[0] for sym, e in
                    getattr(api, "_PRICES_CACHE", {}).items()}
    rows: list = []
    portfolio = 0.0
    priced = 0
    unpriced: list[str] = []
    for b in balances or []:
        tok = b.get("token") or {}
        asset = universe.get((tok.get("address_hash") or "").lower())
        if asset is None:
            continue  # not one of our tokenized equities
        raw = _f(b.get("value"))
        value = raw / 10**18 if raw is not None else None
        sym = asset.get("tokenSymbol")
        price = cached_quotes.get(sym)
        est = None
        if value is not None and price is not None:
            ts = entry_ts.get(sym)
            if ts is not None and now - ts <= ttl:
                bid, ask = _f(price.get("bid")), _f(price.get("ask"))
                m = _multiplier_block(asset)["current"]
                if bid is not None and ask is not None:
                    est = round(value * (bid + ask) / 2 * m, 2)
        if est is not None:
            portfolio += est
            priced += 1
        else:
            unpriced.append(sym)
        rows.append({
            "symbol": sym,
            "name": asset.get("tokenName"),
            "value": round(value, 6) if value is not None else None,
            "est_position_usd": est,
        })
    rows.sort(key=lambda r: -(r.get("est_position_usd")
                              or r.get("value") or 0))
    out: dict = {
        "address": addr,
        "count": len(rows),
        "holdings": rows,
        "portfolio_usd_total": round(portfolio, 2) if priced else None,
        "priced_positions": priced,
    }
    if unpriced:
        out["note"] = (f"est_position_usd omitted for {len(unpriced)} "
                       "position(s) with no FRESH cached quote (cache-only "
                       "pricing, no fan-out; call quotes() on those "
                       "symbols first)")
    with _TOOL_CACHE_LOCK:
        _HOLDINGS_CACHE[key] = (time.monotonic(), out)
    return out


def transfer_history(symbol: str, limit: int = 25,
                     min_value: float | None = None) -> dict:
    """Recent ERC-20 Transfer events for a token's contract, from the
    public RPC's adaptive walk-back over the last ~800 blocks (windows
    start 48 blocks wide and shrink 48->32->16->8 on archive-403s, ~14
    getLogs requests max - the free RPC only serves a floating
    ~45-60-block window). Rows (newest first): ts (ISO-8601 from the
    log's own blockTimestamp), from, to, value (float token units),
    tx_hash, block. min_value filters in token units AFTER
    normalization. Results cached 60s. The note is honest about why the
    walk stopped: 'window-closed' points at the explorer transfer list
    for older history, 'budget' says the request cap was hit.
    Example: transfer_history(symbol="AAPL", limit=10, min_value=1.0)
    """
    a = _require_symbol(symbol)
    contract = _contract_of(a)
    if not contract:
        return {"error": _err_dict(
            "rest", f"asset {a.get('tokenSymbol')!r} has no deployed "
            "contract address", hint="on-chain tools need a contract")}
    n = max(1, min(100, int(limit)))
    mv = None if min_value is None else float(min_value)
    ck = f"{contract.lower()}|{n}|{mv}"
    with _TOOL_CACHE_LOCK:
        cached = _TRANSFERS_CACHE.get(ck)
        if cached and time.monotonic() - cached[0] <= _TRANSFERS_TTL:
            return cached[1]
    try:
        latest = rpc.block_number()
        walked = rpc.get_transfers(
            contract, max(0, latest - _TRANSFERS_LOOKBACK), latest)
    except rpc.RpcError as e:
        return {"error": _err_dict(e.kind, f"transfers unavailable: {e}",
                                   hint="the free RPC window may have "
                                        "drifted; retry shortly")}
    logs = walked.get("logs") or []
    win = walked.get("window") or {}
    rows: list = []
    for log in logs:
        data = log.get("data") or ""
        try:
            units = int(data, 16) if data.startswith("0x") else int(data)
        except ValueError:
            units = None
        value = units / 10**18 if units is not None else None
        if mv is not None and (value is None or value < mv):
            continue
        topics = log.get("topics") or []
        frm = ("0x" + topics[1][-40:] if len(topics) > 1
               and len(topics[1]) >= 42 else None)
        to = ("0x" + topics[2][-40:] if len(topics) > 2
              and len(topics[2]) >= 42 else None)
        ts_raw = log.get("blockTimestamp")
        ts = None
        if isinstance(ts_raw, str) and ts_raw.startswith("0x"):
            try:
                ts = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime(int(ts_raw, 16)))
            except ValueError:
                ts = None
        elif isinstance(ts_raw, str) and ts_raw.isdigit():
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                               time.gmtime(int(ts_raw)))
        bn = log.get("blockNumber")
        rows.append({
            "ts": ts,
            "from": frm,
            "to": to,
            "value": round(value, 6) if value is not None else None,
            "tx_hash": log.get("transactionHash"),
            "block": (int(bn, 16) if isinstance(bn, str)
                      and bn.startswith("0x") else bn),
        })
    rows.sort(key=lambda r: ((r.get("block") or 0),
                             r.get("tx_hash") or ""), reverse=True)
    rows = rows[:n]
    out: dict = {
        "symbol": a.get("tokenSymbol"),
        "contract": contract,
        "count": len(rows),
        "transfers": rows,
        "window": {
            "requests": win.get("requests"),
            "stopped_reason": win.get("stopped_reason"),
            "oldest_covered": win.get("oldest_covered"),
            "newest_covered": win.get("newest_covered"),
        },
    }
    reason = win.get("stopped_reason")
    notes: list[str] = []
    if not logs:
        if reason == "complete":
            notes.append(f"no Transfer logs in the fully covered window "
                         f"({win.get('requests')} request(s))")
        elif reason == "budget":
            notes.append(
                "walk-back stopped at the ~14-request budget cap before "
                "finding any logs; retry shortly or use explorer "
                "/tokens/{contract}/transfers")
        else:
            notes.append("on-chain window reached; older transfers via "
                         "explorer /tokens/{contract}/transfers")
    else:
        if len(rows) < len(logs):
            notes.append(f"{len(logs) - len(rows)} transfer(s) below "
                         f"min_value={mv} filtered out")
        if reason == "budget":
            notes.append("history truncated: walk-back hit the "
                         "~14-request budget cap")
        elif reason == "window-closed":
            notes.append("older transfers beyond the RPC window via "
                         "explorer /tokens/{contract}/transfers")
    if notes:
        out["note"] = "; ".join(notes)
    with _TOOL_CACHE_LOCK:
        _TRANSFERS_CACHE[ck] = (time.monotonic(), out)
    return out


# --------------------------------------------------------- price_history

_DAILY_COLUMNS = ("symbol", "date", "open", "high", "low", "close", "volume")
_RAW_COLUMNS = ("ts_utc", "symbol", "bid_raw", "ask_raw", "mid_adjusted",
                "daily_volume", "multiplier", "is_halted")


def _history_rows(table, columns: tuple[str, ...]) -> list[dict]:
    """Parquet table -> list of plain dicts, one per row, pyarrow types
    coerced to JSON-safe Python (bool stays bool, numbers -> float)."""
    n = len(table)
    cols = {name: table.column(name).to_pylist() if name in table.column_names
            else [None] * n for name in columns}
    # fill any column missing from an older file with None
    return [{k: cols[k][i] for k in columns} for i in range(n)]


def price_history(symbol: str, timeframe: str = "daily",
                  limit: int = 90) -> dict:
    """Historical prices recorded by the OPTIONAL local recorder
    (scripts/recorder.py) - not a live API call. timeframe='daily':
    OHLCV bars from history/daily.parquet (open/high/low/close are
    multiplier-adjusted); timeframe='raw': every recorded snapshot
    (bid/ask raw, mid_adjusted, multiplier, is_halted) from
    history/snapshots_*.parquet. Rows are newest-first. The symbol is
    NOT checked against token_list (the recorder writes the whole
    universe; assets drift) - a symbol simply absent from the file
    returns count 0. Degrades honestly instead of raising when the
    optional pieces are missing: pyarrow absent -> error suggesting
    'pip install arcus-agent-gateway[recorder]'; recorder never run /
    no data yet -> error pointing at README § Optional price history
    recorder. limit must be positive (daily capped at 200, raw at 500).
    Example: price_history(symbol='AAPL', timeframe='daily', limit=30)"""
    sym = (symbol or "").strip().upper()
    if not sym:
        raise ValueError("symbol required")
    tf = (timeframe or "").strip().lower()
    if tf not in ("daily", "raw"):
        raise ValueError(
            f"unknown timeframe {timeframe!r}: use 'daily' or 'raw'")
    try:
        n = int(limit)
    except (TypeError, ValueError):
        raise ValueError(f"limit must be positive, got {limit!r}")
    if n <= 0:
        raise ValueError("limit must be positive")
    n = min(n, 200 if tf == "daily" else 500)
    out: dict = {"symbol": sym, "timeframe": tf, "count": 0, "rows": []}
    try:  # lazy: pyarrow ships only with the [recorder] extra
        import pyarrow.parquet as pq
    except ImportError:
        out["error"] = ("pyarrow not installed - install "
                        "arcus-agent-gateway[recorder] to enable price "
                        "history")
        out["hint"] = "README § Optional price history recorder"
        return out

    history = paths.data_dir() / "history"
    files = ([history / "daily.parquet"] if tf == "daily"
             else sorted(history.glob("snapshots_*.parquet")))
    tables = []
    for f in files:
        try:
            if f.exists():
                tables.append(pq.read_table(f))
        except Exception as exc:  # corrupt file: skip, don't crash
            out.setdefault("warnings", []).append(
                f"skipped unreadable {f.name}: {exc}")
    if not tables:
        out["error"] = ("recorder not enabled or no data yet - see README "
                        "§ Optional price history recorder")
        out["note"] = ("no snapshot files found; run the recorder "
                       "(scripts/recorder.py) to start collecting"
                       if tf == "raw" else
                       "need >= 1 day of snapshots for daily bars; "
                       "snapshots accumulate at the recorder interval "
                       "(default 300s)")
        return out
    columns = _DAILY_COLUMNS if tf == "daily" else _RAW_COLUMNS
    key = "date" if tf == "daily" else "ts_utc"
    rows: list[dict] = []
    for t in tables:
        rows.extend(_history_rows(t, columns))
    rows = [r for r in rows if r.get("symbol") == sym]
    rows.sort(key=lambda r: r.get(key) or "", reverse=True)
    out["rows"] = rows[:n]
    out["count"] = len(out["rows"])
    return out


# --------------------------------------------------------------- MCP wire


def build_server():
    """FastMCP instance with the 13 read-only tools wired (market-data
    spec tools + price_history + the v0.2 on-chain trio reading the
    public RPC and the block explorer)."""
    from fastmcp import FastMCP
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    mcp = FastMCP(
        "arcus-agent-gateway",
        version="0.2.0",
        instructions=(
            "Tokenized US equities on Robinhood Chain (Arcus DEX), "
            "read-only and keyless. Symbols come from token_list()/search() "
            "(e.g. 'AAPL'); quote() joins price + metadata with "
            "multiplier-adjusted fields (read adjusted values next to "
            "multiplier). token_detail() is the full dossier (contract, "
            "ISIN, corporate actions, pending-split warnings). "
            "market_status() and sector_view() are cheap (no price fan-out; "
            "averages populate only after quotes() calls). All prices are "
            "USD strings from the API, normalized to floats here."),
    )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "service": "arcus-agent-gateway"})

    # read-only by design; destructiveHint explicit False (the spec
    # default true would mislabel this keyless analytics gateway)
    RO = {"readOnlyHint": True, "destructiveHint": False,
          "openWorldHint": True}
    for tool in (token_list, quote, quotes, token_detail, market_status,
                 corporate_actions, search, sector_view, onchain_info,
                 holder_snapshot, wallet_holdings, transfer_history,
                 price_history):
        mcp.tool(tool, annotations=RO)
    return mcp


def main():
    """Console entry point (arcus-agent-gateway). stdio by default for
    local agents; --http for the hosted form (port 8902)."""
    import argparse
    ap = argparse.ArgumentParser(prog="arcus-agent-gateway")
    ap.add_argument("--http", action="store_true",
                    help="streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = ap.parse_args()
    mcp = build_server()
    if a.http:
        mcp.run(transport="http", host=a.host, port=a.port, show_banner=False)
    else:
        mcp.run(show_banner=False)  # stdio: no banner noise in agent logs


if __name__ == "__main__":
    main()
