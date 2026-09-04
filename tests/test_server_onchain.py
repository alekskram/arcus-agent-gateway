"""Offline tests for the v0.2 on-chain server tools - 100% mocked.

The server imports its clients as module namespaces (`from . import
rpc` / `from . import explorer`), so every test monkeypatches the
FUNCTION on the module object the server sees - same pattern as
tests/test_server.py uses for `api`. No socket may open: api.get and
the raw client transports are unreachable.

Covered, per the acceptance contract:

  * onchain_info: per-field "source" tags, v0.1 fields intact,
    supply_crosscheck math + the >1% divergence warning, independent
    degradation of the rpc/explorer sources (field omitted + warning,
    never silent)
  * holder_snapshot: share_pct == value / total_supply * 100 invariant,
    50-row cap, explorer-supply fallback when the RPC fails, share None
    when no supply at all, error dicts with kind + hint
  * wallet_holdings: assets() universe intersection (only tokenized
    equities survive), cache-only USD estimates (fresh quotes only),
    portfolio_usd_total == sum of estimates, unpriced note, 120s cache
  * transfer_history: row normalization (ts from blockTimestamp, from/to
    from topics, value == int(data, 16) / 10**18, block int), min_value
    filter + filtered-count note, honest empty-window note pointing at
    the explorer, rpc failure -> error dict, 60s cache
"""
import json
import time
from pathlib import Path

import pytest

import arcus_mcp.api as api
import arcus_mcp.explorer as explorer
import arcus_mcp.rpc as rpc
import arcus_mcp.server as srv

FIXTURES = Path(__file__).parent / "fixtures"

ASSETS = json.loads((FIXTURES / "assets.json").read_text())
ASSETS = ASSETS.get("assets", ASSETS) if isinstance(ASSETS, dict) else ASSETS
AAPL_ASSET = next(a for a in ASSETS if a["tokenSymbol"] == "AAPL")
AAPL_QUOTE = json.loads(
    (FIXTURES / "prices_AAPL.json").read_text())["quotes"][0]
EXPLORER_TOKEN = json.loads((FIXTURES / "explorer_token.json").read_text())
HOLDERS_PAGE = {
    "items": [
        {"address": "0x8366a39CC670B4001A1121B8F6A443A643e40951",
         "is_contract": True, "value": "6155456228534021317261",
         "token_id": None},
        {"address": "0x498752D5fa0600CBd613074C151Abe15B3FeC7CB",
         "is_contract": False, "value": "1000000000000000000",
         "token_id": None},
    ],
    "next_page_params": {"holder_address": "0xabc", "items_count": 50},
}
HOLDERS_PAGE_LAST = {"items": HOLDERS_PAGE["items"],
                     "next_page_params": None}
BALANCES = json.loads(
    (FIXTURES / "explorer_token_balances.json").read_text())
LOGS = json.loads((FIXTURES / "rpc_transfer_logs.json").read_text())

CONTRACT = "0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9"
EOA = "0x8366a39CC670B4001A1121B8F6A443A643e40951"
AAPL_SUPPLY = int(EXPLORER_TOKEN["total_supply"]) / 10 ** 18  # 16871.85...
AAPL_MID = (float(AAPL_QUOTE["bid"]) + float(AAPL_QUOTE["ask"])) / 2
AAPL_MULT = float(AAPL_ASSET["currentMultiplier"])


def _norm(s: str) -> str:
    return (s or "").strip().upper()


def _supply_hex(tokens: float) -> str:
    """Hex-encoded raw 18-dec units for a fake totalSupply() result."""
    return hex(int(tokens * 10 ** 18))


