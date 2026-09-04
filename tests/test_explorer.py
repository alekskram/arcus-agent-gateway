"""Full offline test coverage for arcus_mcp.explorer (Blockscout v2).

100% offline: urllib.request.urlopen is monkeypatched to serve fixture
data or scripted failures - no socket may open. Covers:

  * token()/token_holders()/address_token_balances() parsing against
    the live-captured fixtures (rows, raw-string values, 404 -> None)
  * the browser User-Agent header on EVERY request (no UA -> Cloudflare
    403 challenge in production)
  * Cloudflare 403 ("Just a moment" HTML) -> ExplorerError kind
    "cloudflare", immediately (no retry burn)
  * socket timeout -> kind "explorer-timeout" (contract-address
    token-balances hang 40s+ in production), also not retried
  * 5xx retried twice with 2s*(n+1) backoff, then "explorer-network";
    non-retryable HTTP -> "explorer-http"
  * TTL caches (600s) per key: no repeat HTTP inside the TTL, refetch
    after it (monkeypatched monotonic clock)
  * holders limit clamping 1..50 served from ONE cached page fetch
"""
import email.message
import io
import json
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import arcus_mcp.explorer as explorer

FIXTURES = Path(__file__).parent / "fixtures"
CONTRACT = "0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9"
EOA = "0x8366a39CC670B4001A1121B8F6A443A643e40951"
CLOUDFLARE_HTML = (b"<!DOCTYPE html><html><head><title>Just a moment..."
                   b"</title></head></html>")


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class BytesResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(url: str, code: int, msg: str, body: bytes = b""):
    return urllib.error.HTTPError(
        url, code, msg, email.message.Message(),
        io.BytesIO(body) if body else None)


def _cloudflare_403(url: str):
    return _http_error(url, 403, "Forbidden", CLOUDFLARE_HTML)


@pytest.fixture
def explorer_mock(monkeypatch):
    """Fresh module caches + fake clock/sleep + fixture-backed urlopen.

    Yields a namespace with `calls` (one dict per request: url, headers)
    and `sleeps` (recorded sleep durations, never executed).
    """
    clock = FakeClock()
    calls: list[dict] = []
    sleeps: list[float] = []

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        path = url.replace(explorer.BASE, "").split("?")[0].lower()
        calls.append({"url": url, "path": path,
                      "headers": dict(req.header_items())})
        if path == f"/tokens/{CONTRACT.lower()}":
            return FakeResponse(json.loads(
                (FIXTURES / "explorer_token.json").read_text()))
        if path == f"/tokens/{CONTRACT.lower()}/holders":
            return FakeResponse(json.loads(
                (FIXTURES / "explorer_holders.json").read_text()))
        if path == f"/addresses/{EOA.lower()}/token-balances":
            return FakeResponse(json.loads(
                (FIXTURES / "explorer_token_balances.json").read_text()))
        raise _http_error(url, 404, "Not Found")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(explorer, "_TOKEN_CACHE", {})
    monkeypatch.setattr(explorer, "_HOLDERS_CACHE", {})
    monkeypatch.setattr(explorer, "_BALANCES_CACHE", {})
    monkeypatch.setattr(explorer.time, "monotonic", clock)
    monkeypatch.setattr(explorer.time, "sleep", lambda s: sleeps.append(s))
    return SimpleNamespace(clock=clock, calls=calls, sleeps=sleeps)


# ------------------------------------------------------------------ token


def test_token_parses_fixture_row(explorer_mock):
    tok = explorer.token(CONTRACT)
    assert tok["symbol"] == "AAPL"
    assert tok["decimals"] == "18"
    assert tok["holders_count"] == "62882"
    # total_supply is a RAW unscaled string: /10**18 is the caller's job
    assert int(tok["total_supply"]) / 10 ** 18 == pytest.approx(
        16871.85310037)
    assert float(tok["exchange_rate"]) == 321.06
    assert float(tok["circulating_market_cap"]) == pytest.approx(
        4864702.466124486)


def test_token_case_insensitive_key(explorer_mock):
    tok = explorer.token(CONTRACT.upper())
    assert tok is not None and tok["symbol"] == "AAPL"


def test_token_unknown_404_maps_to_none(explorer_mock):
    assert explorer.token("0x" + "ab" * 20) is None


