"""Unit tests for arcus_mcp.server tools - 100% offline.

Every test monkeypatches the api module surface (api.assets / api.asset
/ api.prices / api.corporate_actions / api.get) so no HTTP can happen;
api.get raises loudly if anything leaks through. Data = the live
fixtures in tests/fixtures/ (194 assets, real AAPL quote) plus synthetic
rows for the edge cases the spec demands:

  SPLT  pendingMultiplier "4.0..." + SPLIT action with effectiveTime
  HALT  quote with isTradingHalt=true
  FRAC  fractional-untradable asset
  NOQT  valid asset, no quote (metadata-only path)
  INACT ASSET_STATUS_INACTIVE row

Pinned the spec invariants (see scripts/smoke_server_offline.py):
  (a) bid_adjusted == round(bid_raw * multiplier, 6)
  (b) spread_raw == ask_raw - bid_raw >= 0
  (c) halted token: in market_status().halted (cached) + is_halted in quote()
  (d) unknown symbol -> ValueError mentioning token_list
  (e) quotes() with 21+ symbols -> ValueError
  (f) quotes(): unknown -> errors, known -> quotes
  (g) token_detail warnings on pendingMultiplier + effectiveTime
  (h) sector_view with cold cache: avg None + note
  (i) search('apple') -> AAPL first
  (j) onchain_info: v0.2 stub note
"""
import asyncio
import json
import time
from pathlib import Path

import pytest

import arcus_mcp.api as api
import arcus_mcp.sectors as sectors
import arcus_mcp.server as srv

FIXTURES = Path(__file__).parent / "fixtures"


# ----------------------------------------------------------------- test data

ASSETS = json.loads((FIXTURES / "assets.json").read_text())
ASSETS = ASSETS.get("assets", ASSETS) if isinstance(ASSETS, dict) else ASSETS
AAPL_QUOTE = json.loads(
    (FIXTURES / "prices_AAPL.json").read_text())["quotes"][0]
AAPL_ASSET = next(a for a in ASSETS if a["tokenSymbol"] == "AAPL")
UNTRADABLE_FIXTURE = [  # WYFI/SLS/XNDU-style rows in the live snapshot
    a["tokenSymbol"] for a in ASSETS
    if (a.get("tradingCapabilities") or {}).get("market", {})
    .get("fractional") != "TRADING_STATUS_TRADABLE"]


def _clone(sym: str, **over) -> dict:
    """Schema-true synthetic asset row (fixture row + overrides)."""
    base = json.loads(json.dumps(ASSETS[0]))
    base.update({"tokenSymbol": sym, "tokenName": f"{sym} • Robinhood Token",
                 "isin": "US0000000000"})
    base.update(over)
    return base


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

MOCK_ACTIONS = [  # three field-name variants (API_NOTES #5)
    {"tokenSymbol": "AAPL", "kind": "DIVIDEND", "amount": "0.25",
     "processDate": "2026-08-11"},
    {"tokenSymbol": "SPLT", "actionType": "SPLIT", "status": "PENDING",
     "splitFrom": "1", "splitTo": "4",
     "effectiveTime": "2026-11-06T00:00:00Z"},
    {"tokenSymbol": "MSFT", "type": "SPLIT", "status": "PROCESSED",
     "rateFrom": "2", "rateTo": "1",
     "effectiveTime": "2026-03-27T00:00:00Z"},
]

MOCK_PRICES = {  # keyed by normalized symbol; NOQT deliberately absent
    "AAPL": AAPL_QUOTE,
    "HALT": {**AAPL_QUOTE, "tokenSymbol": "HALT", "bid": "9.99",
             "ask": "10.01", "isTradingHalt": True},
    "SPLT": {**AAPL_QUOTE, "tokenSymbol": "SPLT", "bid": "120.00",
             "ask": "120.02", "isTradingHalt": False},
    "FRAC": {**AAPL_QUOTE, "tokenSymbol": "FRAC", "bid": "5.00",
             "ask": "5.02", "isTradingHalt": False},
}


def _norm(s: str) -> str:
    return (s or "").strip().upper()


# ------------------------------------------------------------------ fixtures