@pytest.fixture
def mock_api(monkeypatch):
    """Replace the api surface the on-chain tools touch; seed a FRESH
    price cache (timestamps = now, so cache-only pricing sees them)."""
    def _no_get(*a, **k):  # pragma: no cover - only on a bug
        raise RuntimeError("live HTTP attempted in offline test!")

    monkeypatch.setattr(api, "assets", lambda: ASSETS)
    monkeypatch.setattr(api, "asset", lambda symbol: next(
        (a for a in ASSETS
         if a.get("tokenSymbol", "").upper() == _norm(symbol)), None))
    monkeypatch.setattr(api, "prices",
                        lambda symbol: AAPL_QUOTE
                        if _norm(symbol) == "AAPL" else None)
    monkeypatch.setattr(api, "get", _no_get)
    monkeypatch.setattr(api, "_PRICES_CACHE",
                        {"AAPL": (time.monotonic(), AAPL_QUOTE)})
    # tool-level result caches start empty in every test
    monkeypatch.setattr(srv, "_HOLDINGS_CACHE", {})
    monkeypatch.setattr(srv, "_TRANSFERS_CACHE", {})
    return api


@pytest.fixture
def mock_onchain(mock_api, monkeypatch):
    """Healthy rpc + explorer: fixture-backed, counting every call."""
    calls = {"rpc_call": 0, "explorer_token": 0, "explorer_holders": 0,
             "explorer_balances": 0, "rpc_get_transfers": 0}

    def fake_call(to, data, block="latest"):
        calls["rpc_call"] += 1
        return _supply_hex(AAPL_SUPPLY)

    def fake_token(address):
        calls["explorer_token"] += 1
        return EXPLORER_TOKEN

    def fake_holders(address, limit=50):
        calls["explorer_holders"] += 1
        return {"items": HOLDERS_PAGE["items"][:max(1, min(50, limit))],
                "next_page_params": HOLDERS_PAGE["next_page_params"]}

    def fake_balances(address):
        calls["explorer_balances"] += 1
        return BALANCES

    def fake_transfers(contract, from_block, to_block=None):
        calls["rpc_get_transfers"] += 1
        return {"logs": list(LOGS), "window": {
            "from_block": 0, "to_block": 54405595, "requests": 3,
            "widths_used": [48, 32, 16], "final_width": 16,
            "oldest_covered": 54405548, "newest_covered": 54405595,
            "stopped_reason": "window-closed",
            "note": "window closed"}}

    monkeypatch.setattr(srv.rpc, "call", fake_call)
    monkeypatch.setattr(srv.rpc, "get_transfers", fake_transfers)
    monkeypatch.setattr(srv.explorer, "token", fake_token)
    monkeypatch.setattr(srv.explorer, "token_holders", fake_holders)
    monkeypatch.setattr(srv.explorer, "address_token_balances",
                        fake_balances)
    return calls


# ------------------------------------------------------------- onchain_info


def test_onchain_info_rest_fields_and_source_tags(mock_onchain):
    oc = srv.onchain_info("AAPL")
    assert oc["symbol"] == "AAPL"
    assert oc["contract"] == CONTRACT
    assert oc["chain_id"] == 4663
    assert oc["network"] == "Robinhood Chain"
    assert oc["decimals"] == 18
    assert oc["isin"] == AAPL_ASSET["isin"]
    # derived fields carry per-field source tags
    assert oc["total_supply"] == pytest.approx(AAPL_SUPPLY)
    assert oc["total_supply_source"] == "rpc"
    assert oc["holders_count"] == int(EXPLORER_TOKEN["holders_count"])
    assert oc["holders_count_source"] == "explorer"
    assert oc["circulating_market_cap"] == pytest.approx(
        float(EXPLORER_TOKEN["circulating_market_cap"]))
    assert oc["circulating_market_cap_source"] == "explorer"
    # both sources healthy: no error-dict warnings; the live fixtures do
    # trigger the >1% supply cross-check note (asserted in its own test)
    assert all(isinstance(w, str) for w in oc["warnings"])


def test_onchain_info_crosscheck_divergence_warning(mock_onchain,
                                                    monkeypatch):
    """Fixture supply x fixture price diverges >1% from the explorer cap:
    both numbers returned, warning string in warnings[]."""
    oc = srv.onchain_info("AAPL")
    cc = oc["supply_crosscheck"]
    rest_implied = AAPL_SUPPLY * AAPL_MULT * AAPL_MID
    assert cc["checked"] is True
    assert cc["rest_implied_usd"] == pytest.approx(rest_implied, abs=0.01)
    assert cc["explorer_implied_usd"] == pytest.approx(
        float(EXPLORER_TOKEN["circulating_market_cap"]), abs=0.01)
    assert cc["divergence_pct"] > 1.0  # 13.74% on the live fixtures
    assert any(isinstance(w, str) and "supply cross-check" in w
               for w in oc["warnings"])