def test_token_malformed_address_raises(explorer_mock):
    with pytest.raises(ValueError, match="not a contract/wallet address"):
        explorer.token("0x1234")  # too short
    with pytest.raises(ValueError):
        explorer.token("")
    with pytest.raises(ValueError):
        explorer.token(None)  # type: ignore[arg-type]


def test_token_cache_within_ttl_then_refetch(explorer_mock):
    explorer.token(CONTRACT)
    n = len(explorer_mock.calls)
    explorer.token(CONTRACT)  # fresh: served from cache
    assert len(explorer_mock.calls) == n
    explorer_mock.clock.advance(explorer._TTL + 1)  # 600s + epsilon
    explorer.token(CONTRACT)
    assert len(explorer_mock.calls) == n + 1  # refetched


# ---------------------------------------------------------------- holders


def test_holders_page_shape_and_values(explorer_mock):
    page = explorer.token_holders(CONTRACT)
    assert page is not None
    assert len(page["items"]) == 50  # fixture is one full page
    first = page["items"][0]
    assert first["address"] == EOA  # fixture top holder (PoolManager)
    assert first["is_contract"] is True
    # raw STRING value: int()/10**18 is the caller's job
    assert int(first["value"]) / 10 ** 18 == pytest.approx(
        6155.456228534021317261)
    assert page["next_page_params"] is not None  # honest: more pages exist


def test_holders_limit_slices_cached_page(explorer_mock):
    page = explorer.token_holders(CONTRACT, limit=10)
    assert len(page["items"]) == 10
    assert page["items"][0]["address"] == EOA
    # limits are clamped, never rejected
    assert len(explorer.token_holders(CONTRACT, limit=0)["items"]) == 1
    big = explorer.token_holders(CONTRACT, limit=500)["items"]
    assert len(big) == 50  # page cap
    # all of the above came from ONE fetch (cached page, sliced locally)
    paths = [c["path"] for c in explorer_mock.calls
             if c["path"].endswith("/holders")]
    assert len(paths) == 1


def test_holders_cache_per_address(explorer_mock):
    explorer.token_holders(CONTRACT)
    n = len(explorer_mock.calls)
    explorer.token_holders(CONTRACT, limit=5)  # different limit, same page
    explorer.token_holders(CONTRACT, limit=50)
    assert len(explorer_mock.calls) == n  # no repeat HTTP
    explorer_mock.clock.advance(explorer._TTL + 1)
    explorer.token_holders(CONTRACT)
    assert len(explorer_mock.calls) == n + 1


def test_holders_unknown_404_maps_to_none(explorer_mock):
    assert explorer.token_holders("0x" + "cd" * 20) is None


# --------------------------------------------------------------- balances


def test_balances_flat_list_of_rows(explorer_mock):
    rows = explorer.address_token_balances(EOA)
    assert isinstance(rows, list)
    assert len(rows) == 86  # live snapshot 2026-09-04
    first = rows[0]
    assert first["token"]["symbol"] == "AAPL"
    # raw unscaled STRING: /10**18 for token units
    assert int(first["value"]) / 10 ** 18 == pytest.approx(
        229.942366437633735816)
    # token_instance is dropped (bulky, null on ERC-20)
    assert "token_instance" not in first


def test_balances_cache_within_ttl_then_refetch(explorer_mock):
    explorer.address_token_balances(EOA)
    n = len(explorer_mock.calls)
    explorer.address_token_balances(EOA)
    assert len(explorer_mock.calls) == n
    explorer_mock.clock.advance(explorer._TTL + 1)
    explorer.address_token_balances(EOA)
    assert len(explorer_mock.calls) == n + 1


def test_balances_unknown_404_maps_to_none(explorer_mock):
    assert explorer.address_token_balances("0x" + "ef" * 20) is None


# ------------------------------------------------------ UA + error paths


def test_browser_user_agent_on_every_request(explorer_mock):
    """The UA header is MANDATORY: without it Cloudflare 403s with a
    challenge page. Every request must carry a browser-looking UA."""
    explorer.token(CONTRACT)
    explorer.token_holders(CONTRACT)
    explorer.address_token_balances(EOA)
    explorer.token("0x" + "ab" * 20)  # even the 404 carried the UA
    assert len(explorer_mock.calls) == 4
    for c in explorer_mock.calls:
        ua = c["headers"].get("User-agent") or c["headers"].get(
            "User-Agent")
        assert ua, c["url"]
        assert "Mozilla" in ua  # browser-shaped, not a bare library tag
        assert "Chrome" in ua