@pytest.fixture
def mock_api(monkeypatch):
    """Replace the api surface the server touches; seed the price cache.

    Yields the MOCK_PRICES-backed api module. Any attempt to reach
    api.get() (live HTTP) fails the test loudly.
    """
    def _no_get(*a, **k):  # pragma: no cover - only on a bug
        raise RuntimeError("live HTTP attempted in offline test!")

    monkeypatch.setattr(api, "assets", lambda: MOCK_ASSETS)
    monkeypatch.setattr(api, "asset", lambda symbol: next(
        (a for a in MOCK_ASSETS
         if a.get("tokenSymbol", "").upper() == _norm(symbol)), None))
    monkeypatch.setattr(api, "prices",
                        lambda symbol: MOCK_PRICES.get(_norm(symbol)))
    monkeypatch.setattr(api, "corporate_actions",
                        lambda symbol=None, limit=10: [
                            x for x in MOCK_ACTIONS
                            if symbol is None
                            or x.get("tokenSymbol", "").upper()
                            == _norm(symbol)][:limit])
    monkeypatch.setattr(api, "get", _no_get)
    # seed the price cache exactly like api.prices() would
    monkeypatch.setattr(api, "_PRICES_CACHE", {
        sym: (0.0, q) for sym, q in MOCK_PRICES.items()})
    return api


@pytest.fixture
def cold_cache(mock_api, monkeypatch):
    """mock_api but with an EMPTY price cache (fresh server start)."""
    monkeypatch.setattr(api, "_PRICES_CACHE", {})
    return api


# ---------------------------------------------------------------- token_list

def test_token_list_active_default_hides_inactive(mock_api):
    tl = srv.token_list()
    assert tl["total_matching"] == 198  # 194 live + SPLT/HALT/FRAC/NOQT
    assert all(t["status"] == "ASSET_STATUS_ACTIVE" for t in tl["tokens"])
    assert "INACT" not in [t["symbol"] for t in tl["tokens"]]


def test_token_list_limit_and_count(mock_api):
    tl = srv.token_list(limit=50)
    assert tl["count"] == 50 and len(tl["tokens"]) == 50
    assert tl["total_matching"] == 198


def test_token_list_status_all_includes_inactive(mock_api):
    tl = srv.token_list(status="ALL")
    assert tl["total_matching"] == 199  # + INACT


def test_token_list_row_shape_and_multiplier(mock_api):
    tl = srv.token_list(status="ALL", limit=500)
    row = next(t for t in tl["tokens"] if t["symbol"] == "AAPL")
    assert set(row) == {"symbol", "name", "status", "multiplier",
                        "tradable"}
    assert row["multiplier"] == float(AAPL_ASSET["currentMultiplier"])
    assert row["tradable"] is True


def test_token_list_tradable_false_when_fractional_untradable(mock_api):
    tl = srv.token_list()
    row = next(t for t in tl["tokens"] if t["symbol"] == "FRAC")
    assert row["tradable"] is False


# -------------------------------------------------------------------- quote

def test_quote_joins_fixture_price_and_metadata(mock_api):
    q = srv.quote("aapl")  # case-insensitive
    assert q["symbol"] == "AAPL"
    assert q["bid_raw"] == 327.77 and q["ask_raw"] == 327.78
    assert q["is_halted"] is False
    assert q["generated_at"] == AAPL_QUOTE["generatedAt"]
    assert q["daily_volume"] == 25806784


def test_quote_invariant_adjusted_equals_raw_times_multiplier(mock_api):
    """spec invariant (a): adjusted == round(raw * current multiplier, 6)."""
    for sym in ("AAPL", "HALT", "SPLT", "FRAC"):
        q = srv.quote(sym)
        m = q["multiplier"]["current"]
        assert q["bid_adjusted"] == round(q["bid_raw"] * m, 6), sym
        assert q["ask_adjusted"] == round(q["ask_raw"] * m, 6), sym
        assert q["mid_adjusted"] == round(
            (q["bid_raw"] + q["ask_raw"]) / 2 * m, 6), sym


def test_quote_invariant_spread_non_negative(mock_api):
    """spec invariant (b): spread_raw == ask_raw - bid_raw, >= 0."""
    for sym in ("AAPL", "HALT", "SPLT", "FRAC"):
        q = srv.quote(sym)
        assert q["spread_raw"] == round(
            q["ask_raw"] - q["bid_raw"], 6), sym
        assert q["spread_raw"] >= 0, sym


