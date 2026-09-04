"""Offline smoke test for arcus_mcp.server - zero HTTP, fixtures only.

Monkeypatches api.assets/api.asset/api.prices/api.corporate_actions in
the arcus_mcp.server namespace (server only ever calls api.<fn>(), by
design) and exercises all 9 MCP tools of MEC-53 plus the spec invariants:

  * adjusted prices: bid_adjusted == round(bid_raw * multiplier, 6)
  * spread_raw >= 0
  * unknown symbol -> ValueError mentioning token_list
  * quotes() with 21 symbols -> error
  * no-quote symbol -> metadata with bid/ask None + note
  * sector averages populate only after quotes() (cache-driven)
  * corporate-actions field-name tolerance (3 synthetic variants)
  * fastmcp build_server() wires all 10 tools with RO annotations

Run: .venv/bin/python scripts/smoke_server_offline.py
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures"
sys.path.insert(0, str(REPO))

import arcus_mcp.api as api          # noqa: E402
import arcus_mcp.server as srv       # noqa: E402
from arcus_mcp import sectors        # noqa: E402

checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


# ---------------------------------------------------------------- fixtures

ASSETS = json.loads((FIXTURES / "assets.json").read_text())
ASSETS = ASSETS.get("assets", ASSETS) if isinstance(ASSETS, dict) else ASSETS
AAPL_QUOTE = json.loads((FIXTURES / "prices_AAPL.json").read_text())["quotes"][0]


def _clone(sym, **over):
    """A synthetic-but-schema-true asset row for a symbol not in the map."""
    base = json.loads(json.dumps(ASSETS[0]))
    base.update({"tokenSymbol": sym, "tokenName": f"{sym} • Robinhood Token",
                 "isin": "US0000000000"})
    base.update(over)
    return base


# Synthetic assets exercising the edge cases (deltas vs the 194 fixtures):
#  - SPLT: pendingMultiplier "4.0..." (pending split, like NVDA 2026-11)
#  - HALT: quote with isTradingHalt true
#  - FRAC: fractional-untradable token (shows up in untradable count)
#  - NOQT: valid asset with NO quote (metadata-only path)
SPLT = _clone("SPLT", pendingMultiplier="4.000000000000000000")
HALT = _clone("HALT")
FRAC = _clone("FRAC", tradingCapabilities={
    "market": {"whole": "TRADING_STATUS_TRADABLE",
               "fractional": "TRADING_STATUS_UNTRADABLE"},
    "extended": {"whole": "TRADING_STATUS_TRADABLE",
                 "fractional": "TRADING_STATUS_TRADABLE"},
    "overnight": {"whole": "TRADING_STATUS_UNTRADABLE",
                  "fractional": "TRADING_STATUS_UNTRADABLE"},
})
NOQT = _clone("NOQT")
INACT = _clone("INACT", status="ASSET_STATUS_INACTIVE")
MOCK_ASSETS = ASSETS + [SPLT, HALT, FRAC, NOQT, INACT]

# Corporate actions: 3 records with DIFFERENT field-name variants to pin
# the tolerant mapping (type|actionType|kind, *From/*To, splitFrom/To,
# processDate|effectiveTime).
MOCK_ACTIONS = [
    {"tokenSymbol": "AAPL", "kind": "DIVIDEND", "amount": "0.25",
     "processDate": "2026-08-11"},
    {"tokenSymbol": "SPLT", "actionType": "SPLIT", "status": "PENDING",
     "splitFrom": "1", "splitTo": "4",
     "effectiveTime": "2026-11-06T00:00:00Z"},
    {"tokenSymbol": "MSFT", "type": "SPLIT", "status": "PROCESSED",
     "rateFrom": "2", "rateTo": "1",
     "effectiveTime": "2026-03-27T00:00:00Z"},
]

# Prices: fixture AAPL + synthetics. Keyed by normalized symbol.
MOCK_PRICES = {
    "AAPL": AAPL_QUOTE,
    "HALT": {**AAPL_QUOTE, "tokenSymbol": "HALT", "bid": "9.99",
             "ask": "10.01", "isTradingHalt": True},
    "SPLT": {**AAPL_QUOTE, "tokenSymbol": "SPLT", "bid": "120.00",
             "ask": "120.02", "isTradingHalt": False},
    "FRAC": {**AAPL_QUOTE, "tokenSymbol": "FRAC", "bid": "5.00",
             "ask": "5.02", "isTradingHalt": False},
    # NOQT deliberately absent -> quote() metadata-only path
}

# api's real price cache is used by market_status()/sector_view(): seed
# it exactly the way api.prices() would (monotonic ts -> raw quote dict).
import time as _time                        # noqa: E402
api._PRICES_CACHE.clear()
for _sym, _q in MOCK_PRICES.items():
    api._PRICES_CACHE[_sym] = (_time.monotonic(), _q)


# ------------------------------------------------------------ monkeypatch

srv.api.assets = lambda: MOCK_ASSETS
srv.api.asset = lambda symbol: next(
    (a for a in MOCK_ASSETS
     if a.get("tokenSymbol", "").upper() == (symbol or "").strip().upper()),
    None)
srv.api.prices = lambda symbol: MOCK_PRICES.get((symbol or "").strip().upper())
srv.api.corporate_actions = lambda symbol=None, limit=10: [
    x for x in MOCK_ACTIONS
    if symbol is None
    or x.get("tokenSymbol", "").upper() == (symbol or "").strip().upper()
][:limit]

HTTP_ATTEMPTS = []


def _no_http(*a, **k):
    HTTP_ATTEMPTS.append(1)
    raise RuntimeError("live HTTP attempted in offline smoke!")


srv.api.get = _no_http  # any leak through to get() fails loudly below

# ---------------------------------------------------------------- sectors

check("SECTORS matches the validated map (13 sectors, 194 symbols)",
      len(sectors.SECTORS) == 13
      and sum(len(v) for v in sectors.SECTORS.values()) == 194)
src_map = json.loads(Path("/tmp/mec54/sector_map_final.json").read_text()) \
    if Path("/tmp/mec54/sector_map_final.json").exists() else sectors.SECTORS
check("SECTORS content identical to source map", sectors.SECTORS == src_map)
check("sector_of: known -> sector, unknown -> 'Unknown'",
      sectors.sector_of("aapl") == "Tech"
      and sectors.sector_of("ZZZZ") == "Unknown"
      and sectors.sector_of("WYFI") == "Other")  # residual bucket

# ------------------------------------------------------------- token_list

tl = srv.token_list()
check("token_list: ACTIVE default hides inactive",
      tl["total_matching"] == 194 + 4  # 194 live + SPLT/HALT/FRAC/NOQT
      and all(t["status"] == "ASSET_STATUS_ACTIVE" for t in tl["tokens"]))
check("token_list: count/limit honored", tl["count"] == 100)
tl_all = srv.token_list(status="ALL")
check("token_list: status=ALL includes INACT (199)",
      tl_all["total_matching"] == 199)
aapl_row = next(t for t in tl["tokens"] if t["symbol"] == "AAPL")
_aapl_asset = next(a for a in ASSETS if a["tokenSymbol"] == "AAPL")
check("token_list row: {symbol,name,status,multiplier,tradable}",
      aapl_row["multiplier"] == float(_aapl_asset["currentMultiplier"])
      and aapl_row["tradable"] is True
      and "Robinhood Token" in aapl_row["name"],
      f"multiplier={aapl_row['multiplier']}")
frac_row = next(t for t in tl["tokens"] if t["symbol"] == "FRAC")
check("token_list: FRAC tradable=False (fractional untradable)",
      frac_row["tradable"] is False)

# ------------------------------------------------------------------ quote

q = srv.quote("aapl")  # case-insensitive
check("quote: fields present",
      q["symbol"] == "AAPL" and q["bid_raw"] == 327.77
      and q["ask_raw"] == 327.78 and q["is_halted"] is False
      and q["generated_at"] == AAPL_QUOTE["generatedAt"])
check("quote: invariant bid_adjusted == round(bid_raw*mult, 6)",
      q["bid_adjusted"] == round(q["bid_raw"] * q["multiplier"]["current"], 6)
      and q["ask_adjusted"] == round(q["ask_raw"] * q["multiplier"]["current"], 6)
      and q["mid_adjusted"] == round((q["bid_raw"] + q["ask_raw"]) / 2
                                     * q["multiplier"]["current"], 6))
check("quote: spread_raw = ask-bid >= 0",
      q["spread_raw"] == round(q["ask_raw"] - q["bid_raw"], 6)
      and q["spread_raw"] >= 0)
check("quote: trading_capabilities normalized",
      q["trading_capabilities"] == {"fractional": True, "all_day": True,
                                    "extended_hours": True})
_m = float(_aapl_asset["currentMultiplier"])
check("quote: multiplier block current float, pending None",
      q["multiplier"]["current"] == _m and q["multiplier"]["pending"] is None,
      f"current={q['multiplier']['current']}")

qs = srv.quote("SPLT")
check("quote: pending split -> multiplier.pending=4.0, effective_time set",
      qs["multiplier"]["pending"] == 4.0
      and qs["multiplier"]["effective_time"] == "2026-11-06T00:00:00Z")
check("quote SPLT: adjusted with multiplier 1.0 pending not applied",
      qs["bid_adjusted"] == round(120.00 * 1.0, 6))

qh = srv.quote("HALT")
check("quote: is_halted top-level bool True", qh["is_halted"] is True)

qn = srv.quote("NOQT")
check("quote: no quote -> metadata, bid/ask None + note",
      qn["bid_raw"] is None and qn["ask_raw"] is None and "note" in qn)

try:
    srv.quote("NOPE")
    check("quote: unknown symbol raises", False)
except ValueError as e:
    check("quote: unknown symbol raises ValueError mentioning token_list",
          "token_list" in str(e), str(e)[:90] + "...")

# ----------------------------------------------------------------- quotes

batch = srv.quotes(["AAPL", "MSFT", "NOPE"])
check("quotes: batch returns quotes + errors separately",
      len(batch["quotes"]) == 2 and batch["errors"][0]["symbol"] == "NOPE")
try:
    srv.quotes([f"S{i}" for i in range(21)])
    check("quotes: >20 symbols raises", False)
except ValueError as e:
    check("quotes: >20 symbols raises ValueError", "20" in str(e))

# ----------------------------------------------------------- token_detail

td = srv.token_detail("SPLT")
check("token_detail: metadata block (contract, chain 4663, isin)",
      td["metadata"]["chain_id"] == 4663
      and td["metadata"]["contract"].startswith("0x")
      and td["metadata"]["isin"])
check("token_detail: quote embedded", td["quote"]["symbol"] == "SPLT")
check("token_detail: last 5 corporate actions, tolerant mapping",
      td["corporate_actions"][0]["type"] == "SPLIT"
      and td["corporate_actions"][0]["details"] == {"old_rate": 1.0,
                                                    "new_rate": 4.0,
                                                    "rate": None}
      and "splitFrom" in td["corporate_actions"][0]["raw"])
check("token_detail: multiplier with history_note + pending",
      td["multiplier"]["pending"] == 4.0
      and isinstance(td["multiplier"]["history_note"], str))
check("token_detail: warning 'pending split: 1->4.0 on <date>'",
      any("pending split" in w and "4.0" in w
          and "2026-11-06" in w for w in td["warnings"]),
      "; ".join(td["warnings"]))
check("token_detail: trading_capabilities raw statuses included",
      td["trading_capabilities"]["raw"]["market"]["fractional"]
      == "TRADING_STATUS_TRADABLE")
tda = srv.token_detail("AAPL")
check("token_detail AAPL: no pending -> warnings empty",
      tda["multiplier"]["pending"] is None and tda["warnings"] == [])

# ---------------------------------------------------------- market_status

ms = srv.market_status()
_untradable_fixtures = [  # WYFI/SLS/XNDU in fixtures + synthetic FRAC
    a["tokenSymbol"] for a in ASSETS
    if (a.get("tradingCapabilities") or {}).get("market", {})
    .get("fractional") != "TRADING_STATUS_TRADABLE"]
check("market_status: counts from assets() only",
      ms["total_tokens"] == 199 and ms["active"] == 198
      and ms["untradable"] == len(_untradable_fixtures) + 1,
      f"untradable={ms['untradable']} (fixtures {len(_untradable_fixtures)} + FRAC)")
check("market_status: halted from cached quotes (HALT), no fan-out",
      ms["halted"] == ["HALT"])
check("market_status: extended_hours_open + status string",
      ms["extended_hours_open"] is True and "tradingCapabilities" in
      ms["market_hours_status"] and "note" in ms)

# ------------------------------------------------------ corporate_actions

ca = srv.corporate_actions()
check("corporate_actions: all 3, count field",
      ca["count"] == 3 and len(ca["actions"]) == 3)
types = [(a["symbol"], a["type"], a["details"]) for a in ca["actions"]]
check("corporate_actions: field variants normalized (kind/actionType/type)",
      [t[1] for t in types] == ["DIVIDEND", "SPLIT", "SPLIT"]
      and types[2][2] == {"old_rate": 2.0, "new_rate": 1.0, "rate": None},
      str(types))
ca_s = srv.corporate_actions(symbol="MSFT", limit=5)
check("corporate_actions: symbol filter + limit",
      ca_s["count"] == 1 and ca_s["actions"][0]["symbol"] == "MSFT")

# ----------------------------------------------------------------- search

sr = srv.search("apple")
check("search 'apple' -> AAPL first (name-word match)",
      sr["results"][0]["symbol"] == "AAPL" and sr["count"] >= 1)
sr2 = srv.search("aapl")
check("search 'aapl': exact > prefix ranking",
      sr2["results"][0]["symbol"] == "AAPL"
      and sr2["results"][0]["score"] == 100)
sr3 = srv.search("nv")
check("search 'nv': NVDA prefix beats substring matches",
      sr3["results"][0]["symbol"] == "NVDA")
check("search: results carry sector", "sector" in sr2["results"][0])
check("search: top-10 cap", srv.search("a")["count"] <= 10)
check("search: empty query -> empty", srv.search("  ")["count"] == 0)

# ------------------------------------------------------------- sector_view

sv = srv.sector_view()
tech = sv["sectors"]["Tech"]
check("sector_view: 13 sectors, sizes from static map",
      len(sv["sectors"]) == 13 and tech["symbols"] == 50)
check("sector_view: quoted counts from cache (AAPL cached in Tech)",
      tech["quoted"] == 1 and tech["avg_bid_adjusted"] is not None
      and tech["avg_ask_adjusted"] is not None,
      f"quoted={tech['quoted']}")
check("sector_view: empty sector -> avg None",
      sv["sectors"]["Space"]["avg_bid_adjusted"] is None
      and sv["sectors"]["Space"]["quoted"] == 0)
check("sector_view: note explains quotes() priming", "quotes(" in sv["note"])

# ------------------------------------------------------------ onchain_info

oc = srv.onchain_info("AAPL")
check("onchain_info: contract/chain/decimals/isin + v0.2 note",
      oc["chain_id"] == 4663 and oc["decimals"] == 18
      and oc["contract"].startswith("0x") and oc["isin"]
      and "v0.2" in oc["note"])
try:
    srv.onchain_info("NOPE")
    check("onchain_info: unknown raises", False)
except ValueError as e:
    check("onchain_info: unknown symbol raises", "token_list" in str(e))

# -------------------------------------------------------------- MCP wire

mcp = srv.build_server()

import asyncio  # noqa: E402


async def _tools_meta():
    ts = await mcp.list_tools()          # fastmcp 4.x public API
    meta = {}
    for t in ts:
        ann = t.annotations or {}
        ro = (ann.get("read_only_hint") if isinstance(ann, dict)
              else getattr(ann, "read_only_hint", None))
        de = (ann.get("destructive_hint") if isinstance(ann, dict)
              else getattr(ann, "destructive_hint", None))
        meta[t.name] = (ro, de)
    return {t.name for t in ts}, meta


tool_names, tool_meta = asyncio.run(_tools_meta())

EXPECTED = {"token_list", "quote", "quotes", "token_detail", "market_status",
            "corporate_actions", "search", "sector_view", "onchain_info",
            "price_history"}
check("build_server: exactly the 10 v0.1.1 tools registered",
      tool_names == EXPECTED, f"got {sorted(tool_names)}")
check("build_server: all tools read-only annotated",
      all(v == (True, False) for v in tool_meta.values()), str(tool_meta))


async def _call_quote():
    res = await mcp.call_tool("quote", {"symbol": "aapl"})
    content = res.content[0].text if res.content else "{}"
    return json.loads(content) if isinstance(content, str) else content


wire = asyncio.run(_call_quote())
check("MCP wire: call_tool('quote') round-trips through fastmcp",
      wire.get("symbol") == "AAPL" and wire.get("bid_raw") == 327.77
      and wire.get("bid_adjusted") is not None)

# ----------------------------------------------------------------- hygiene

check("zero live HTTP calls across the whole smoke", not HTTP_ATTEMPTS,
      f"attempts={len(HTTP_ATTEMPTS)}")

failed = [c for c in checks if not c[1]]
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
if failed:
    print("FAILED:", *[f"  - {n}" for n, _, _ in failed], sep="\n")
sys.exit(1 if failed else 0)