def test_cloudflare_403_maps_to_cloudflare_kind(explorer_mock, monkeypatch):
    def fake_open(req, timeout=None):
        raise _cloudflare_403(req.full_url)

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    with pytest.raises(explorer.ExplorerError) as ei:
        explorer.token(CONTRACT)
    assert ei.value.kind == "cloudflare"
    assert ei.value.status == 403
    assert "User-Agent" in (ei.value.hint or "")


def test_cloudflare_not_retried(explorer_mock, monkeypatch):
    hits = {"n": 0}

    def fake_open(req, timeout=None):
        hits["n"] += 1
        raise _cloudflare_403(req.full_url)

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    with pytest.raises(explorer.ExplorerError):
        explorer.token(CONTRACT)
    assert hits["n"] == 1  # challenge fails fast: no backoff burn
    assert explorer_mock.sleeps == []


def test_timeout_maps_to_explorer_timeout(explorer_mock, monkeypatch):
    """A socket timeout (huge-contract token-balances hang) must surface
    as kind explorer-timeout and NOT be retried - it already burned 15s."""
    hits = {"n": 0}

    def fake_open(req, timeout=None):
        hits["n"] += 1
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    with pytest.raises(explorer.ExplorerError) as ei:
        explorer.address_token_balances(EOA)
    assert ei.value.kind == "explorer-timeout"
    assert hits["n"] == 1
    assert explorer_mock.sleeps == []


def test_urlerror_wrapped_timeout_is_explorer_timeout(explorer_mock,
                                                      monkeypatch):
    def fake_open(req, timeout=None):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    with pytest.raises(explorer.ExplorerError) as ei:
        explorer.address_token_balances(EOA)
    assert ei.value.kind == "explorer-timeout"


def test_5xx_retried_with_backoff_then_network(explorer_mock, monkeypatch):
    hits = {"n": 0}

    def fake_open(req, timeout=None):
        hits["n"] += 1
        raise _http_error(req.full_url, 502, "Bad Gateway")

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    with pytest.raises(explorer.ExplorerError) as ei:
        explorer.token(CONTRACT)
    assert ei.value.kind == "explorer-http"  # final status surfaced
    assert ei.value.status == 502
    assert hits["n"] == 3  # 1 attempt + 2 retries
    assert [s for s in explorer_mock.sleeps if s >= 2.0] == [2.0, 4.0]


def test_5xx_retried_then_success(explorer_mock, monkeypatch):
    state = {"n": 0}

    def fake_open(req, timeout=None):
        state["n"] += 1
        if state["n"] == 1:
            raise _http_error(req.full_url, 503, "Unavailable")
        return FakeResponse(json.loads(
            (FIXTURES / "explorer_token.json").read_text()))

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    tok = explorer.token(CONTRACT)
    assert tok["symbol"] == "AAPL"
    assert state["n"] == 2


def test_network_error_retried_then_network_kind(explorer_mock, monkeypatch):
    hits = {"n": 0}

    def fake_open(req, timeout=None):
        hits["n"] += 1
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    with pytest.raises(explorer.ExplorerError) as ei:
        explorer.token(CONTRACT)
    assert ei.value.kind == "explorer-network"
    assert hits["n"] == 3


def test_non_json_body_is_explorer_http(explorer_mock, monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: BytesResponse(b"<html>challenge</html>"))
    with pytest.raises(explorer.ExplorerError) as ei:
        explorer.token(CONTRACT)
    assert ei.value.kind == "explorer-http"


def test_error_to_dict_shape(explorer_mock):
    err = explorer.ExplorerError("explorer-timeout", "boom",
                                 hint="try an EOA")
    assert err.to_dict() == {"error": {"kind": "explorer-timeout",
                                       "hint": "try an EOA",
                                       "message": "boom"}}


def test_env_override_base_url(explorer_mock, monkeypatch):
    monkeypatch.setenv("ARCUS_EXPLORER_URL",
                       "https://explorer.example/api/v2/")
    assert explorer._base() == "https://explorer.example/api/v2"
    monkeypatch.delenv("ARCUS_EXPLORER_URL")
    assert explorer._base() == explorer.BASE


def test_requests_use_items_count_fifty(explorer_mock):
    explorer.token_holders(CONTRACT)
    url = explorer_mock.calls[-1]["url"]
    assert "items_count=50" in url  # always the max page size