def test_quote_multiplier_block_raw_and_pending(mock_api):
    q = srv.quote("AAPL")
    assert q["multiplier"]["current"] == float(
        AAPL_ASSET["currentMultiplier"])
    assert q["multiplier"]["pending"] is None  # "" -> None, never parsed
    qs = srv.quote("SPLT")
    assert qs["multiplier"]["pending"] == 4.0
    assert qs["multiplier"]["effective_time"] == "2026-11-06T00:00:00Z"


def test_quote_trading_capabilities_normalized(mock_api):
    assert srv.quote("AAPL")["trading_capabilities"] == {
        "fractional": True, "all_day": True, "extended_hours": True}


def test_quote_halted_token_is_halted_true(mock_api):
    """spec invariant (c): is_halted=true prominently (top level) in quote()."""
    q = srv.quote("HALT")
    assert q["is_halted"] is True


def test_quote_no_quote_returns_metadata_with_note(mock_api):
    q = srv.quote("NOQT")
    assert q["symbol"] == "NOQT"
    assert q["bid_raw"] is None and q["ask_raw"] is None
    assert q["bid_adjusted"] is None and q["ask_adjusted"] is None
    assert "note" in q


def test_quote_unknown_symbol_mentions_token_list(mock_api):
    """spec invariant (d): actionable ValueError pointing at token_list()."""
    with pytest.raises(ValueError, match="token_list"):
        srv.quote("NOPE")


# ------------------------------------------------------------------- quotes

def test_quotes_batch_known_and_unknown_split(mock_api):
    """spec invariant (f): unknown -> errors, known -> quotes."""
    out = srv.quotes(["AAPL", "MSFT", "NOPE"])
    assert [q["symbol"] for q in out["quotes"]] == ["AAPL", "MSFT"]
    assert len(out["errors"]) == 1
    assert out["errors"][0]["symbol"] == "NOPE"
    assert "token_list" in out["errors"][0]["reason"]


def test_quotes_max_20_symbols(mock_api):
    """spec invariant (e): 21 symbols -> ValueError telling to split the batch."""
    with pytest.raises(ValueError, match="20"):
        srv.quotes([f"S{i}" for i in range(21)])
    ok = srv.quotes(["AAPL"] * 20)  # exactly 20 is allowed (dupes ok)
    assert len(ok["quotes"]) == 20


def test_quotes_filters_blank_symbols(mock_api):
    out = srv.quotes(["AAPL", "", "  "])
    assert [q["symbol"] for q in out["quotes"]] == ["AAPL"]


def test_quotes_adjusted_invariant_holds_for_batch(mock_api):
    out = srv.quotes(["AAPL", "HALT", "SPLT", "FRAC"])
    for q in out["quotes"]:
        m = q["multiplier"]["current"]
        assert q["bid_adjusted"] == round(q["bid_raw"] * m, 6)
        assert q["spread_raw"] >= 0


# ------------------------------------------------------------- token_detail

def test_token_detail_metadata_block(mock_api):
    td = srv.token_detail("SPLT")
    md = td["metadata"]
    assert md["symbol"] == "SPLT"
    assert md["chain_id"] == 4663  # Robinhood Chain
    assert str(md["contract"]).startswith("0x")
    assert md["isin"] and md["decimals"] == 18


def test_token_detail_embeds_quote_view(mock_api):
    td = srv.token_detail("AAPL")
    assert td["quote"]["symbol"] == "AAPL"
    assert td["quote"]["bid_raw"] == 327.77


def test_token_detail_pending_split_warning(mock_api):
    """spec invariant (g): pendingMultiplier + effectiveTime -> warnings entry."""
    td = srv.token_detail("SPLT")
    assert td["multiplier"]["pending"] == 4.0
    assert any("pending split" in w and "4.0" in w
               and "2026-11-06" in w for w in td["warnings"]), (
        td["warnings"])


def test_token_detail_no_pending_no_warnings(mock_api):
    td = srv.token_detail("AAPL")
    assert td["multiplier"]["pending"] is None
    assert td["warnings"] == []


def test_token_detail_multiplier_history_note(mock_api):
    assert isinstance(srv.token_detail("AAPL")["multiplier"]
                      ["history_note"], str)


