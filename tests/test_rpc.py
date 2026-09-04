"""Full offline test coverage for arcus_mcp.rpc (public JSON-RPC client).

100% offline: urllib.request.urlopen is monkeypatched to serve fixture
data or scripted failures - no socket may open. Covers:

  * chain_id()/block_number(): 0x-hex parsing + drpc fallback wiring
  * get_block_timestamp(): fixture block timestamp; missing -> ValueError
  * call(): hex string result; the value == int(data, 16) / 10**18
    normalization invariant used by every consumer
  * get_transfers(): fixture window metadata, Transfer topic filter,
    removed-log dropping, ascending sort, block-range validation
  * adaptive walk-back: archive-403 shrinks the window 48->32->16->8 and
    retries the SAME top block; 403 at width 8 stops with
    "window-closed"; the 14-request budget stops with "budget";
    non-403 failures (rate limit, network, timeout) still raise
  * retry/backoff: 429/5xx retried with 2s*(n+1); timeouts NOT retried
  * RpcError taxonomy: rpc-timeout / rpc-http / rpc-network / rpc-limit
  * env overrides ARCUS_RPC_URL / ARCUS_RPC_FALLBACK_URL
"""
import email.message
import io
import json
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import arcus_mcp.rpc as rpc

FIXTURES = Path(__file__).parent / "fixtures"
CONTRACT = "0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9"
LOGS = json.loads((FIXTURES / "rpc_transfer_logs.json").read_text())
BLOCK = json.loads((FIXTURES / "rpc_block.json").read_text())
BLOCK_NUMBER = int(BLOCK["result"]["number"], 16)  # 0x33e29db


class FakeClock:
    """time.monotonic stand-in advanced by hand."""

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