def test_onchain_info_crosscheck_passes_under_one_percent(
        mock_onchain, monkeypatch):
    """RPC supply picked so REST-implied == explorer cap: checked, no
    divergence warning."""
    target = (float(EXPLORER_TOKEN["circulating_market_cap"])
              / (AAPL_MULT * AAPL_MID))
    monkeypatch.setattr(srv.rpc, "call",
                        lambda to, data, block="latest":
                        _supply_hex(target))
    oc = srv.onchain_info("AAPL")
    cc = oc["supply_crosscheck"]
    assert cc["checked"] is True
    assert cc["divergence_pct"] < 1.0
    assert not any(isinstance(w, str) and "supply cross-check" in w
                   for w in oc["warnings"])


def test_onchain_info_rpc_down_degrades_to_warning(mock_onchain,
                                                   monkeypatch):
    def boom(to, data, block="latest"):
        raise rpc.RpcError("rpc 403 (after 1 attempt(s))",
                           kind="rpc-http", status=403)
    monkeypatch.setattr(srv.rpc, "call", boom)
    oc = srv.onchain_info("AAPL")
    assert "total_supply" not in oc  # field omitted, not faked
    assert "total_supply_source" not in oc
    kinds = [w.get("kind") for w in oc["warnings"] if isinstance(w, dict)]
    assert "rpc" in kinds
    # explorer fields survive independently
    assert oc["holders_count"] == int(EXPLORER_TOKEN["holders_count"])
    assert oc["supply_crosscheck"]["checked"] is False


def test_onchain_info_explorer_down_degrades_to_warning(mock_onchain,
                                                        monkeypatch):
    def boom(address):
        raise explorer.ExplorerError(
            "cloudflare", "explorer Cloudflare challenge", status=403,
            hint="browser User-Agent required")
    monkeypatch.setattr(srv.explorer, "token", boom)
    oc = srv.onchain_info("AAPL")
    assert "holders_count" not in oc
    assert "circulating_market_cap" not in oc
    kinds = [w.get("kind") for w in oc["warnings"] if isinstance(w, dict)]
    assert "explorer" in kinds
    # rpc fields survive independently
    assert oc["total_supply"] == pytest.approx(AAPL_SUPPLY)
    # one-sided crosscheck still reports the REST-implied number
    assert oc["supply_crosscheck"]["rest_implied_usd"] is not None


def test_onchain_info_all_sources_down(mock_onchain, monkeypatch):
    def rpc_boom(to, data, block="latest"):
        raise rpc.RpcError("rpc timeout", kind="rpc-timeout")
    def expl_boom(address):
        raise explorer.ExplorerError(
            "explorer-timeout", "timed out", hint="try an EOA")
    monkeypatch.setattr(srv.rpc, "call", rpc_boom)
    monkeypatch.setattr(srv.explorer, "token", expl_boom)
    oc = srv.onchain_info("AAPL")
    # REST metadata is still there - it is an independent source
    assert oc["contract"] == CONTRACT and oc["isin"]
    kinds = [w.get("kind") for w in oc["warnings"] if isinstance(w, dict)]
    assert "rpc" in kinds and "explorer" in kinds


def test_onchain_info_explorer_row_missing_fields_warns(
        mock_onchain, monkeypatch):
    stripped = {k: v for k, v in EXPLORER_TOKEN.items()
                if k not in ("holders_count", "circulating_market_cap")}
    monkeypatch.setattr(srv.explorer, "token", lambda a: stripped)
    oc = srv.onchain_info("AAPL")
    assert "holders_count" not in oc
    assert "circulating_market_cap" not in oc
    assert any("holders_count" in str(w) for w in oc["warnings"])


def test_onchain_info_unknown_symbol(mock_api):
    with pytest.raises(ValueError, match="token_list"):
        srv.onchain_info("NOPE")


# --------------------------------------------------------- holder_snapshot