def test_token_detail_corporate_actions_tolerant_mapping(mock_api):
    td = srv.token_detail("SPLT")
    rows = td["corporate_actions"]
    assert len(rows) == 1
    assert rows[0]["type"] == "SPLIT"           # actionType variant
    assert rows[0]["details"] == {"old_rate": 1.0, "new_rate": 4.0, "rate": None}
    assert "splitFrom" in rows[0]["raw"]        # original kept untouched


def test_token_detail_raw_trading_capabilities_kept(mock_api):
    tc = srv.token_detail("AAPL")["trading_capabilities"]
    assert tc["raw"]["market"]["fractional"] == "TRADING_STATUS_TRADABLE"
    assert tc["fractional"] is True


def test_token_detail_unknown_symbol(mock_api):
    with pytest.raises(ValueError, match="token_list"):
        srv.token_detail("NOPE")


# ------------------------------------------------------------ market_status

def test_market_status_counts_from_assets_only(mock_api):
    ms = srv.market_status()
    assert ms["total_tokens"] == 199  # 194 + SPLT/HALT/FRAC/NOQT/INACT
    assert ms["active"] == 198
    assert ms["untradable"] == len(UNTRADABLE_FIXTURE) + 1  # + FRAC


def test_market_status_halted_from_cached_quotes(mock_api):
    """spec invariant (c): halted token (cached price) shows in market_status()."""
    ms = srv.market_status()
    assert ms["halted"] == ["HALT"]


def test_market_status_halted_empty_when_cache_cold(cold_cache):
    ms = srv.market_status()
    assert ms["halted"] == []


def test_market_status_fields_and_note(mock_api):
    ms = srv.market_status()
    assert ms["extended_hours_open"] is True
    assert "tradingCapabilities" in ms["market_hours_status"]
    assert "note" in ms and "quotes()" in ms["note"]


# -------------------------------------------------------- corporate_actions

def test_corporate_actions_all_with_count(mock_api):
    ca = srv.corporate_actions()
    assert ca["count"] == 3 and len(ca["actions"]) == 3


def test_corporate_actions_field_variants_normalized(mock_api):
    rows = srv.corporate_actions()["actions"]
    assert [r["type"] for r in rows] == ["DIVIDEND", "SPLIT", "SPLIT"]
    msft = next(r for r in rows if r["symbol"] == "MSFT")
    assert msft["type"] == "SPLIT"                # `type` variant
    assert msft["details"] == {"old_rate": 2.0, "new_rate": 1.0, "rate": None}


def test_corporate_actions_symbol_filter_and_limit(mock_api):
    ca = srv.corporate_actions(symbol="msft", limit=5)
    assert ca["count"] == 1 and ca["actions"][0]["symbol"] == "MSFT"
    assert srv.corporate_actions(limit=1)["count"] == 1


# ------------------------------------------------------------------- search

def test_search_apple_ranks_aapl_first(mock_api):
    """spec invariant (i): 'apple' -> AAPL first (name-word match)."""
    sr = srv.search("apple")
    assert sr["results"][0]["symbol"] == "AAPL"
    assert sr["count"] >= 1


def test_search_exact_symbol_scores_highest(mock_api):
    sr = srv.search("aapl")
    assert sr["results"][0]["symbol"] == "AAPL"
    assert sr["results"][0]["score"] == 100


def test_search_prefix_beats_substring(mock_api):
    assert srv.search("nv")["results"][0]["symbol"] == "NVDA"


def test_search_rows_carry_sector_and_capped_at_10(mock_api):
    sr = srv.search("a")
    assert sr["count"] <= 10
    assert all("sector" in r for r in sr["results"])


def test_search_empty_query(mock_api):
    assert srv.search("   ") == {"query": "   ", "results": [],
                                 "count": 0}


# --------------------------------------------------------------- sector_view

def test_sector_view_sizes_from_static_map(mock_api):
    sv = srv.sector_view()
    assert len(sv["sectors"]) == 13
    assert sv["sectors"]["Tech"]["symbols"] == 50


def test_sector_view_averages_from_cached_quotes(mock_api):
    sv = srv.sector_view()
    tech = sv["sectors"]["Tech"]  # AAPL cached by the fixture
    assert tech["quoted"] == 1
    assert tech["avg_bid_adjusted"] is not None
    assert tech["avg_ask_adjusted"] is not None
    # average is multiplier-adjusted, not raw
    aapl = srv.quote("AAPL")
    assert tech["avg_bid_adjusted"] == aapl["bid_adjusted"]


