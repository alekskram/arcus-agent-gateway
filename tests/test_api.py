"""Unit tests for arcus_mcp.api - 100% offline (fixtures + fake clock).

Covers the MEC-54 invariants for the REST client layer:
  * cache TTLs (assets 300s, prices 15s, corporate-actions 3600s):
    no repeat HTTP inside the TTL, refetch after it
  * asset() case-insensitive lookup
  * prices() 404 -> None (and negative answers stay uncached)
  * rate limiter: >=20ms between consecutive requests
  * get() retry/backoff on 429, ValueError on exhausted retries,
    RuntimeError when the network is down

No test in this module may open a socket: urllib.request.urlopen is
monkeypatched to serve tests/fixtures/*.json.
"""
import email.message
import json
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import arcus_mcp.api as api

FIXTURES = Path(__file__).parent / "fixtures"


class FakeClock:
    """time.monotonic stand-in we advance by hand."""

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


def _http_error(url: str, code: int, msg: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url, code, msg, email.message.Message(), None)


# The /corporate-actions wrapper key was never captured live (API_NOTES
# #5): exercise the dict-wrapped shape, the tolerant unwrap path.
ACTIONS_PAYLOAD = {"corporateActions": [
    {"tokenSymbol": "AAPL", "kind": "DIVIDEND", "amount": "0.25",
     "processDate": "2026-08-11"},
    {"tokenSymbol": "MSFT", "kind": "DIVIDEND", "amount": "0.83",
     "processDate": "2026-08-18"},
]}


@pytest.fixture
def api_mock(monkeypatch):
    """Fresh api module state + fake clock/sleep + fixture-backed urlopen.

    Returns a namespace with `clock`, `calls` (requested paths, in
    order) and `sleeps` (recorded sleep durations, never executed).
    """
    clock = FakeClock()
    calls: list[str] = []
    sleeps: list[float] = []

    def fake_urlopen(req, timeout=None):
        path = req.full_url.replace(api.BASE, "").split("?")[0]
        calls.append(path)
        if path == "assets":
            return FakeResponse(
                json.loads((FIXTURES / "assets.json").read_text()))
        if path.startswith("prices/"):
            sym = path.split("/", 1)[1]
            f = FIXTURES / f"prices_{sym}.json"
            if not f.exists():
                raise _http_error(req.full_url, 404, "Not Found")
            return FakeResponse(json.loads(f.read_text()))
        if path == "corporate-actions":
            return FakeResponse(ACTIONS_PAYLOAD)
        raise _http_error(req.full_url, 404, "Not Found")

    # reset module-level caches/rate-limit state so tests are order-free
    monkeypatch.setattr(api, "_ASSETS_CACHE", (float("-inf"), []))
    monkeypatch.setattr(api, "_ACTIONS_CACHE", (float("-inf"), []))
    monkeypatch.setattr(api, "_PRICES_CACHE", {})
    monkeypatch.setattr(api, "_last_req", 0.0)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(api.time, "monotonic", clock)
    monkeypatch.setattr(api.time, "sleep", lambda s: sleeps.append(s))
    return SimpleNamespace(clock=clock, calls=calls, sleeps=sleeps)


# --------------------------------------------------------------- assets()


def test_assets_returns_full_fixture_list(api_mock):
    out = api.assets()
    assert isinstance(out, list)
    assert len(out) == 194  # live snapshot 2026-09-03
    assert all(isinstance(a, dict) for a in out)


def test_assets_cache_hit_within_ttl(api_mock):
    api.assets()
    n = len(api_mock.calls)
    api.assets()  # fresh: must be served from cache
    assert len(api_mock.calls) == n
    assert api_mock.calls.count("assets") == 1


def test_assets_refetch_after_ttl(api_mock):
    api.assets()
    n = len(api_mock.calls)
    api_mock.clock.advance(api._ASSETS_TTL + 1)  # 300s + epsilon
    api.assets()
    assert len(api_mock.calls) == n + 1


def test_assets_tolerates_bare_list(api_mock, monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: FakeResponse([{"tokenSymbol": "X"}]))
    monkeypatch.setattr(api, "_ASSETS_CACHE", (float("-inf"), []))
    assert api.assets() == [{"tokenSymbol": "X"}]


# ---------------------------------------------------------------- asset()


def test_asset_case_insensitive_and_whitespace(api_mock):
    for probe in ("aapl", "AAPL", "aApL", "  aapl  "):
        row = api.asset(probe)
        assert row is not None, probe
        assert row["tokenSymbol"] == "AAPL"


def test_asset_unknown_empty_none(api_mock):
    assert api.asset("NOPE") is None
    assert api.asset("") is None
    assert api.asset(None) is None  # type: ignore[arg-type]  # garbage in


# --------------------------------------------------------------- prices()


def test_prices_first_quote_from_fixture(api_mock):
    q = api.prices("aapl")
    assert q is not None
    assert q["tokenSymbol"] == "AAPL"
    assert q["bid"] == "327.77" and q["ask"] == "327.78"
    assert q["isTradingHalt"] is False


