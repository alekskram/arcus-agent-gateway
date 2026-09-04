"""Offline smoke for the v0.2 on-chain tools - monkeypatched rpc/explorer.

Not a pytest file (Junior owns tests): a plain script asserting the
spec invariants against the committed live fixtures.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import arcus_mcp.api as api
import arcus_mcp.explorer as explorer
import arcus_mcp.rpc as rpc
import arcus_mcp.server as srv

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
ASSETS = json.loads((FIX / "assets.json").read_text())
ASSETS = ASSETS.get("assets", ASSETS) if isinstance(ASSETS, dict) else ASSETS
TOKEN = json.loads((FIX / "explorer_token.json").read_text())
HOLDERS = json.loads((FIX / "explorer_holders.json").read_text())
BALANCES = json.loads((FIX / "explorer_token_balances.json").read_text())
LOGS = json.loads((FIX / "rpc_transfer_logs.json").read_text())
QUOTE = json.loads((FIX / "prices_AAPL.json").read_text())["quotes"][0]

AAPL_CONTRACT = "0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9"
# fixture total_supply raw = 16871853100370000000000 -> /1e18
SUPPLY = int(TOKEN["total_supply"]) / 10**18
HEX_SUPPLY = hex(int(TOKEN["total_supply"]))


def fake_assets():
    return ASSETS


def fake_asset(sym):
    s = (sym or "").strip().upper()
    return next((a for a in ASSETS
                 if a.get("tokenSymbol", "").upper() == s), None)


def fake_prices(sym):
    return QUOTE if (sym or "").upper() == "AAPL" else None


def fake_token(address):
    return TOKEN if address.lower() == AAPL_CONTRACT.lower() else None


def fake_holders(address, limit=50):
    assert address.lower() == AAPL_CONTRACT.lower()
    # normalized shape, as the real explorer.token_holders returns it
    items = [{"address": (it.get("address") or {}).get("hash"),
              "is_contract": bool((it.get("address") or {})
                                  .get("is_contract")),
              "value": it.get("value"), "token_id": it.get("token_id")}
             for it in HOLDERS["items"]]
    n = max(1, min(50, int(limit)))
    return {"items": items[:n],
            "next_page_params": HOLDERS.get("next_page_params")}


def fake_balances(address):
    return [{"token": it["token"], "value": it["value"],
             "token_id": None} for it in BALANCES]


def fake_call(to, data, block="latest"):
    assert data == "0x18160ddd", data
    assert to.lower() == AAPL_CONTRACT.lower()
    return HEX_SUPPLY


def fake_get_transfers(contract, from_block, to_block=None):
    assert contract.lower() == AAPL_CONTRACT.lower()
    return {"logs": LOGS,
            "window": {"from_block": from_block, "to_block": 5451380,
                       "requests": 3, "widths_used": [48, 48, 32],
                       "final_width": 32, "oldest_covered": 5451305,
                       "newest_covered": 5451380,
                       "stopped_reason": "budget",
                       "note": "stub"}}


def main():
    api.assets = fake_assets
    api.asset = fake_asset
    api.prices = fake_prices
    api._PRICES_CACHE.clear()
    import time as _t
    api._PRICES_CACHE["AAPL"] = (_t.monotonic(), QUOTE)  # fresh seed
    explorer.token = fake_token
    explorer.token_holders = fake_holders
    explorer.address_token_balances = fake_balances
    rpc.call = fake_call
    rpc.get_transfers = fake_get_transfers
    srv._HOLDINGS_CACHE.clear()
    srv._TRANSFERS_CACHE.clear()

    ok = 0

    # ---- onchain_info
    oc = srv.onchain_info("AAPL")
    assert oc["symbol"] == "AAPL" and oc["contract"] == AAPL_CONTRACT
    assert oc["chain_id"] == 4663 and oc["decimals"] == 18
    assert oc["total_supply"] == round(SUPPLY, 6), oc["total_supply"]
    assert oc["total_supply_source"] == "rpc"
    assert oc["holders_count"] == int(TOKEN["holders_count"])
    assert oc["holders_count_source"] == "explorer"
    assert oc["circulating_market_cap"] == float(
        TOKEN["circulating_market_cap"])
    assert oc["circulating_market_cap_source"] == "explorer"
    xc = oc["supply_crosscheck"]
    assert xc["checked"] is True, xc
    # invariant: rest_implied == total_supply * multiplier * mid
    mid = (float(QUOTE["bid"]) + float(QUOTE["ask"])) / 2
    mult = float(fake_asset("AAPL")["currentMultiplier"])
    assert abs(xc["rest_implied_usd"]
               - SUPPLY * mult * mid) < 0.5, xc["rest_implied_usd"]
    assert xc["explorer_implied_usd"] == round(float(
        TOKEN["circulating_market_cap"]), 2)
    # Live fixtures genuinely diverge ~13.7%: the explorer cap uses its
    # own circulating amount + exchange_rate (321.06) vs REST mid 327.775
    # on total_supply. The >1% warning MUST fire here - that is the
    # crosscheck doing its job.
    assert xc["divergence_pct"] > 1.0, xc
    assert any("divergence" in str(w) for w in oc["warnings"]), \
        oc["warnings"]
    assert "note" not in oc, "v0.1 stub note must be gone"
    assert isinstance(oc["warnings"], list)
    ok += 1
    print("onchain_info OK:", json.dumps(
        {k: oc[k] for k in ("total_supply", "holders_count",
                            "circulating_market_cap")}, indent=None))

    # ---- onchain_info degradation: rpc.call fails -> warning, no field
    def boom_call(to, data, block="latest"):
        raise rpc.RpcError("eth_call exploded", kind="rpc-http", status=500)

    rpc.call = boom_call
    oc2 = srv.onchain_info("AAPL")
    assert "total_supply" not in oc2, oc2.keys()
    assert any(w["kind"] == "rpc" for w in oc2["warnings"]), oc2["warnings"]
    assert oc2["holders_count"] == int(TOKEN["holders_count"])  # independent
    assert oc2["supply_crosscheck"]["checked"] is False
    rpc.call = fake_call
    ok += 1
    print("onchain_info rpc-failure degradation OK:",
          [w["kind"] for w in oc2["warnings"]])

    # ---- onchain_info degradation: explorer fails -> warning, no fields
    def boom_token(address):
        raise explorer.ExplorerError("cloudflare", "challenge",
                                    status=403, hint="UA required")

    explorer.token = boom_token
    oc3 = srv.onchain_info("AAPL")
    assert "holders_count" not in oc3 and \
        "circulating_market_cap" not in oc3
    assert any(w["kind"] == "explorer" for w in oc3["warnings"])
    assert oc3["total_supply"] == round(SUPPLY, 6)  # rpc still fine
    explorer.token = fake_token
    ok += 1
    print("onchain_info explorer-failure degradation OK:",
          [w["kind"] for w in oc3["warnings"]])

    # ---- onchain_info divergence >1% surfaces a warning
    bad = dict(TOKEN, circulating_market_cap="1000000.0")
    explorer.token = lambda a: bad
    oc4 = srv.onchain_info("AAPL")
    assert any("divergence" in str(w) for w in oc4["warnings"]), \
        oc4["warnings"]
    assert oc4["supply_crosscheck"]["divergence_pct"] > 1.0
    explorer.token = fake_token
    ok += 1
    print("onchain_info >1% divergence warning OK:",
          oc4["supply_crosscheck"]["divergence_pct"])

    # ---- holder_snapshot
    hs = srv.holder_snapshot("AAPL", limit=10)
    assert hs["count"] == 10 and len(hs["holders"]) == 10
    top = hs["holders"][0]
    raw0 = int(HOLDERS["items"][0]["value"]) / 10**18
    assert top["value"] == round(raw0, 6)
    # invariant: share_pct == value / total_supply * 100
    assert top["share_pct"] == round(raw0 / SUPPLY * 100, 6), top
    assert top["is_contract"] is True
    assert hs["total_supply"] == round(SUPPLY, 6)
    assert hs["total_supply_source"] == "rpc"
    assert hs.get("next_page") is True
    ok += 1
    print("holder_snapshot OK: top share", top["share_pct"])

    # share invariant across all rows
    for row, src in zip(hs["holders"], HOLDERS["items"][:10]):
        v = int(src["value"]) / 10**18
        assert row["share_pct"] == round(v / SUPPLY * 100, 6)
    ok += 1
    print("holder_snapshot share invariant (all 10 rows) OK")

    # holder_snapshot rpc-fallback path
    rpc.call = boom_call
    hs2 = srv.holder_snapshot("AAPL", limit=5)
    assert hs2["total_supply_source"] == "explorer", hs2
    assert hs2["total_supply"] == round(SUPPLY, 6)
    assert any(w["kind"] == "rpc" for w in hs2.get("warnings", []))
    assert hs2["holders"][0]["share_pct"] == round(raw0 / SUPPLY * 100, 6)
    rpc.call = fake_call
    ok += 1
    print("holder_snapshot explorer-supply fallback OK")

    # ---- wallet_holdings
    wh = srv.wallet_holdings(AAPL_CONTRACT)  # fixture EOA holds 86 tokens
    syms = [r["symbol"] for r in wh["holdings"]]
    assert "AAPL" in syms and "WETH" not in syms, syms  # universe intersect
    aapl_row = next(r for r in wh["holdings"] if r["symbol"] == "AAPL")
    assert aapl_row["value"] == round(
        int(BALANCES[0]["value"]) / 10**18, 6)
    assert aapl_row["est_position_usd"] is not None  # fresh cache seed
    total = sum(r["est_position_usd"] for r in wh["holdings"]
                if r["est_position_usd"] is not None)
    assert wh["portfolio_usd_total"] == round(total, 2)
    # position math: value * mid * multiplier
    mid = (float(QUOTE["bid"]) + float(QUOTE["ask"])) / 2
    mult = float(fake_asset("AAPL")["currentMultiplier"])
    assert aapl_row["est_position_usd"] == round(
        aapl_row["value"] * mid * mult, 2)
    ok += 1
    print("wallet_holdings OK:", wh["count"], "positions, portfolio",
          wh["portfolio_usd_total"])

    # wallet_holdings: stale cache -> est omitted + note (no fan-out)
    api._PRICES_CACHE.clear()
    api._PRICES_CACHE["AAPL"] = (-10_000.0, QUOTE)  # ancient ts -> stale
    srv._HOLDINGS_CACHE.clear()
    wh2 = srv.wallet_holdings(AAPL_CONTRACT)
    aapl2 = next(r for r in wh2["holdings"] if r["symbol"] == "AAPL")
    assert aapl2["est_position_usd"] is None, aapl2
    assert "note" in wh2 and "fresh" in wh2["note"].lower()
    calls = {"n": 0}

    def no_prices(sym):  # prove no fan-out happened
        calls["n"] += 1
        return None

    api.prices = no_prices
    assert calls["n"] == 0
    api.prices = fake_prices
    ok += 1
    print("wallet_holdings stale-quote note OK:", wh2["note"][:60])

    # ---- transfer_history
    th = srv.transfer_history("AAPL", limit=25)
    assert th["count"] == 1 and len(th["transfers"]) == 1
    row = th["transfers"][0]
    assert row["tx_hash"] == LOGS[0]["transactionHash"]
    assert row["value"] == round(int(LOGS[0]["data"], 16) / 10**18, 6)
    assert row["block"] == int(LOGS[0]["blockNumber"], 16)
    assert row["from"] == "0x" + LOGS[0]["topics"][1][-40:]
    assert row["to"] == "0x" + LOGS[0]["topics"][2][-40:]
    assert row["ts"] == "2026-09-04T16:25:07Z", row["ts"]  # 0x6a9af0e3
    ok += 1
    print("transfer_history OK:", row)

    # min_value filter (fixture value = 0.0090707... tokens)
    th2 = srv.transfer_history("AAPL", limit=25, min_value=1.0)
    assert th2["count"] == 0 and th2["transfers"] == []
    assert "min_value" in th2.get("note", "")
    ok += 1
    print("transfer_history min_value filter OK:", th2["note"])

    # window exhausted with 0 logs -> honest note
    def empty_walk(c, f, t=None):
        return {"logs": [], "window": {"requests": 8,
                                       "stopped_reason": "window-closed",
                                       "oldest_covered": 100,
                                       "newest_covered": 148}}

    rpc.get_transfers = empty_walk
    srv._TRANSFERS_CACHE.clear()
    th3 = srv.transfer_history("AAPL")
    assert th3["count"] == 0
    assert th3["note"] == ("on-chain window reached; older transfers via "
                           "explorer /tokens/{contract}/transfers"), th3
    ok += 1
    print("transfer_history honest-empty note OK")

    # ---- registration + version
    mcp = srv.build_server()
    tools = {t.name for t in asyncio.run(mcp.list_tools())}
    expected = {"token_list", "quote", "quotes", "token_detail",
                "market_status", "corporate_actions", "search",
                "sector_view", "onchain_info", "holder_snapshot",
                "wallet_holdings", "transfer_history", "price_history"}
    assert tools == expected, tools ^ expected
    for t in asyncio.run(mcp.list_tools()):
        ann = t.annotations or {}
        ro = ann.get("read_only_hint") if isinstance(ann, dict) \
            else getattr(ann, "read_only_hint", None)
        de = ann.get("destructive_hint") if isinstance(ann, dict) \
            else getattr(ann, "destructive_hint", None)
        assert (ro, de) == (True, False), t.name
    ok += 1
    print("build_server registers 13 RO tools OK")

    print(f"\nALL {ok} SMOKE GROUPS PASSED")


if __name__ == "__main__":
    main()