def test_sector_view_halted_listed_per_sector(mock_api):
    halt_sector = srv.sector_view()["sectors"]["Other"]
    # HALT is synthetic (not in the map) so no sector claims it;
    # cached-halt accounting lives in market_status().halted instead.
    assert "HALT" not in halt_sector["halted"]


def test_sector_view_cold_cache_avg_none_with_note(cold_cache):
    """spec invariant (h): no cached quotes -> avg None, explanatory note present."""
    sv = srv.sector_view()
    for name, sec in sv["sectors"].items():
        assert sec["avg_bid_adjusted"] is None, name
        assert sec["avg_ask_adjusted"] is None, name
        assert sec["quoted"] == 0, name
    assert "note" in sv and "quotes(" in sv["note"]


def test_sector_view_never_fetches_prices(cold_cache, monkeypatch):
    """sector_view is cache-driven: a cold cache means zero price calls."""
    def _no_prices(symbol):
        raise AssertionError(
            "sector_view() must not fetch prices (cache-only by design)")
    monkeypatch.setattr(cold_cache, "prices", _no_prices)
    sv = srv.sector_view()  # must not raise
    assert len(sv["sectors"]) == 13


# --------------------------------------------------------------- onchain_info

def test_onchain_info_stub_fields(mock_api):
    """spec invariant (j): v0.2 stub - contract/chain/decimals/isin + note."""
    oc = srv.onchain_info("AAPL")
    aapl_dep = AAPL_ASSET["deployments"][0]
    assert oc["symbol"] == "AAPL"
    assert oc["contract"] == aapl_dep["contractAddress"]
    assert oc["chain_id"] == 4663
    assert oc["network"] == "Robinhood Chain"
    assert oc["decimals"] == 18
    assert oc["isin"] == AAPL_ASSET["isin"]
    assert "v0.2" in oc["note"]


def test_onchain_info_unknown_symbol(mock_api):
    with pytest.raises(ValueError, match="token_list"):
        srv.onchain_info("NOPE")


# ---------------------------------------------------------------- MCP wire

EXPECTED_TOOLS = {"token_list", "quote", "quotes", "token_detail",
                  "market_status", "corporate_actions", "search",
                  "sector_view", "onchain_info", "price_history"}


def test_build_server_registers_exactly_ten_readonly_tools(mock_api):
    mcp = srv.build_server()
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOLS

    for t in tools:
        ann = t.annotations or {}
        ro = ann.get("read_only_hint") if isinstance(ann, dict) \
            else getattr(ann, "read_only_hint", None)
        de = ann.get("destructive_hint") if isinstance(ann, dict) \
            else getattr(ann, "destructive_hint", None)
        assert (ro, de) == (True, False), t.name


def test_mcp_wire_call_quote_round_trip(mock_api):
    mcp = srv.build_server()
    res = asyncio.run(mcp.call_tool("quote", {"symbol": "aapl"}))
    payload = json.loads(res.content[0].text)
    assert payload["symbol"] == "AAPL"
    assert payload["bid_raw"] == 327.77
    assert payload["bid_adjusted"] == round(327.77 * payload[
        "multiplier"]["current"], 6)


# ------------------------------------------------- v0.1.1: search limit + rate


def test_search_limit_param(tmp_path, monkeypatch):
    """search(limit=N) caps results; default 10; cap 50."""
    import arcus_mcp.server as srv

    def fake_assets():
        return [{"tokenSymbol": f"S{i:02d}", "tokenName": f"Sym {i}",
                 "status": "ASSET_STATUS_ACTIVE"} for i in range(30)]

    monkeypatch.setattr(srv.api, "assets", fake_assets)
    r_all = srv.search("sym")
    assert r_all["count"] == 10  # default limit
    r3 = srv.search("sym", limit=3)
    assert r3["count"] == 3
    r_big = srv.search("sym", limit=99)  # cap 50
    assert r_big["count"] == 30  # only 30 exist


