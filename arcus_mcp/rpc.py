"""Stdlib-only JSON-RPC client for Robinhood Chain (Arcus, chainId 4663).

Two keyless public providers (live capture 2026-09-04, fixtures in
tests/fixtures/rpc_*.json, facts also summarized in API_NOTES.md):

* publicnode (default): eth_call works, but eth_getLogs answers ONLY
  inside a FLOATING ~45-60-block window behind latest - wider or older
  ranges come back as HTTP 403 "Archive requests require a personal
  token" (backend is Alchemy). The window drifts minute to minute, so
  get_transfers() walks back in shrinking windows instead of trusting
  one wide query. getLogs log objects carry `blockTimestamp` directly -
  no per-log eth_getBlockByNumber round-trip is needed.
* drpc (fallback): NO eth_getLogs and NO eth_call (JSON-RPC -32601
  "method not available") - usable ONLY for eth_chainId and
  eth_blockNumber, which is exactly where the fallback is wired in.

Error surface: RpcError with .kind in {"rpc-timeout", "rpc-http",
"rpc-network", "rpc-limit"} plus .status when an HTTP status applies.
Transport is urllib.request.urlopen - offline tests monkeypatch it the
same way tests/test_api.py does for api.get().

Env overrides: ARCUS_RPC_URL (default publicnode), ARCUS_RPC_FALLBACK_URL
(default drpc).
"""
import itertools
import json
import os
import threading
import time
import urllib.error
import urllib.request

DEFAULT_RPC_URL = "https://robinhood-rpc.publicnode.com"
DEFAULT_FALLBACK_URL = "https://robinhood.drpc.org"

TRANSFER_TOPIC = ("0xddf252ad1be2c89b69c2b068fc378daa952ba7f16"
                  "3c4a11628f55a4df523b3ef")

UA = {"User-Agent": "arcus-agent-gateway/0.1",
      "Content-Type": "application/json"}
_last_req = 0.0
_MIN_INTERVAL = 0.05  # >=50ms between requests: 20 req/s politeness cap
_LOCK = threading.Lock()
_TIMEOUT = 12.0
_IDS = itertools.count(1)

# Walk-back ladder for get_transfers: start at 48 (inside the observed
# 45-60-block floating window), halve-ish on each archive-403. Width 8
# still 403-ing means the older range is simply unreachable on publicnode.
_WALK_WIDTHS = (48, 32, 16, 8)
_MAX_WALK_REQUESTS = 14  # hard budget: stop after ~14 getLogs requests