def test_holder_snapshot_rows_and_share_invariant(mock_onchain):
    out = srv.holder_snapshot("AAPL", limit=10)
    assert out["symbol"] == "AAPL"
    assert out["contract"] == CONTRACT
    assert out["count"] == 2
    assert out["total_supply"] == pytest.approx(AAPL_SUPPLY, abs=1e-6)
    assert out["total_supply_source"] == "rpc"
    top = out["holders"][0]
    assert top["address"] == EOA
    assert top["is_contract"] is True
    # invariant: value == raw / 1e18 and share == value / supply * 100
    raw_value = int(HOLDERS_PAGE["items"][0]["value"]) / 10 ** 18
    assert top["value"] == pytest.approx(raw_value, abs=1e-6)
    assert top["share_pct"] == pytest.approx(
        raw_value / AAPL_SUPPLY * 100, abs=1e-6)
    second = out["holders"][1]
    assert second["share_pct"] == pytest.approx(
        1.0 / AAPL_SUPPLY * 100, abs=1e-6)  # the 1-token EOA row
    # more holders exist beyond the page -> honest note
    assert out.get("next_page") is True
    assert "50" in out.get("note", "")


def test_holder_snapshot_limit_cap(mock_onchain):
    out = srv.holder_snapshot("AAPL", limit=500)
    assert out["count"] <= 50  # hard page cap, never a pagination loop


def test_holder_snapshot_rpc_supply_fallback_to_explorer(
        mock_onchain, monkeypatch):
    def boom(to, data, block="latest"):
        raise rpc.RpcError("rpc unreachable", kind="rpc-network")
    monkeypatch.setattr(srv.rpc, "call", boom)
    out = srv.holder_snapshot("AAPL")
    assert out["total_supply_source"] == "explorer"
    assert out["total_supply"] == pytest.approx(AAPL_SUPPLY, abs=1e-6)
    kinds = [w.get("kind") for w in out.get("warnings", [])
             if isinstance(w, dict)]
    assert "rpc" in kinds  # the fallback is announced, not silent
    top = out["holders"][0]
    assert top["share_pct"] == pytest.approx(
        int(HOLDERS_PAGE["items"][0]["value"]) / 10 ** 18
        / AAPL_SUPPLY * 100, abs=1e-6)


def test_holder_snapshot_no_supply_shares_null(mock_onchain, monkeypatch):
    def rpc_boom(to, data, block="latest"):
        raise rpc.RpcError("rpc unreachable", kind="rpc-network")
    def expl_boom(address):
        raise explorer.ExplorerError("explorer-network", "down")
    monkeypatch.setattr(srv.rpc, "call", rpc_boom)
    monkeypatch.setattr(srv.explorer, "token", expl_boom)
    out = srv.holder_snapshot("AAPL")
    assert out["total_supply"] is None
    assert out["total_supply_source"] == "none"
    assert all(h["share_pct"] is None for h in out["holders"])
    assert out["holders"][0]["value"] is not None  # holders still fresh
    assert any("share_pct" in str(w) for w in out["warnings"])


def test_holder_snapshot_explorer_error_dict(mock_onchain, monkeypatch):
    def boom(address, limit=50):
        raise explorer.ExplorerError(
            "cloudflare", "explorer Cloudflare challenge for holders",
            status=403, hint="browser User-Agent required")
    monkeypatch.setattr(srv.explorer, "token_holders", boom)
    out = srv.holder_snapshot("AAPL")
    err = out["error"]
    assert err["kind"] == "cloudflare"
    assert err["hint"]
    assert "holders" not in out  # never a silent empty list


def test_holder_snapshot_unknown_token_404(mock_onchain, monkeypatch):
    monkeypatch.setattr(srv.explorer, "token_holders",
                        lambda a, limit=50: None)
    out = srv.holder_snapshot("AAPL")
    assert out["error"]["kind"] == "explorer"
    assert "404" in out["error"]["message"] or "unknown" in (
        out["error"]["message"])


def test_holder_snapshot_unknown_symbol(mock_api):
    with pytest.raises(ValueError, match="token_list"):
        srv.holder_snapshot("NOPE")


# --------------------------------------------------------- wallet_holdings