def test_action_row_dividend_rate():
    """Live shape (2026-09-04): details.cashDividend.rate -> details.rate."""
    import arcus_mcp.server as srv
    act = {
        "id": "0x1", "type": "CORPORATE_ACTION_TYPE_CASH_DIVIDEND",
        "status": "CORPORATE_ACTION_STATUS_IN_PROGRESS",
        "processDate": {"year": 2026, "month": 9, "day": 10},
        "tokenSymbol": "AMAT",
        "details": {"cashDividend": {"underlyingSymbol": "AMAT",
                                     "rate": "0.53"}},
    }
    row = srv._action_row(act)
    assert row["details"]["rate"] == 0.53
    assert row["type"] == "CORPORATE_ACTION_TYPE_CASH_DIVIDEND"
    assert row["process_date"] == "2026-09-10"  # protobuf dict -> ISO


def test_action_row_split_rates_untouched():
    """Split-shaped actions keep old_rate/new_rate; rate stays None."""
    import arcus_mcp.server as srv
    act = {"tokenSymbol": "CRWD", "type": "SPLIT", "splitFrom": "1.0",
           "splitTo": "4.0", "processDate": "2026-09-01"}
    row = srv._action_row(act)
    assert row["details"] == {"old_rate": 1.0, "new_rate": 4.0, "rate": None}

# ------------------------------------------------- v0.1.1 parallel quotes

def test_quotes_parallel_ten_symbols_under_1_5s(monkeypatch):
    """T1 v0.1.1: 10 symbols, each quote costing 300ms of fake RTT,
    finish well under the serial 3s - the semaphore(8) fan-out wall
    clock is ~2 rounds (~0.6s); assert < 1.5s with headroom for
    threads+GIL. All 10 present, input order kept, no errors."""
    syms = [f"P{i}" for i in range(10)]
    assets_rows = [{
        "tokenSymbol": s, "tokenName": f"{s} • Robinhood Token",
        "currentMultiplier": "1.000000000000000000",
        "pendingMultiplier": "",
        "status": "ASSET_STATUS_ACTIVE",
        "tradingCapabilities": {
            "market": {"whole": "TRADING_STATUS_TRADABLE",
                       "fractional": "TRADING_STATUS_TRADABLE"},
            "extended": {"whole": "TRADING_STATUS_TRADABLE",
                         "fractional": "TRADING_STATUS_TRADABLE"},
            "overnight": {"whole": "TRADING_STATUS_TRADABLE",
                          "fractional": "TRADING_STATUS_TRADABLE"}},
    } for s in syms]

    def _slow_prices(symbol):
        time.sleep(0.3)  # fake RTT
        return {"tokenSymbol": symbol, "bid": "10.00", "ask": "10.02",
                "dailyTradingVolume": "1", "isTradingHalt": False,
                "generatedAt": "2026-09-04T00:00:00Z"}

    monkeypatch.setattr(api, "assets", lambda: assets_rows)
    monkeypatch.setattr(api, "asset", lambda symbol: next(
        (a for a in assets_rows
         if a["tokenSymbol"] == (symbol or "").strip().upper()), None))
    monkeypatch.setattr(api, "prices", _slow_prices)
    monkeypatch.setattr(api, "_PRICES_CACHE", {})  # cold: every
    # symbol must really go through prices()

    t0 = time.monotonic()
    out = srv.quotes(syms)
    dt = time.monotonic() - t0
    assert dt < 1.5, f"quotes() took {dt:.2f}s - not parallel"
    assert [q["symbol"] for q in out["quotes"]] == syms  # order kept
    assert out["errors"] == []
    assert all(q["bid_raw"] == 10.0 for q in out["quotes"])


# ---------------------------------------------- v0.1.1 sector_view warm

def test_sector_view_warm_single_sector(mock_api, monkeypatch):
    """T2 v0.1.1: warm the 4-symbol Crypto sector - warmed True,
    requests_made == 4 (cold cache: every symbol is a miss), averages
    populated, and 'sectors' holds ONLY the warmed one."""
    crypto = sectors.SECTORS["Crypto/Digital Assets"]
    assert len(crypto) == 4
    calls = []

    def _caching_prices(symbol):
        # behave like the real api.prices: return + cache the quote so
        # the warm fan-out actually populates _PRICES_CACHE
        want = (symbol or "").strip().upper()
        calls.append(want)
        q = {**AAPL_QUOTE, "tokenSymbol": want,
             "bid": "100.00", "ask": "100.10"}
        monkeypatch.setattr(
            api, "_PRICES_CACHE",
            {**getattr(api, "_PRICES_CACHE", {}),
             want: (time.monotonic(), q)})
        return q

    monkeypatch.setattr(api, "prices", _caching_prices)
    monkeypatch.setattr(api, "_PRICES_CACHE", {})  # cold at start
    sv = srv.sector_view(warm=True, sector="Crypto/Digital Assets")
    assert sv["warmed"] is True
    assert sv["requests_made"] == 4
    assert sorted(calls) == sorted(crypto)  # fan-out hit all 4 symbols
    assert list(sv["sectors"]) == ["Crypto/Digital Assets"]
    row = sv["sectors"]["Crypto/Digital Assets"]
    assert row["quoted"] == 4
    assert row["avg_bid_adjusted"] is not None
    assert row["avg_ask_adjusted"] is not None


