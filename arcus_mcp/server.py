"""Arcus Agent Gateway - MCP server (Robinhood Chain / rhj tokenized equities).

Read-only, keyless market-data tools per the design spec: token_list, quote(s),
token_detail, market_status, corporate_actions, search, sector_view,
onchain_info. Sectors come from the static validated map in sectors.py.

Style follows dydx_mcp/server.py: plain functions registered via
build_server() -> FastMCP, all annotated read-only. All upstream access
goes through the `api` module namespace (api.assets() etc.) so tests can
monkeypatch it - never `from .api import x`.
"""
from __future__ import annotations

from . import api
from . import sectors

TRADABLE = "TRADING_STATUS_TRADABLE"
DEFAULT_PORT = 8902


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


def quotes(symbols: list[str]) -> dict:
    """Batch of quote() rows, up to 20 per call (more raises). Unknown
    symbols do not fail the batch - they land in
    errors: [{symbol, reason}] while the rest return normally.
    Example: quotes(symbols=["AAPL", "MSFT", "NVDA"])"""
    syms = [s for s in (symbols or []) if s and s.strip()]
    if len(syms) > 20:
        raise ValueError(
            f"quotes: {len(syms)} symbols > 20 per call - split the batch")
    out, errors = [], []
    for s in syms:
        try:
            out.append(quote(s))
        except ValueError as e:  # unknown symbol: collect, don't fail batch
            errors.append({"symbol": s, "reason": str(e)})
    return {"quotes": out, "errors": errors}


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
    *From/*To/splitFrom/splitTo for the rates). `raw` keeps the original
    untouched."""
    typ = next((act[k] for k in ("type", "actionType", "kind")
                if act.get(k)), None)
    when = next((act[k] for k in ("processDate", "effectiveTime",
                                  "process_date", "effective_time")
                 if act.get(k)), None)
    old_rate = next((act[k] for k in sorted(act)
                     if k.endswith("From") and _f(act[k]) is not None), None)
    new_rate = next((act[k] for k in sorted(act)
                     if k.endswith("To") and _f(act[k]) is not None), None)
    return {
        "symbol": act.get("tokenSymbol"),
        "type": typ,
        "status": act.get("status"),
        "process_date": when,
        "details": {"old_rate": _f(old_rate), "new_rate": _f(new_rate)},
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


def search(query: str) -> dict:
    """Local fuzzy search over the token list - no HTTP beyond the cached
    assets(). Ranking: exact symbol > symbol prefix > name-word prefix >
    substring in name/symbol (case-insensitive); top 10 with score.
    'apple' -> AAPL. Results carry symbol, name, status, sector, score.
    Example: search(query="apple")"""
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
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    top = [row for _, _, _, row in scored[:10]]
    return {"query": query, "results": top, "count": len(top)}


def sector_view() -> dict:
    """Sector map (13 sectors, static validated classification) with
    per-sector size and - where quotes are ALREADY cached - live
    averages. Makes no requests: avg_bid_adjusted/avg_ask_adjusted are
    None until quotes() has been called for that sector's symbols
    (quoted counts how many are cached; usually 0 right after start).
    halted lists cached-halted symbols per sector.
    Example: sector_view()"""
    cached = _cached_quotes()
    out: dict[str, dict] = {}
    for name, syms in sectors.SECTORS.items():
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
    return {
        "sectors": out,
        "note": "call quotes([...sector symbols...]) first to populate "
                "live averages (max 20 per call); averages are "
                "multiplier-adjusted",
    }


def onchain_info(symbol: str) -> dict:
    """On-chain footprint of a token: contract address, chain id (4663,
    Robinhood Chain), network, decimals and ISIN. v0.2 stub - full
    on-chain data (balances via eth_call, Transfer/Split history via
    getLogs against an Alchemy endpoint) is planned for v0.2.
    Example: onchain_info(symbol="AAPL")"""
    a = _require_symbol(symbol)
    dep = (a.get("deployments") or [{}])[0]
    return {
        "symbol": a.get("tokenSymbol"),
        "contract": dep.get("contractAddress"),
        "chain_id": dep.get("chainId"),
        "network": dep.get("networkName"),
        "decimals": a.get("tokenDecimals"),
        "isin": a.get("isin"),
        "note": "Full on-chain data (Alchemy eth_call/getLogs) planned "
                "for v0.2",
    }


# --------------------------------------------------------------- MCP wire


def build_server():
    """FastMCP instance with the 9 read-only spec tools wired."""
    from fastmcp import FastMCP
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    mcp = FastMCP(
        "arcus-agent-gateway",
        version="0.1.0",
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
                 corporate_actions, search, sector_view, onchain_info):
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