def test_wallet_holdings_universe_intersect_and_estimate(mock_onchain):
    out = srv.wallet_holdings(EOA)
    # the fixture wallet holds 86 tokens; ONE is a tokenized equity
    assert out["count"] == 1
    row = out["holdings"][0]
    assert row["symbol"] == "AAPL"
    raw_value = int(BALANCES[0]["value"]) / 10 ** 18
    assert row["value"] == pytest.approx(raw_value, abs=1e-6)
    # cache-only pricing from the fresh seeded quote
    expected_est = round(raw_value * AAPL_MID * AAPL_MULT, 2)
    assert row["est_position_usd"] == pytest.approx(expected_est, abs=0.01)
    assert out["portfolio_usd_total"] == pytest.approx(expected_est,
                                                       abs=0.01)
    assert out["priced_positions"] == 1
    assert "note" not in out  # everything priced: no stale-quote note


def test_wallet_holdings_stale_quote_note(mock_onchain, monkeypatch):
    # same wallet, but the cached quote is older than the price TTL:
    # estimates must be omitted with a note, not faked
    monkeypatch.setattr(api, "_PRICES_CACHE",
                        {"AAPL": (time.monotonic() - 999.0, AAPL_QUOTE)})
    out = srv.wallet_holdings(EOA)
    row = out["holdings"][0]
    assert row["est_position_usd"] is None
    assert row["value"] is not None  # the holding itself is still there
    assert out["portfolio_usd_total"] is None
    assert out["priced_positions"] == 0
    assert "no FRESH cached quote" in out["note"]


def test_wallet_holdings_no_quotes_at_all(mock_onchain, monkeypatch):
    monkeypatch.setattr(api, "_PRICES_CACHE", {})
    out = srv.wallet_holdings(EOA)
    assert out["holdings"][0]["est_position_usd"] is None
    assert out["portfolio_usd_total"] is None
    assert "note" in out


def test_wallet_holdings_bad_address(mock_api):
    with pytest.raises(ValueError, match="not a wallet address"):
        srv.wallet_holdings("0x123")  # too short
    with pytest.raises(ValueError):
        srv.wallet_holdings("")


def test_wallet_holdings_explorer_error_dict(mock_onchain, monkeypatch):
    def boom(address):
        raise explorer.ExplorerError(
            "explorer-timeout",
            "explorer timed out after 15s: token-balances",
            hint="token-balances on contract addresses can hang 40s+")
    monkeypatch.setattr(srv.explorer, "address_token_balances", boom)
    out = srv.wallet_holdings(EOA)
    assert out["error"]["kind"] == "explorer-timeout"
    assert out["error"]["hint"]
    assert "holdings" not in out


def test_wallet_holdings_unknown_address_404(mock_onchain, monkeypatch):
    monkeypatch.setattr(srv.explorer, "address_token_balances",
                        lambda a: None)
    out = srv.wallet_holdings(EOA)
    assert out["error"]["kind"] == "explorer"


def test_wallet_holdings_cache_120s(mock_onchain):
    srv.wallet_holdings(EOA)
    assert mock_onchain["explorer_balances"] == 1
    srv.wallet_holdings(EOA)  # fresh: served from the tool cache
    assert mock_onchain["explorer_balances"] == 1
    # expire the entry (TTL is 120s) -> refetch
    key = EOA.lower()
    ts, payload = srv._HOLDINGS_CACHE[key]
    srv._HOLDINGS_CACHE[key] = (ts - 121.0, payload)
    srv.wallet_holdings(EOA)
    assert mock_onchain["explorer_balances"] == 2


# -------------------------------------------------------- transfer_history


def test_transfer_history_row_normalization(mock_onchain):
    out = srv.transfer_history("AAPL", limit=25)
    assert out["symbol"] == "AAPL"
    assert out["contract"] == CONTRACT
    assert out["count"] == 1
    row = out["transfers"][0]
    log = LOGS[0]
    # ts is ISO-8601 derived from the log's OWN blockTimestamp
    assert row["ts"] == time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(log["blockTimestamp"], 16)))
    # from/to unpacked from the indexed topics (0x + last 40 hex chars)
    assert row["from"] == "0x" + log["topics"][1][-40:]
    assert row["to"] == "0x" + log["topics"][2][-40:]
    # value invariant: int(data, 16) / 10**18 (server-rounded to 6 dp)
    assert row["value"] == pytest.approx(
        round(int(log["data"], 16) / 10 ** 18, 6), abs=1e-12)
    assert row["tx_hash"] == log["transactionHash"]
    assert row["block"] == int(log["blockNumber"], 16)
    # window metadata is passed through for honest degradation
    assert out["window"]["stopped_reason"] == "window-closed"
    assert out["window"]["requests"] == 3