def test_sector_view_warm_unknown_sector_raises(mock_api):
    """T3 v0.1.1: unknown sector name -> ValueError with the valid set."""
    with pytest.raises(ValueError, match="Crypto/Digital Assets"):
        srv.sector_view(warm=True, sector="Nope/Nothing")


def test_sector_view_default_makes_no_requests(mock_api, monkeypatch):
    """T4 v0.1.1: default warm=False never touches api.prices."""
    def _no_prices(symbol):
        raise AssertionError(
            "sector_view(warm=False) must not fetch prices")

    monkeypatch.setattr(api, "prices", _no_prices)
    monkeypatch.setattr(api, "_PRICES_CACHE", {})
    sv = srv.sector_view()  # must not raise
    assert sv["warmed"] is False
    assert "requests_made" not in sv
    assert len(sv["sectors"]) == 13

# ------------------------------------------------------------ price_history
# v0.1.1: recorder parquet reads. conftest redirects ARCUS_GATEWAY_DATA
# to a per-session temp dir, so writing under paths.data_dir()/history
# never touches production state. Every test writes its own fixture
# file first (no cross-test file coupling).

def _write_history_parquet(name: str, columns: dict) -> None:
    """Write one parquet file under <data_dir>/history. pyarrow is a
    dev/test dependency only - imported lazily, exactly like the tool."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    hist = srv.paths.data_dir() / "history"
    hist.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(columns), hist / name)


def test_price_history_no_pyarrow_degrades_to_install_hint(monkeypatch):
    """[T1] pyarrow missing -> honest error dict (never an exception).

    Sanity first: without the import mock the same call succeeds, so
    the ImportError path below is genuinely the one under test.
    """
    _write_history_parquet("daily.parquet", {
        "symbol": ["AAPL"], "date": ["2026-09-01"],
        "open": [100.0], "high": [100.5], "low": [99.5],
        "close": [100.2], "volume": [1000.0]})
    ok = srv.price_history("AAPL")
    assert "error" not in ok and ok["count"] == 1

    import builtins
    real_import = builtins.__import__

    def no_pyarrow(name, *args, **kwargs):
        if name == "pyarrow" or name.startswith("pyarrow."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pyarrow)
    out = srv.price_history("AAPL")
    assert "install arcus-agent-gateway[recorder]" in out["error"]
    assert out["hint"] == "README § Optional price history recorder"
    assert out["symbol"] == "AAPL" and out["timeframe"] == "daily"
    assert out["count"] == 0 and out["rows"] == []


def test_price_history_no_data_degrades_daily_and_raw(monkeypatch,
                                                      tmp_path):
    """[T2] recorder never ran -> error dict pointing at the README,
    with a timeframe-specific note."""
    monkeypatch.setattr(srv.paths, "data_dir", lambda: tmp_path)
    daily = srv.price_history("AAPL")
    assert "recorder not enabled" in daily["error"]
    assert "need >= 1 day of snapshots" in daily["note"]
    assert daily["rows"] == [] and daily["count"] == 0
    raw = srv.price_history("AAPL", timeframe="raw")
    assert "recorder not enabled" in raw["error"]
    assert raw["note"] == ("no snapshot files found; run the recorder "
                           "(scripts/recorder.py) to start collecting")
    assert raw["timeframe"] == "raw" and raw["count"] == 0


def test_price_history_daily_happy_path_sorted_desc():
    """[T3] daily bars: newest first, per-symbol filter, lower-case
    input normalized, limit honored."""
    _write_history_parquet("daily.parquet", {
        "symbol": ["AAPL", "AAPL", "AAPL", "MSFT", "MSFT"],
        "date": ["2026-09-01", "2026-09-02", "2026-09-03",
                 "2026-09-02", "2026-09-03"],
        "open": [100.0, 101.0, 102.0, 200.0, 201.0],
        "high": [100.5, 101.5, 102.5, 200.5, 201.5],
        "low": [99.5, 100.5, 101.5, 199.5, 200.5],
        "close": [100.2, 101.2, 102.2, 200.2, 201.2],
        "volume": [1000.0, 1100.0, 1200.0, 2000.0, 2100.0]})
    out = srv.price_history("aapl", limit=2)  # case-insensitive
    assert out["symbol"] == "AAPL" and out["timeframe"] == "daily"
    assert out["count"] == 2 and len(out["rows"]) == 2
    assert all(r["symbol"] == "AAPL" for r in out["rows"])
    assert [r["date"] for r in out["rows"]] == ["2026-09-03",
                                                "2026-09-02"]
    assert set(out["rows"][0]) == {"symbol", "date", "open", "high",
                                   "low", "close", "volume"}


def test_price_history_raw_snapshots_happy_path():
    """[T4] raw timeframe: snapshots_*.parquet rows, ts_utc DESC."""
    _write_history_parquet("snapshots_202609.parquet", {
        "ts_utc": ["2026-09-03T20:00:05Z", "2026-09-03T20:05:05Z",
                   "2026-09-03T20:10:05Z", "2026-09-03T20:05:05Z"],
        "symbol": ["AAPL", "AAPL", "AAPL", "MSFT"],
        "bid_raw": [327.77, 327.80, 327.75, 500.10],
        "ask_raw": [327.78, 327.82, 327.77, 500.12],
        "mid_adjusted": [327.775, 327.81, 327.76, 500.11],
        "daily_volume": [25806784.0, 25810000.0, 25820000.0, 1000000.0],
        "multiplier": [1.0, 1.0, 1.0, 4.0],
        "is_halted": [False, False, True, False]})
    out = srv.price_history("AAPL", timeframe="raw")
    assert out["timeframe"] == "raw" and out["count"] == 3
    assert all(r["symbol"] == "AAPL" for r in out["rows"])
    ts = [r["ts_utc"] for r in out["rows"]]
    assert ts == sorted(ts, reverse=True)  # newest first
    assert set(out["rows"][0]) == {"ts_utc", "symbol", "bid_raw",
                                   "ask_raw", "mid_adjusted",
                                   "daily_volume", "multiplier",
                                   "is_halted"}


def test_price_history_validates_timeframe_and_limit():
    """[T5] bad timeframe names the valid options; limit must be
    positive."""
    with pytest.raises(ValueError, match="unknown timeframe") as e:
        srv.price_history("AAPL", timeframe="weekly")
    assert "daily" in str(e.value) and "raw" in str(e.value)
    with pytest.raises(ValueError, match="limit must be positive"):
        srv.price_history("AAPL", limit=0)


def test_price_history_absent_symbol_is_empty_not_an_error():
    """File exists but the symbol was never recorded -> honest empty
    answer (recorder writes the whole universe; assets drift), NOT the
    recorder-not-enabled degradation."""
    _write_history_parquet("daily.parquet", {
        "symbol": ["MSFT", "MSFT"], "date": ["2026-09-02", "2026-09-03"],
        "open": [200.0, 201.0], "high": [200.5, 201.5],
        "low": [199.5, 200.5], "close": [200.2, 201.2],
        "volume": [2000.0, 2100.0]})
    out = srv.price_history("AAPL")
    assert "error" not in out
    assert out["count"] == 0 and out["rows"] == []
    assert out["symbol"] == "AAPL"


def test_price_history_blank_symbol_raises():
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="symbol required"):
            srv.price_history(bad)


def test_price_history_daily_limit_capped_at_200():
    """limit=10000 on daily still returns at most 200 rows."""
    n = 250
    _write_history_parquet("daily.parquet", {
        "symbol": ["AAPL"] * n,
        "date": [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}"
                 for i in range(n)],
        "open": [100.0] * n, "high": [100.5] * n, "low": [99.5] * n,
        "close": [100.2] * n, "volume": [1000.0] * n})
    out = srv.price_history("AAPL", limit=10000)
    assert out["count"] == 200 and len(out["rows"]) == 200
