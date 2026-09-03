"""Live-capture regressions (final smoke 2026-09-03, MEC-54).

Two defects seen on the LIVE API and pinned here so they never return:
1. /corporate-actions `effectiveTime` arrives as a protobuf Timestamp
   object ({"year": 2026, "month": 8, "day": 13}) in MCP JSON output —
   must be normalized to an ISO date string, not passed through raw.
2. quote() used to ALWAYS fetch corporate-actions (an extra REST call per
   quote); it must only do so when pendingMultiplier is non-empty.
"""
import json

import pytest

from arcus_mcp import server


def _load_assets():
    with open("tests/fixtures/assets.json") as f:
        return json.load(f)["assets"]


@pytest.fixture
def live_aapl(monkeypatch):
    assets = _load_assets()
    aapl = next(a for a in assets if a["tokenSymbol"] == "AAPL")
    monkeypatch.setattr(server.api, "assets", lambda: assets)

    def fake_asset(sym):
        s = (sym or "").strip().upper()
        return next((a for a in assets
                     if a["tokenSymbol"].upper() == s), None)

    monkeypatch.setattr(server.api, "asset", fake_asset)

    def fake_prices(sym):
        return {"tokenSymbol": sym, "bid": "327.78", "ask": "327.87",
                "dailyTradingVolume": "36479731", "isTradingHalt": False,
                "generatedAt": "2026-09-03T20:21:20.546707549Z"}

    monkeypatch.setattr(server.api, "prices", fake_prices)
    return assets, aapl


def test_protobuf_timestamp_normalized(live_aapl, monkeypatch):
    """effectiveTime as protobuf dict -> ISO date string in warnings."""
    assets, aapl = live_aapl
    aapl["pendingMultiplier"] = "4.000000000000000000"
    monkeypatch.setattr(
        server.api, "corporate_actions",
        lambda sym=None, limit=10: [{
            "tokenSymbol": "AAPL", "kind": "FORWARD_SPLIT",
            "effectiveTime": {"year": 2026, "month": 11, "day": 6},
        }])
    try:
        detail = server.token_detail("AAPL")
        assert any("2026-11-06" in w for w in detail["warnings"]), detail[
            "warnings"]
        # and through the multiplier block the time is a string, never a dict
        assert isinstance(detail["multiplier"]["effective_time"], str)
    finally:
        aapl["pendingMultiplier"] = ""


def test_quote_skips_corporate_actions_when_no_pending(live_aapl,
                                                       monkeypatch):
    """No pendingMultiplier -> zero corporate-actions fetches."""
    calls = {"n": 0}

    def boom(sym=None, limit=10):
        calls["n"] += 1
        return []

    monkeypatch.setattr(server.api, "corporate_actions", boom)
    q = server.quote("AAPL")
    assert calls["n"] == 0, "quote() fetched corporate-actions needlessly"
    assert q["multiplier"]["pending"] is None
    # effective_time must stay None when nothing is pending (the live bug
    # showed a dividend date on a no-pending quote)
    assert q["multiplier"]["effective_time"] is None


def test_quote_fetches_effective_time_when_pending(live_aapl, monkeypatch):
    """pendingMultiplier set -> effective_time resolved from actions."""
    assets, aapl = live_aapl
    aapl["pendingMultiplier"] = "4.000000000000000000"
    monkeypatch.setattr(
        server.api, "corporate_actions",
        lambda sym=None, limit=10: [{
            "tokenSymbol": "AAPL", "kind": "FORWARD_SPLIT",
            "effectiveTime": {"year": 2026, "month": 11, "day": 6},
        }])
    try:
        q = server.quote("AAPL")
        assert q["multiplier"]["pending"] == 4.0
        assert q["multiplier"]["effective_time"] == "2026-11-06"
    finally:
        aapl["pendingMultiplier"] = ""