class RawResponse:
    """Response serving arbitrary bytes (non-JSON bodies)."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok(result):
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def _http_error(url: str, code: int, msg: str, body: bytes = b""):
    return urllib.error.HTTPError(
        url, code, msg, email.message.Message(),
        io.BytesIO(body) if body else None)


def _archive_403(url: str):
    return _http_error(url, 403, "Forbidden",
                       b"Archive requests require a personal token")


@pytest.fixture
def rpc_mock(monkeypatch):
    """Fresh module state + fake clock/sleep + scripted urlopen.

    Yields a namespace with `calls` (one dict per request: url, method,
    params) and `sleeps` (recorded sleep durations, never executed).
    """
    clock = FakeClock()
    calls: list[dict] = []
    sleeps: list[float] = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        calls.append({"url": req.full_url, "method": body["method"],
                      "params": body["params"]})
        m = body["method"]
        if m == "eth_chainId":
            return FakeResponse(_ok("0x1237"))
        if m == "eth_blockNumber":
            return FakeResponse(_ok("0x33e29db"))
        if m == "eth_getBlockByNumber":
            return FakeResponse(BLOCK)
        if m == "eth_call":
            return FakeResponse(_ok(hex(200_000_000 * 10 ** 18)))
        if m == "eth_getLogs":
            return FakeResponse(_ok(LOGS))
        raise AssertionError(f"unexpected method {m}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(rpc, "_last_req", 0.0)
    monkeypatch.setattr(rpc.time, "monotonic", clock)
    monkeypatch.setattr(rpc.time, "sleep", lambda s: sleeps.append(s))
    return SimpleNamespace(clock=clock, calls=calls, sleeps=sleeps)


def _serve(monkeypatch, fn):
    calls: list[dict] = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        calls.append({"url": req.full_url, "method": body["method"],
                      "params": body["params"]})
        return fn(body)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


# ------------------------------------------------------ chain_id / blocks


def test_chain_id_int(rpc_mock):
    assert rpc.chain_id() == 4663
    assert rpc_mock.calls[-1]["method"] == "eth_chainId"


def test_chain_id_falls_back_to_drpc(rpc_mock, monkeypatch):
    hit: list[str] = []

    def failing(req, timeout=None):
        hit.append(req.full_url)
        if req.full_url == rpc.DEFAULT_RPC_URL:
            raise urllib.error.URLError("primary down")
        return FakeResponse(_ok("0x1237"))

    monkeypatch.setattr(urllib.request, "urlopen", failing)
    assert rpc.chain_id() == 4663
    assert rpc.DEFAULT_FALLBACK_URL in hit  # drpc actually served it


def test_chain_id_both_endpoints_down(rpc_mock, monkeypatch):
    def down(req, timeout=None):
        raise urllib.error.URLError("no route")
    monkeypatch.setattr(urllib.request, "urlopen", down)
    with pytest.raises(rpc.RpcError) as ei:
        rpc.chain_id()
    assert ei.value.kind == "rpc-network"
    assert "both endpoints" in str(ei.value)


def test_block_number_int_and_fallback(rpc_mock, monkeypatch):
    assert rpc.block_number() == BLOCK_NUMBER
    state = {"n": 0}

    def flaky(req, timeout=None):
        state["n"] += 1
        if state["n"] == 1:  # primary 500s once, then falls to drpc? no:
            # a FINAL error on primary triggers the fallback path
            raise _http_error(req.full_url, 500, "Server Error")
        if req.full_url == rpc.DEFAULT_RPC_URL:
            raise _http_error(req.full_url, 503, "Unavailable")
        return FakeResponse(_ok("0x33e29db"))

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    assert rpc.block_number() == BLOCK_NUMBER  # via fallback


def test_get_block_timestamp_from_fixture(rpc_mock):
    ts = rpc.get_block_timestamp("0x33e29db")
    assert ts == int(BLOCK["result"]["timestamp"], 16)
    assert rpc_mock.calls[-1]["method"] == "eth_getBlockByNumber"
    # params carry the block hex and no full-transactions flag
    assert rpc_mock.calls[-1]["params"] == ["0x33e29db", False]


def test_get_block_timestamp_accepts_int_and_dec(rpc_mock):
    assert rpc.get_block_timestamp(BLOCK_NUMBER) == int(
        BLOCK["result"]["timestamp"], 16)
    assert rpc.get_block_timestamp(str(BLOCK_NUMBER)) == int(
        BLOCK["result"]["timestamp"], 16)


def test_get_block_timestamp_missing_block(rpc_mock, monkeypatch):
    _serve(monkeypatch, lambda body: FakeResponse(_ok(None)))
    with pytest.raises(ValueError, match="no timestamp|not found"):
        rpc.get_block_timestamp("0xdeadbee")


# ------------------------------------------------------------------ call


def test_call_returns_hex_string_and_value_invariant(rpc_mock):
    """Total-supply normalization invariant: units = int(hex, 16) and
    token value == int(result, 16) / 10**18."""
    res = rpc.call(CONTRACT, "0x18160ddd")
    assert isinstance(res, str) and res.startswith("0x")
    units = int(res, 16)
    assert units == 200_000_000 * 10 ** 18
    assert units / 10 ** 18 == 200000000.0  # value invariant
    sent = rpc_mock.calls[-1]
    assert sent["method"] == "eth_call"
    assert sent["params"][0] == {"to": CONTRACT, "data": "0x18160ddd"}
    assert sent["params"][1] == "latest"  # default block tag


def test_call_explicit_block_becomes_hex(rpc_mock):
    rpc.call(CONTRACT, "0x18160ddd", block=54405595)
    assert rpc_mock.calls[-1]["params"][1] == hex(54405595)


def test_call_non_string_result_is_rpc_http(rpc_mock, monkeypatch):
    _serve(monkeypatch, lambda body: FakeResponse(_ok(["0x0"])))
    with pytest.raises(rpc.RpcError) as ei:
        rpc.call(CONTRACT, "0x18160ddd")
    assert ei.value.kind == "rpc-http"


# ---------------------------------------------------------- get_transfers


def test_get_transfers_fixture_window_and_log(rpc_mock):
    # fixture log sits at block 54405595; a 48-block range = one window
    out = rpc.get_transfers(CONTRACT, 54405548, 54405595)
    assert out["window"]["stopped_reason"] == "complete"
    assert out["window"]["requests"] == 1  # single 48-wide window
    assert out["window"]["oldest_covered"] == 54405548
    assert out["window"]["newest_covered"] == 54405595
    assert len(out["logs"]) == 1
    log = out["logs"][0]
    assert log["topics"][0] == rpc.TRANSFER_TOPIC
    assert log["blockTimestamp"] == "0x6a9af0e3"
    # invariant: token value = int(data, 16) / 1e18
    assert int(log["data"], 16) / 10 ** 18 == pytest.approx(0.0574105022)


def test_get_transfers_filters_transfer_topic_and_address(rpc_mock):
    rpc.get_transfers(CONTRACT, 54405548, 54405595)
    sent = rpc_mock.calls[-1]["params"][0]
    assert sent["topics"] == [rpc.TRANSFER_TOPIC]
    assert sent["address"] == CONTRACT
    assert sent["fromBlock"] == hex(54405548)
    assert sent["toBlock"] == hex(54405595)


def test_get_transfers_accepts_hex_and_none_to_block(rpc_mock, monkeypatch):
    monkeypatch.setattr(rpc, "block_number", lambda: BLOCK_NUMBER)
    out = rpc.get_transfers(CONTRACT, hex(BLOCK_NUMBER - 47))
    assert out["window"]["to_block"] == BLOCK_NUMBER
    assert out["window"]["stopped_reason"] == "complete"


def test_get_transfers_drops_removed_and_sorts_ascending(rpc_mock,
                                                         monkeypatch):
    newer = dict(LOGS[0], blockNumber=hex(BLOCK_NUMBER + 1),
                 logIndex="0x1", removed=False)
    stale = dict(LOGS[0], blockNumber=hex(BLOCK_NUMBER + 2),
                 logIndex="0x2", removed=True)  # re-org'd away
    oldest = dict(LOGS[0], blockNumber=hex(BLOCK_NUMBER),
                  logIndex="0x9", removed=False)
    # served deliberately out of order
    _serve(monkeypatch, lambda body: FakeResponse(
        _ok([newer, stale, oldest])))
    out = rpc.get_transfers(CONTRACT, BLOCK_NUMBER, BLOCK_NUMBER + 2)
    assert len(out["logs"]) == 2  # removed entry dropped
    bns = [int(l["blockNumber"], 16) for l in out["logs"]]
    assert bns == sorted(bns)  # ascending by block
    assert int(out["logs"][0]["logIndex"], 16) == 0x9


def test_get_transfers_validates_range_and_contract(rpc_mock):
    with pytest.raises(ValueError, match="above"):
        rpc.get_transfers(CONTRACT, 100, 99)
    with pytest.raises(ValueError, match="required"):
        rpc.get_transfers("", 0, 10)


def test_get_transfers_empty_batch_is_complete(rpc_mock, monkeypatch):
    _serve(monkeypatch, lambda body: FakeResponse(_ok([])))
    out = rpc.get_transfers(CONTRACT, 100, 147)
    assert out["logs"] == []
    assert out["window"]["stopped_reason"] == "complete"
    assert "0 Transfer log" in out["window"]["note"]


# ------------------------------------------------- adaptive 403 walk-back


def test_walk_shrinks_on_archive_403(rpc_mock, monkeypatch):
    """403 -> shrink 48->32->16->8, retry same top block; success at the
    smaller width, then a 403 at width 8 stops with window metadata."""
    seen_widths: list[int] = []
    ok_windows: list[int] = []
    ARCHIVE_FLOOR = 54560030  # pretend blocks below this are archive-only

    def fake(req, timeout=None):
        body = json.loads(req.data.decode())
        assert body["method"] == "eth_getLogs"
        p = body["params"][0]
        width = int(p["toBlock"], 16) - int(p["fromBlock"], 16) + 1
        seen_widths.append(width)
        if int(p["fromBlock"], 16) < ARCHIVE_FLOOR:
            raise _archive_403(req.full_url)
        ok_windows.append(width)
        if len(ok_windows) == 1:  # only the newest window has the log
            return FakeResponse(_ok(LOGS))
        return FakeResponse(_ok([]))

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(rpc, "block_number", lambda: 54560060)
    out = rpc.get_transfers(CONTRACT, 54560000)
    # ladder: 48 and 32 403-ed, 16 caught the log, then 8 also worked but
    # the block below the archive floor 403-ed even at width 8
    assert seen_widths[:3] == [48, 32, 16]
    assert out["window"]["stopped_reason"] == "window-closed"
    assert out["window"]["final_width"] == 8
    assert out["window"]["requests"] >= 4
    assert len(out["logs"]) == 1  # the width-16 success
    assert "explorer" in out["window"]["note"]  # honest pointer onward


def test_walk_all_widths_403_stops_at_eight(rpc_mock, monkeypatch):
    """Even width 8 keeps 403-ing: stop after the ladder bottoms out,
    never raise, and report what (nothing) was caught."""
    n = {"k": 0}

    def fake(req, timeout=None):
        body = json.loads(req.data.decode())
        assert body["method"] == "eth_getLogs"
        n["k"] += 1
        raise _archive_403(req.full_url)

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(rpc, "block_number", lambda: 54560060)
    out = rpc.get_transfers(CONTRACT, 54560000)
    assert n["k"] == 4  # exactly the ladder: 48, 32, 16, 8
    assert out["window"]["stopped_reason"] == "window-closed"
    assert out["window"]["widths_used"] == []  # no window ever succeeded
    assert out["logs"] == []
    assert out["window"]["oldest_covered"] is None


def test_walk_budget_cap(rpc_mock, monkeypatch):
    """from_block far below the reachable window: after the ladder
    bottoms out at width 8 the walk keeps stepping down 8 blocks at a
    time until the request-budget cap stops it."""
    def fake(req, timeout=None):
        body = json.loads(req.data.decode())
        if body["method"] == "eth_getLogs":
            p = body["params"][0]
            width = int(p["toBlock"], 16) - int(p["fromBlock"], 16) + 1
            if width > 8:
                raise _archive_403(req.full_url)
            return FakeResponse(_ok([]))
        return FakeResponse(_ok("0x33e29db"))

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(rpc, "block_number", lambda: 54560100)
    out = rpc.get_transfers(CONTRACT, 54500000)
    assert out["window"]["requests"] == rpc._MAX_WALK_REQUESTS
    assert out["window"]["stopped_reason"] == "budget"
    assert out["logs"] == []
    assert "budget" in out["window"]["note"]


def test_walk_non_403_failure_raises(rpc_mock, monkeypatch):
    """A rate-limited getLogs must raise loudly - an empty answer must
    never fake success (only archive-403 is a walk signal)."""
    def fake(req, timeout=None):
        raise _http_error(req.full_url, 429, "Too Many Requests")

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    with pytest.raises(rpc.RpcError) as ei:
        rpc.get_transfers(CONTRACT, 100, 200)
    assert ei.value.kind == "rpc-limit"


# ------------------------------------------------------ retry / taxonomy


def test_rate_limit_interval_and_backoff(rpc_mock, monkeypatch):
    """429 once then success: exactly one 2s backoff sleep, then the
    result. Rate-limit gaps between requests are <= 50ms each (the fake
    clock does not advance, so every request waits the full interval)."""
    state = {"n": 0}

    def fake(req, timeout=None):
        body = json.loads(req.data.decode())
        if body["method"] == "eth_chainId":
            state["n"] += 1
            if state["n"] == 1:
                raise _http_error(req.full_url, 429, "Too Many Requests")
            return FakeResponse(_ok("0x1237"))
        raise AssertionError(body["method"])

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert rpc.chain_id() == 4663
    assert 2.0 in rpc_mock.sleeps  # backoff after the 429
    intervals = [s for s in rpc_mock.sleeps if s <= 0.05]
    assert intervals and all(0 < s <= 0.05 for s in intervals)


def test_429_persistent_is_rpc_limit(rpc_mock, monkeypatch):
    calls: list[dict] = []

    def fake(req, timeout=None):
        body = json.loads(req.data.decode())
        calls.append(body["method"])
        raise _http_error(req.full_url, 429, "Too Many Requests")

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    with pytest.raises(rpc.RpcError) as ei:
        rpc.post("eth_chainId")
    assert ei.value.kind == "rpc-limit"
    assert ei.value.status == 429
    assert len(calls) == 3  # 1 attempt + 2 retries
    assert [s for s in rpc_mock.sleeps if s >= 2.0] == [2.0, 4.0]


def test_5xx_retried_then_success(rpc_mock, monkeypatch):
    state = {"n": 0}

    def fake(req, timeout=None):
        state["n"] += 1
        if state["n"] == 1:
            raise _http_error(req.full_url, 502, "Bad Gateway")
        return FakeResponse(_ok("0x1237"))

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert rpc.chain_id() == 4663
    assert state["n"] == 2
    assert 2.0 in rpc_mock.sleeps


def test_5xx_persistent_is_rpc_http(rpc_mock, monkeypatch):
    calls: list[dict] = []

    def fake(req, timeout=None):
        body = json.loads(req.data.decode())
        calls.append(body["method"])
        raise _http_error(req.full_url, 500, "Server Error")

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    with pytest.raises(rpc.RpcError) as ei:
        rpc.post("eth_chainId")
    assert ei.value.kind == "rpc-http"
    assert ei.value.status == 500
    assert len(calls) == 3


def test_timeout_not_retried(rpc_mock, monkeypatch):
    calls = _serve(monkeypatch, lambda body: (_ for _ in ()).throw(
        TimeoutError("timed out")))
    with pytest.raises(rpc.RpcError) as ei:
        rpc.post("eth_chainId")
    assert ei.value.kind == "rpc-timeout"
    assert len(calls) == 1  # burned the full socket budget: no retry
    assert not [s for s in rpc_mock.sleeps if s >= 2.0]


def test_urlerror_wrapping_timeout_is_rpc_timeout(rpc_mock, monkeypatch):
    calls = _serve(monkeypatch, lambda body: (_ for _ in ()).throw(
        urllib.error.URLError(TimeoutError("timed out"))))
    with pytest.raises(rpc.RpcError) as ei:
        rpc.post("eth_chainId")
    assert ei.value.kind == "rpc-timeout"
    assert len(calls) == 1


def test_rpc_error_kinds(rpc_mock, monkeypatch):
    def timeout_err(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", timeout_err)
    with pytest.raises(rpc.RpcError) as ei:
        rpc.post("eth_chainId")
    assert ei.value.kind == "rpc-timeout"

    def net_err(req, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", net_err)
    with pytest.raises(rpc.RpcError) as ei:
        rpc.post("eth_chainId")
    assert ei.value.kind == "rpc-network"
    # two backoffs (2s, 4s) plus 50ms rate-limit gaps between attempts
    assert sorted(set(rpc_mock.sleeps)) == [0.05, 2.0, 4.0]

    def rpc_err(req, timeout=None):
        return FakeResponse({"jsonrpc": "2.0", "id": 1, "error": {
            "code": -32601, "message": "the method eth_getLogs is not "
            "available on this endpoint"}})

    monkeypatch.setattr(urllib.request, "urlopen", rpc_err)
    with pytest.raises(rpc.RpcError) as ei:
        rpc.post("eth_getLogs", [])
    assert ei.value.kind == "rpc-http"
    assert "-32601" in str(ei.value)


def test_malformed_responses_are_rpc_http(rpc_mock, monkeypatch):
    # JSON-RPC payload without a result field
    _serve(monkeypatch, lambda body: FakeResponse({"jsonrpc": "2.0",
                                                   "id": 1}))
    with pytest.raises(rpc.RpcError, match="no result"):
        rpc.post("eth_chainId")
    # body is not JSON at all
    _serve(monkeypatch, lambda body: RawResponse(b"<html>gateway</html>"))
    with pytest.raises(rpc.RpcError, match="invalid JSON"):
        rpc.post("eth_chainId")


# ------------------------------------------------------ parsing / config


def test_to_int_parsing():
    assert rpc._to_int("0x1237") == 4663
    assert rpc._to_int(4663) == 4663
    assert rpc._to_int("4663") == 4663
    assert rpc._to_int(" 0x10 ") == 16
    for bad in ("latest", "", "0xzz", None, True, "1.5"):
        with pytest.raises(ValueError):
            rpc._to_int(bad)


def test_block_param_tags_and_hex():
    assert rpc._block_param("latest") == "latest"
    assert rpc._block_param("FINALIZED") == "finalized"
    assert rpc._block_param(16) == "0x10"
    assert rpc._block_param("16") == "0x10"
    with pytest.raises(ValueError):
        rpc._block_param("soon")


def test_env_overrides_urls(rpc_mock, monkeypatch):
    monkeypatch.setenv("ARCUS_RPC_URL", "https://example.invalid/rpc")
    assert rpc._rpc_url() == "https://example.invalid/rpc"
    monkeypatch.setenv("ARCUS_RPC_FALLBACK_URL", "https://fb.invalid")
    assert rpc._fallback_url() == "https://fb.invalid"
    assert "publicnode" in rpc.DEFAULT_RPC_URL
    assert "drpc" in rpc.DEFAULT_FALLBACK_URL


def test_post_sends_jsonrpc_envelope(rpc_mock):
    rpc.post("eth_chainId")
    # the fixture records the decoded body: envelope shape is checked there
    sent = rpc_mock.calls[-1]
    assert sent["method"] == "eth_chainId"
    assert sent["params"] == []
    # request ids increment across calls (one id per JSON-RPC call)
    rpc.post("eth_blockNumber")
    assert rpc_mock.calls[-1]["method"] == "eth_blockNumber"
