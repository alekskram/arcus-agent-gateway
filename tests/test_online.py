"""Short LIVE sanity checks of the rhj REST + on-chain layer (quote,
token list, corporate actions, holders, transfers, price-history
degradation, market status). They hit real endpoints, so they are
opt-in and never run in CI; every test degrades honestly (skip with a
reason on a reachable, well-formed error) instead of failing."""
# Run: .venv/bin/python -m pytest tests/test_online.py -m online -v
# Deselected by default (pyproject addopts "-m 'not online'") and CI
# (.github/workflows/tests.yml runs plain pytest) never collects them.

import pytest

from arcus_mcp.server import (corporate_actions, holder_snapshot,
                              market_status, price_history, quote,
                              token_list, transfer_history)


def _skip_on_error_dict(result: dict, tool: str) -> None:
    """Skip (never fail) when a tool returned its well-formed error dict."""
    err = result.get("error") if isinstance(result, dict) else None
    if isinstance(err, dict):
        pytest.skip(f"{tool}: {err.get('kind')}: {err.get('message')}")


@pytest.mark.online
def test_quote_aapl():
    q = quote("AAPL")
    assert q["symbol"] == "AAPL"
    if q.get("note") and q.get("bid_raw") is None:
        pytest.skip(f"quote: no live quote: {q['note']}")
    assert isinstance(q["bid_raw"], float) and q["bid_raw"] > 0
    assert isinstance(q["ask_raw"], float) and q["ask_raw"] > 0
    assert isinstance(q["is_halted"], bool)


@pytest.mark.online
def test_token_list():
    out = token_list(limit=20)
    assert out["count"] >= 1
    assert all(row.get("symbol") for row in out["tokens"])


@pytest.mark.online
def test_corporate_actions():
    out = corporate_actions(limit=5)
    rows = out["actions"]
    if not rows:
        pytest.skip("corporate_actions: API returned an empty actions list")
    assert all("symbol" in r and "type" in r for r in rows)


@pytest.mark.online
def test_holder_snapshot_limit3():
    out = holder_snapshot("AAPL", limit=3)
    _skip_on_error_dict(out, "holder_snapshot")
    rows = out["holders"]
    assert len(rows) <= 3
    if rows and all(r.get("share_pct") is None for r in rows):
        pytest.skip(f"holder_snapshot: supply unavailable: "
                    f"{[w.get('message') for w in out.get('warnings', [])]}")
    assert all(r.get("address") and "share_pct" in r for r in rows)


@pytest.mark.online
def test_transfer_history_limit3():
    out = transfer_history("AAPL", limit=3)
    _skip_on_error_dict(out, "transfer_history")
    rows = out["transfers"]
    if not rows:
        pytest.skip(f"rpc window empty: note={out.get('note', 'none')}")
    assert len(rows) <= 3
    assert all(k in r for r in rows for k in ("ts", "from", "to", "value"))


@pytest.mark.online
def test_price_history_degradation(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCUS_GATEWAY_DATA", str(tmp_path))
    out = price_history("AAPL")  # data_dir() resolves per call: fresh dir
    assert isinstance(out.get("error"), str) and out["error"]
    assert out.get("count") == 0 and out.get("rows") == []
    assert out.get("note") or out.get("hint")


@pytest.mark.online
def test_market_status():
    out = market_status()
    assert isinstance(out["total_tokens"], int) and out["total_tokens"] > 0
    assert isinstance(out["active"], int) and out["active"] > 0
    assert isinstance(out["halted"], list)