def test_prices_cache_within_ttl(api_mock):
    api.prices("AAPL")
    n = len(api_mock.calls)
    api.prices("AAPL")
    assert len(api_mock.calls) == n
    assert api_mock.calls.count("prices/AAPL") == 1


def test_prices_refetch_after_ttl(api_mock):
    api.prices("AAPL")
    n = len(api_mock.calls)
    api_mock.clock.advance(api._PRICES_TTL + 1)  # 15s + epsilon
    api.prices("AAPL")
    assert len(api_mock.calls) == n + 1


def test_prices_404_maps_to_none(api_mock):
    assert api.prices("ZZZZ") is None
    assert "prices/ZZZZ" in api_mock.calls


def test_prices_404_not_cached(api_mock):
    api.prices("ZZZZ")
    api.prices("ZZZZ")  # negative answers stay uncached (API_NOTES #6)
    assert api_mock.calls.count("prices/ZZZZ") == 2


def test_prices_empty_quotes_list_maps_to_none(api_mock, monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: FakeResponse({"quotes": []}))
    assert api.prices("AAPL") is None


# ----------------------------------------------------- corporate_actions()


def test_corporate_actions_unwraps_dict(api_mock):
    acts = api.corporate_actions()
    assert isinstance(acts, list)
    assert len(acts) == 2
    assert {a["tokenSymbol"] for a in acts} == {"AAPL", "MSFT"}


def test_corporate_actions_filter_and_limit(api_mock):
    aapl = api.corporate_actions("aapl")
    assert len(aapl) == 1 and aapl[0]["tokenSymbol"] == "AAPL"
    assert api.corporate_actions(limit=1).__len__() == 1
    assert api.corporate_actions("NOPE") == []


def test_corporate_actions_cache_ttl(api_mock):
    api.corporate_actions()
    n = len(api_mock.calls)
    api.corporate_actions()  # 1h TTL: cached
    assert len(api_mock.calls) == n
    api_mock.clock.advance(api._ACTIONS_TTL + 1)
    api.corporate_actions()
    assert len(api_mock.calls) == n + 1


# ------------------------------------------------------------ rate limit


def test_rate_limit_every_request_waits_20ms(api_mock):
    api._last_req = api_mock.clock()  # a request "just happened"
    api_mock.sleeps.clear()
    for _ in range(4):
        api.get("prices/AAPL")
    # frozen clock: each of the 4 requests must wait the full interval
    assert len(api_mock.sleeps) == 4
    assert all(w >= api._MIN_INTERVAL - 1e-9 for w in api_mock.sleeps)
    assert all(abs(w - 0.02) < 1e-9 for w in api_mock.sleeps)


def test_rate_limit_no_wait_when_interval_elapsed(api_mock):
    api._last_req = api_mock.clock()
    api_mock.clock.advance(1.0)  # long since the last request
    api.get("prices/AAPL")
    assert api_mock.sleeps == []  # nothing to wait


# ------------------------------------------------------- retries / get()


class Flaky429Open:
    """Fails N times with 429, then serves a 200."""

    def __init__(self, failures: int = 1):
        self.failures = failures
        self.calls = 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise _http_error(req.full_url, 429, "Too Many Requests")
        return FakeResponse(
            {"quotes": [{"tokenSymbol": "AAPL", "bid": "1", "ask": "2"}]})


def test_get_retries_on_429_then_succeeds(api_mock, monkeypatch):
    flaky = Flaky429Open(failures=1)
    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    out = api.get("prices/AAPL")
    assert out["quotes"][0]["bid"] == "1"  # type: ignore[index,union-attr]
    assert flaky.calls == 2                      # 429 + retry
    assert 2.0 in api_mock.sleeps                # backoff 2s*(0+1)


def test_get_backoff_scales_with_attempt(api_mock, monkeypatch):
    flaky = Flaky429Open(failures=2)
    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    api.get("prices/AAPL")
    backoffs = [s for s in api_mock.sleeps if s >= 1.0]
    assert backoffs == [2.0, 4.0]                # 2s, then 2s*(1+1)


def test_get_exhausted_retries_raises_valueerror(api_mock, monkeypatch):
    def always_429(req, timeout=None):
        raise _http_error(req.full_url, 429, "Too Many Requests")
    monkeypatch.setattr(urllib.request, "urlopen", always_429)
    with pytest.raises(ValueError, match="429"):
        api.get("prices/AAPL", retries=2)


def test_get_network_down_raises_runtimeerror(api_mock, monkeypatch):
    def down(req, timeout=None):
        raise urllib.error.URLError("no route to host")
    monkeypatch.setattr(urllib.request, "urlopen", down)
    with pytest.raises(RuntimeError, match="unreachable"):
        api.get("assets", retries=2)


def test_get_serializes_params_into_query_string(api_mock, monkeypatch):
    seen: list[str] = []

    def fake(req, timeout=None):
        seen.append(req.full_url)
        return FakeResponse([])

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    api.get("corporate-actions", params={"symbol": "AAPL",
                                         "limit": 5, "skip": None})
    assert seen == [f"{api.BASE}corporate-actions?symbol=AAPL&limit=5"]