def test_transfer_history_newest_first_and_limit(mock_onchain,
                                                 monkeypatch):
    older = dict(LOGS[0], blockNumber=hex(int(LOGS[0]["blockNumber"], 16)
                                          - 1),
                 transactionHash="0x" + "01" * 32)
    newer = dict(LOGS[0], blockNumber=hex(int(LOGS[0]["blockNumber"], 16)
                                          + 1),
                 transactionHash="0x" + "02" * 32)
    monkeypatch.setattr(srv.rpc, "get_transfers",
                        lambda c, f, t=None: {"logs": [LOGS[0], older,
                                                       newer],
                                              "window": {
                                                  "stopped_reason":
                                                  "complete"}})
    out = srv.transfer_history("AAPL", limit=2)
    assert out["count"] == 2
    blocks = [r["block"] for r in out["transfers"]]
    assert blocks == sorted(blocks, reverse=True)  # newest first


def test_transfer_history_min_value_filter(mock_onchain):
    # fixture transfer is ~0.0574 tokens: a 1.0 floor filters it out
    out = srv.transfer_history("AAPL", min_value=1.0)
    assert out["count"] == 0
    assert "filtered out" in out["note"]
    # and a floor below it keeps it
    keep = srv.transfer_history("AAPL", min_value=0.01)
    assert keep["count"] == 1
    assert "filtered out" not in keep.get("note", "")


def test_transfer_history_window_silence_note(mock_onchain, monkeypatch):
    """0 logs + window closed: the note must point at the explorer
    instead of pretending the token has no activity."""
    monkeypatch.setattr(srv.rpc, "get_transfers",
                        lambda c, f, t=None: {"logs": [], "window": {
                            "stopped_reason": "window-closed",
                            "requests": 4}})
    out = srv.transfer_history("AAPL")
    assert out["count"] == 0
    assert "on-chain window reached" in out["note"]
    assert "explorer" in out["note"]


def test_transfer_history_empty_but_complete_note(mock_onchain,
                                                  monkeypatch):
    monkeypatch.setattr(srv.rpc, "get_transfers",
                        lambda c, f, t=None: {"logs": [], "window": {
                            "stopped_reason": "complete", "requests": 1}})
    out = srv.transfer_history("AAPL")
    assert out["count"] == 0
    assert "no Transfer logs" in out["note"]


def test_transfer_history_rpc_error_dict(mock_onchain, monkeypatch):
    def boom(contract, from_block, to_block=None):
        raise rpc.RpcError("rpc 429 (after 3 attempt(s))",
                           kind="rpc-limit", status=429)
    monkeypatch.setattr(srv.rpc, "get_transfers", boom)
    out = srv.transfer_history("AAPL")
    assert out["error"]["kind"] == "rpc-limit"
    assert out["error"]["hint"]
    assert "transfers" not in out


def test_transfer_history_cache_60s(mock_onchain):
    srv.transfer_history("AAPL")
    assert mock_onchain["rpc_get_transfers"] == 1
    srv.transfer_history("AAPL", min_value=0.001)  # different cache key
    assert mock_onchain["rpc_get_transfers"] == 2
    srv.transfer_history("AAPL")  # fresh: cached
    assert mock_onchain["rpc_get_transfers"] == 2
    ck = next(iter(srv._TRANSFERS_CACHE))
    ts, payload = srv._TRANSFERS_CACHE[ck]
    srv._TRANSFERS_CACHE[ck] = (ts - 61.0, payload)  # expire (TTL 60s)
    srv.transfer_history("AAPL")
    assert mock_onchain["rpc_get_transfers"] == 3


def test_transfer_history_unknown_symbol(mock_api):
    with pytest.raises(ValueError, match="token_list"):
        srv.transfer_history("NOPE")
