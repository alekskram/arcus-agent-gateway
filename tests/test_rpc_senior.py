"""Offline sanity tests for arcus_mcp.rpc (Senior-authored, quick).

100% offline: urllib.request.urlopen is monkeypatched to serve fixture
data / scripted failures - no socket may open. Full coverage of the
client lives in the Junior-authored test_rpc.py; this file only pins
the walker behavior, fallback wiring and error taxonomy basics that
the client author considers contract-critical.
"""
import email.message
import json
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import arcus_mcp.rpc as rpc

FIXTURES = Path(__file__).parent / "fixtures"
CONTRACT = "0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9"


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += dt


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_403_archive(url):
    e = urllib.error.HTTPError(
        url, 403, "Forbidden", email.message.Message(),
        __import__("io").BytesIO(
            b"Archive requests require a personal token"))
    return e


@pytest.fixture
def rpc_mock(monkeypatch):
    """Fresh module state + fake clock/sleep + scripted urlopen."""
    clock = FakeClock()
    calls: list[dict] = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        calls.append({"url": req.full_url, "method": body["method"],
                      "params": body["params"]})
        m = body["method"]
        if m == "eth_chainId":
            return FakeResponse({"jsonrpc": "2.0", "id": 1,
                                 "result": "0x1237"})
        if m == "eth_blockNumber":
            return FakeResponse({"jsonrpc": "2.0", "id": 1,
                                 "result": "0x33e29db"})
        if m == "eth_getBlockByNumber":
            return FakeResponse(json.loads(
                (FIXTURES / "rpc_block.json").read_text()))
        if m == "eth_call":
            return FakeResponse({"jsonrpc": "2.0", "id": 1,
                                 "result": hex(200_000_000 * 10 ** 18)})
        if m == "eth_getLogs":
            return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": json.loads(
                (FIXTURES / "rpc_transfer_logs.json").read_text())})
        raise AssertionError(f"unexpected method {m}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(rpc, "_last_req", 0.0)
    monkeypatch.setattr(rpc.time, "monotonic", clock)
    sleeps: list[float] = []
    monkeypatch.setattr(rpc.time, "sleep", lambda s: sleeps.append(s))
    return SimpleNamespace(clock=clock, calls=calls, sleeps=sleeps)


def test_chain_id_int(rpc_mock):
    assert rpc.chain_id() == 4663
    assert rpc_mock.calls[-1]["method"] == "eth_chainId"


def test_chain_id_falls_back_to_drpc(rpc_mock, monkeypatch):
    hit: list[str] = []

    def failing(req, timeout=None):
        hit.append(req.full_url)
        if req.full_url == rpc.DEFAULT_RPC_URL:
            raise urllib.error.URLError("primary down")
        return FakeResponse({"jsonrpc": "2.0", "id": 1,
                             "result": "0x1237"})
    monkeypatch.setattr(urllib.request, "urlopen", failing)
    assert rpc.chain_id() == 4663
    assert rpc.DEFAULT_FALLBACK_URL in hit  # drpc actually served it


def test_block_number_int(rpc_mock):
    assert rpc.block_number() == int("33e29db", 16)


def test_get_block_timestamp_from_fixture(rpc_mock):
    ts = rpc.get_block_timestamp("0x33e29db")
    assert ts == int("6a9af0e3", 16)  # fixture block timestamp


def test_call_returns_hex_string(rpc_mock):
    res = rpc.call(CONTRACT, "0x18160ddd")
    assert isinstance(res, str) and res.startswith("0x")
    assert int(res, 16) / 10 ** 18 == 200000000.0  # 2e8 * 1e18 raw


def test_get_transfers_returns_fixture_log_and_window(rpc_mock):
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
    # invariant: float value = int(data, 16) / 1e18
    assert int(log["data"], 16) / 10 ** 18 == pytest.approx(0.0574105022)


def test_walk_shrinks_on_archive_403(rpc_mock, monkeypatch):
    """403 -> shrink 48->32->16->8, retry same top block; success at the
    smaller width, then 403 at width 8 stops with window metadata."""
    seen_widths: list[int] = []
    ok_windows: list[int] = []
    ARCHIVE_FLOOR = 54560030  # pretend blocks below this are archive-only

    def fake(req, timeout=None):
        body = json.loads(req.data.decode())
        assert body["method"] == "eth_getLogs"  # block_number is patched out
        p = body["params"][0]
        width = int(p["toBlock"], 16) - int(p["fromBlock"], 16) + 1
        seen_widths.append(width)
        if int(p["fromBlock"], 16) < ARCHIVE_FLOOR:
            raise _http_403_archive(req.full_url)
        ok_windows.append(width)
        if len(ok_windows) == 1:  # only the newest window has the log
            return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": json.loads(
                (FIXTURES / "rpc_transfer_logs.json").read_text())})
        return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": []})
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


def test_walk_budget_cap(rpc_mock, monkeypatch):
    """from_block far below the reachable window: after the ladder bottoms
    out at width 8 the walk keeps stepping down 8 blocks at a time until
    the 14-request budget cap stops it."""
    def fake(req, timeout=None):
        body = json.loads(req.data.decode())
        if body["method"] == "eth_getLogs":
            p = body["params"][0]
            width = int(p["toBlock"], 16) - int(p["fromBlock"], 16) + 1
            if width > 8:
                raise _http_403_archive(req.full_url)
            return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": []})
        return FakeResponse({"jsonrpc": "2.0", "id": 1,
                             "result": "0x33e29db"})
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(rpc, "block_number", lambda: 54560100)
    out = rpc.get_transfers(CONTRACT, 54500000)
    assert out["window"]["requests"] == rpc._MAX_WALK_REQUESTS
    assert out["window"]["stopped_reason"] == "budget"
    assert out["logs"] == []
    assert "budget" in out["window"]["note"]


def test_rate_limit_interval_and_backoff(rpc_mock, monkeypatch):
    """429 once then success: exactly one 2s backoff sleep, then result.
    Rate-limit sleeps between requests are >= 0.05s apart (fake clock
    does not advance, so every request waits the full interval)."""
    state = {"n": 0}

    def fake(req, timeout=None):
        body = json.loads(req.data.decode())
        if body["method"] == "eth_chainId":
            state["n"] += 1
            if state["n"] == 1:
                raise urllib.error.HTTPError(
                    req.full_url, 429, "Too Many Requests",
                    email.message.Message(), None)
            return FakeResponse({"jsonrpc": "2.0", "id": 1,
                                 "result": "0x1237"})
        raise AssertionError(body["method"])
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert rpc.chain_id() == 4663
    assert 2.0 in rpc_mock.sleeps  # backoff after the 429
    intervals = [s for s in rpc_mock.sleeps if s <= 0.05]
    assert intervals and all(0 < s <= 0.05 for s in intervals)


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
            "available"}})
    monkeypatch.setattr(urllib.request, "urlopen", rpc_err)
    with pytest.raises(rpc.RpcError) as ei:
        rpc.post("eth_getLogs", [])
    assert ei.value.kind == "rpc-http"


def test_env_overrides_urls(rpc_mock, monkeypatch):
    monkeypatch.setenv("ARCUS_RPC_URL", "https://example.invalid/rpc")
    assert rpc._rpc_url() == "https://example.invalid/rpc"
    monkeypatch.setenv("ARCUS_RPC_FALLBACK_URL", "https://fb.invalid")
    assert rpc._fallback_url() == "https://fb.invalid"
    assert "publicnode" in rpc.DEFAULT_RPC_URL
    assert "drpc" in rpc.DEFAULT_FALLBACK_URL