class RpcError(Exception):
    """RPC failure with a taxonomy kind + optional HTTP status.

    kind: "rpc-timeout" (socket timeout, NOT retried - it already
    waited _TIMEOUT), "rpc-http" (final HTTP error or a JSON-RPC
    error payload), "rpc-network" (unreachable after retries),
    "rpc-limit" (429 after retries).
    """

    def __init__(self, message: str, *, kind: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


def _rpc_url() -> str:
    return os.environ.get("ARCUS_RPC_URL") or DEFAULT_RPC_URL


def _fallback_url() -> str:
    return os.environ.get("ARCUS_RPC_FALLBACK_URL") or DEFAULT_FALLBACK_URL


def _to_int(value) -> int:
    """Block-ish scalar -> int. 0x-strings are hex, bare digits decimal;
    block tags ("latest" ...) are rejected - they are not numbers."""
    if isinstance(value, bool):
        raise ValueError(f"bad block number: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.lower().startswith("0x"):
            try:
                return int(s, 16)
            except ValueError:
                pass
        elif s.isdigit():
            return int(s, 10)
    raise ValueError(f"bad block number: {value!r}")


def _block_param(block) -> str:
    """Block tag passthrough ("latest" et al), everything else to 0xhex."""
    if isinstance(block, str) and block.strip().lower() in (
            "latest", "earliest", "pending", "safe", "finalized"):
        return block.strip().lower()
    return hex(_to_int(block))


def post(method: str, params: list | None = None, *, url: str | None = None,
         retries: int = 3):
    """POST one JSON-RPC call; returns the decoded `result` field.

    Rate-limited to one request per _MIN_INTERVAL across threads.
    Retries (2 retries, 3 attempts) on 429/5xx/network errors with
    2s*(n+1) backoff; timeouts are NOT retried. Any final failure is a
    RpcError - see the class docstring for the kind taxonomy.
    """
    global _last_req
    target = url or _rpc_url()
    body = json.dumps({"jsonrpc": "2.0", "id": next(_IDS),
                       "method": method, "params": params or []}).encode()
    for attempt in range(retries):
        with _LOCK:  # thread-safe politeness window
            wait = _MIN_INTERVAL - (time.monotonic() - _last_req)
            if wait > 0:
                time.sleep(wait)
            _last_req = time.monotonic()
        req = urllib.request.Request(target, data=body, headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
                payload = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if (e.code == 429 or 500 <= e.code <= 599) and attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            try:
                detail = e.read(300).decode("utf-8", "replace").strip()
            except Exception:
                detail = ""
            hint = detail or e.reason
            kind = "rpc-limit" if e.code == 429 else "rpc-http"
            raise RpcError(
                f"rpc {e.code} on {method} ({target}): {hint} "
                f"(after {attempt + 1} attempt(s))",
                kind=kind, status=e.code) from e
        except TimeoutError as e:
            # socket.timeout IS TimeoutError since 3.10; it already burned
            # the full 12s budget, so no retry
            raise RpcError(
                f"rpc timeout on {method} ({target}) after {_TIMEOUT}s",
                kind="rpc-timeout") from e
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            if isinstance(e, urllib.error.URLError) and isinstance(
                    getattr(e, "reason", None), TimeoutError):
                raise RpcError(
                    f"rpc timeout on {method} ({target}) after {_TIMEOUT}s",
                    kind="rpc-timeout") from e
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise RpcError(
                f"rpc unreachable on {method} ({target}) after "
                f"{retries} tries: {e}",
                kind="rpc-network") from e
        except ValueError as e:  # json.loads failed -> garbage body
            raise RpcError(
                f"rpc returned invalid JSON on {method} ({target}): {e}",
                kind="rpc-http") from e
        if "error" in payload:
            err = payload.get("error") or {}
            raise RpcError(
                f"rpc error on {method} ({target}): "
                f"{err.get('code')} {err.get('message')}",
                kind="rpc-http") from None
        if "result" not in payload:
            raise RpcError(
                f"rpc malformed response on {method} ({target}): no result",
                kind="rpc-http")
        return payload["result"]
    raise RpcError(  # defensive: loop always exits above
        f"rpc unreachable on {method} ({target})", kind="rpc-network")


def chain_id() -> int:
    """eth_chainId as int (4663 / 0x1237 on mainnet).

    Primary publicnode first; on RpcError falls back to drpc, which
    still serves chainId even though it lacks getLogs/eth_call.
    """
    try:
        return _to_int(post("eth_chainId", []))
    except RpcError as primary:
        try:
            return _to_int(post("eth_chainId", [], url=_fallback_url()))
        except RpcError as secondary:
            raise RpcError(
                f"eth_chainId failed on both endpoints: "
                f"{primary}; fallback: {secondary}",
                kind=primary.kind) from primary


def block_number() -> int:
    """Latest eth_blockNumber as int (drpc fallback like chain_id())."""
    try:
        return _to_int(post("eth_blockNumber", []))
    except RpcError as primary:
        try:
            return _to_int(post("eth_blockNumber", [], url=_fallback_url()))
        except RpcError as secondary:
            raise RpcError(
                f"eth_blockNumber failed on both endpoints: "
                f"{primary}; fallback: {secondary}",
                kind=primary.kind) from primary


def get_block_timestamp(block) -> int:
    """Unix seconds timestamp of a block (eth_getBlockByNumber, primary
    endpoint only - the fallback has no usable block-by-number contract
    here). Accepts int, decimal or 0x-hex string."""
    res = post("eth_getBlockByNumber", [_block_param(block), False])
    if not isinstance(res, dict) or not res.get("timestamp"):
        raise ValueError(f"block {block!r} not found or has no timestamp")
    return _to_int(res["timestamp"])


def call(to: str, data: str, block="latest") -> str:
    """eth_call(to, data) -> hex result string, e.g. totalSupply().
    Primary endpoint only (fallback has no eth_call - verified)."""
    res = post("eth_call", [{"to": (to or "").strip(),
                             "data": (data or "").strip()},
                            _block_param(block)])
    if not isinstance(res, str):
        raise RpcError(
            f"eth_call returned non-string result: {res!r}",
            kind="rpc-http")
    return res


def get_transfers(contract: str, from_block, to_block=None) -> dict:
    """ERC-20 Transfer logs for `contract`, newest-first walk-back.

    Returns {"logs": [...], "window": {...}} - logs are the raw getLogs
    dicts (they carry blockTimestamp directly), sorted ascending by
    (blockNumber, logIndex), re-org-removed entries dropped; the window
    dict carries the walk metadata callers need for honest degradation.

    Why walk: publicnode 403s any getLogs range wider or older than a
    floating ~45-60-block window behind latest. So the range
    [from_block, to_block] (ints, decimal or 0x-hex; to_block None =
    latest) is fetched newest-first in windows starting 48 blocks wide;
    each archive-403 shrinks the width 48->32->16->8 and retries the
    same top block. A 403 at width 8 means older blocks are unreachable
    - the walk stops and reports stopped_reason "window-closed"; it
    never raises on window-403s. The walk also stops after
    _MAX_WALK_REQUESTS requests total (403 misses included) with
    stopped_reason "budget". Non-403 failures (rate limit, network,
    timeout) still raise RpcError - an empty answer must never fake
    success.
    """
    addr = (contract or "").strip()
    if not addr:
        raise ValueError("contract address required")
    to_bn = _to_int(to_block) if to_block is not None else block_number()
    from_bn = _to_int(from_block)
    if from_bn > to_bn:
        raise ValueError(f"from_block {from_bn} above to_block {to_bn}")

    logs: list[dict] = []
    widths_used: list[int] = []
    requests = 0
    widx = 0
    hi = to_bn
    oldest = newest = None
    while True:
        if hi < from_bn:
            reason = "complete"
            break
        if requests >= _MAX_WALK_REQUESTS:
            reason = "budget"
            break
        width = _WALK_WIDTHS[widx]
        lo = max(hi - width + 1, from_bn)
        requests += 1
        try:
            batch = post("eth_getLogs", [{
                "address": addr,
                "topics": [TRANSFER_TOPIC],
                "fromBlock": hex(lo),
                "toBlock": hex(hi),
            }])
        except RpcError as e:
            if getattr(e, "status", None) == 403:
                if widx < len(_WALK_WIDTHS) - 1:
                    widx += 1  # shrink and retry the same top block
                    continue
                reason = "window-closed"
                break
            raise  # non-window failures stay honest and loud
        widths_used.append(width)
        for entry in batch or []:
            if isinstance(entry, dict) and not entry.get("removed"):
                logs.append(entry)
        if newest is None:
            newest = hi
        oldest = lo
        hi = lo - 1

    logs.sort(key=lambda l: (_to_int(l.get("blockNumber") or "0x0"),
                             _to_int(l.get("logIndex") or "0x0")))
    if reason == "complete":
        note = (f"covered blocks {from_bn}..{to_bn} in {requests} "
                f"eth_getLogs request(s), {len(logs)} Transfer log(s)")
    elif reason == "window-closed":
        note = (f"eth_getLogs window closed at block {hi}: 403 archive "
                f"limit even at width {_WALK_WIDTHS[widx]} ({requests} "
                f"request(s)); blocks below {hi} need an explorer source")
    else:
        note = (f"stopped after {requests} eth_getLogs request(s) "
                f"(budget cap); blocks {from_bn}..{hi} not fully covered")
    return {
        "logs": logs,
        "window": {
            "from_block": from_bn,
            "to_block": to_bn,
            "requests": requests,
            "widths_used": widths_used,
            "final_width": _WALK_WIDTHS[widx],
            "oldest_covered": oldest,
            "newest_covered": newest,
            "stopped_reason": reason,  # complete | window-closed | budget
            "note": note,
        },
    }
