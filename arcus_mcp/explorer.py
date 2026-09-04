"""Stdlib-only client for the Blockscout v2 explorer (Robinhood Chain).

Every request MUST carry a browser User-Agent: without it Cloudflare
answers 403 with a "Just a moment..." HTML challenge (verified live,
see API_NOTES.md). Cached read helpers on top of `_get()`:

    token(address)                GET /tokens/{a}                (TTL 600s)
    token_holders(address, limit) GET /tokens/{a}/holders        (TTL 600s)
    address_token_balances(a)     GET /addresses/{a}/token-balances
                                                                 (TTL 600s)

Schema gotchas (live capture 2026-09-04, fixtures tests/fixtures/
explorer_{token,holders,token_balances}.json): numerics arrive as
STRINGS ("62882" holders, total_supply is a raw unscaled 18-dec string
like "16871853100370000000000" - divide by 10**18 yourself). Holders
come wrapped as {"items": [...], "next_page_params": {...}}; we fetch
ONE page only (max 50 rows, no pagination loops). token-balances is a
flat LIST of {"token": {...}, "value": "..."} rows - fast on EOAs but
hangs 40s+ on huge contract addresses, hence the 15s timeout and the
"explorer-timeout" error kind.

Errors: ExplorerError with .kind in {"cloudflare", "explorer-timeout",
"explorer-http", "explorer-network"} (mirrors the rpc.py taxonomy);
404 maps to None ("unknown token/address"), never a silent empty list.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

BASE = "https://robinhoodchain.blockscout.com/api/v2"
# Browser UA is MANDATORY: plain clients get a Cloudflare 403 challenge.
_USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
_HEADERS = {"User-Agent": _USER_AGENT, "Accept": "application/json"}

_TIMEOUT = 15.0   # token-balances on big contracts hangs 40s+ -> fail honest
_RETRIES = 3      # 1 attempt + 2 retries, on 5xx/network only
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

_TTL = 600.0
_TOKEN_CACHE: dict[str, tuple[float, dict | None]] = {}
_HOLDERS_CACHE: dict[str, tuple[float, dict | None]] = {}
_BALANCES_CACHE: dict[str, tuple[float, list | None]] = {}
_CACHE_LOCK = threading.Lock()


class ExplorerError(RuntimeError):
    """Explorer call failed; .kind picks the failure class.

    kind: "cloudflare"        - 403 + "Just a moment" HTML challenge
          "explorer-timeout"  - no answer within 15s (not retried)
          "explorer-http"     - final non-retryable HTTP status
          "explorer-network"  - unreachable after retries
    """

    def __init__(self, kind: str, message: str,
                 status: int | None = None, hint: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.hint = hint

    def to_dict(self) -> dict:
        """Flat error dict for MCP tool payloads (kind + hint, never silent)."""
        return {"error": {"kind": self.kind, "hint": self.hint or "",
                          "message": str(self)}}


def _base() -> str:
    """Base URL, overridable via ARCUS_EXPLORER_URL (no trailing slash)."""
    return os.environ.get("ARCUS_EXPLORER_URL", BASE).rstrip("/")


def _is_timeout(e: BaseException) -> bool:
    """True for socket timeouts, incl. ones wrapped in URLError.reason."""
    reason = getattr(e, "reason", None)
    if isinstance(e, TimeoutError) or isinstance(reason, TimeoutError):
        return True
    return "timed out" in str(e).lower() or "timed out" in str(reason or "")


def _get(path: str, params: dict | None = None) -> dict | list | None:
    """GET {_base()}{path}[?params] with the browser UA on every request.

    Retries (2x, backoff 2s*(n+1)) on 5xx and network errors only;
    timeouts and Cloudflare challenges fail immediately and honestly.
    Raises ExplorerError; returns parsed JSON (None for 404).
    """
    qs = ""
    if params:
        qs = "?" + "&".join(f"{k}={v}" for k, v in params.items()
                            if v is not None)
    url = f"{_base()}{path}{qs}"
    for attempt in range(_RETRIES):
        req = urllib.request.Request(url, headers=_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            except Exception:
                body = b""
            if b"Just a moment" in body:  # Cloudflare challenge page
                raise ExplorerError(
                    "cloudflare",
                    f"explorer Cloudflare challenge for {url} (HTTP {e.code})",
                    status=e.code,
                    hint="Cloudflare blocked the request - browser "
                         "User-Agent required") from e
            if 500 <= e.code <= 599 and attempt < _RETRIES - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            if e.code == 404:
                return None
            raise ExplorerError(
                "explorer-http",
                f"explorer {e.code} for {url} "
                f"(after {attempt + 1} attempt(s))",
                status=e.code,
                hint=e.reason or "upstream explorer error") from e
        except json.JSONDecodeError as e:
            raise ExplorerError(
                "explorer-http",
                f"explorer returned non-JSON for {url}",
                hint="expected JSON - possibly an HTML challenge page"
                ) from e
        except Exception as e:  # URLError, ConnectionError, OSError, timeouts
            if _is_timeout(e):
                raise ExplorerError(
                    "explorer-timeout",
                    f"explorer timed out after {_TIMEOUT:.0f}s: {url}",
                    hint="token-balances on contract addresses can hang "
                         "40s+; try an EOA or retry later") from e
            if attempt < _RETRIES - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise ExplorerError(
                "explorer-network",
                f"explorer unreachable after {_RETRIES} tries: {url}",
                hint="check network/upstream status") from e
    raise ExplorerError("explorer-network", f"explorer unreachable: {url}")


def _norm_address(address: str) -> str:
    """Lowercased 0x-hex address; ValueError on malformed input."""
    a = (address or "").strip().lower()
    if not _ADDRESS_RE.match(a):
        raise ValueError(f"not a contract/wallet address: {address!r}")
    return a


def token(address: str) -> dict | None:
    """Raw /tokens/{a} dict (600s cache); None if the token is unknown.

    {"address_hash", "symbol": "AAPL", "name", "type": "ERC-20",
     "decimals": "18", "total_supply": "16871853100370000000000",
     # ^ RAW unscaled STRING - divide by 10**18 yourself
     "holders_count": "62882", "circulating_market_cap": "4864702.46",
     "exchange_rate": "321.06", "volume_24h": "...", "icon_url": ...}
    """
    key = _norm_address(address)
    with _CACHE_LOCK:
        cached = _TOKEN_CACHE.get(key)
        if cached and time.monotonic() - cached[0] <= _TTL:
            return cached[1]
    raw = _get(f"/tokens/{key}")
    out = raw if isinstance(raw, dict) else None
    with _CACHE_LOCK:
        _TOKEN_CACHE[key] = (time.monotonic(), out)
    return out


def token_holders(address: str, limit: int = 50) -> dict | None:
    """One page of /tokens/{a}/holders (600s cache); None if unknown.

    Single page ONLY (no pagination loops): always fetched at the max
    page size (items_count=50), cached per address, then sliced to
    `limit` (clamped 1..50) locally - any limit is served from ONE
    cached fetch per address. {"items": [{"address": "0x..",
    "is_contract": true, "value": "6155456228534021317261",  # raw STRING
    "token_id": null}, ...], "next_page_params": {...}|None} - the
    params are echoed so callers KNOW a next page exists.
    """
    key = _norm_address(address)
    n = max(1, min(50, int(limit)))
    with _CACHE_LOCK:
        cached = _HOLDERS_CACHE.get(key)
        if cached and time.monotonic() - cached[0] <= _TTL:
            page = cached[1]
            return {"items": page["items"][:n],
                    "next_page_params": page["next_page_params"]} if page else None
    raw = _get(f"/tokens/{key}/holders", {"items_count": 50})
    page: dict | None = None
    if isinstance(raw, dict):
        items = []
        for it in raw.get("items", [])[:50]:
            addr = it.get("address") or {}
            items.append({
                "address": addr.get("hash"),
                "is_contract": bool(addr.get("is_contract")),
                "value": it.get("value"),
                "token_id": it.get("token_id"),
            })
        page = {"items": items,
                "next_page_params": raw.get("next_page_params")}
    with _CACHE_LOCK:
        _HOLDERS_CACHE[key] = (time.monotonic(), page)
    if page is None:
        return None
    return {"items": page["items"][:n],
            "next_page_params": page["next_page_params"]}


def address_token_balances(address: str) -> list | None:
    """Flat /addresses/{a}/token-balances list (600s cache); None if 404.

    [{"token": {full /tokens/{a} dict, "address_hash", "symbol",
      "exchange_rate", ...}, "value": "229942366437633735816",
      # ^ RAW unscaled STRING - /10**18 for token units
      "token_id": null}, ...] - token_instance dropped (bulky, null
    on ERC-20). Fast on EOAs (~0.5s); on huge contracts the endpoint
    hangs 40s+, so the 15s timeout raises ExplorerError
    kind="explorer-timeout" instead of stalling the caller.
    """
    key = _norm_address(address)
    with _CACHE_LOCK:
        cached = _BALANCES_CACHE.get(key)
        if cached and time.monotonic() - cached[0] <= _TTL:
            return cached[1]
    raw = _get(f"/addresses/{key}/token-balances")
    out: list | None = None
    if isinstance(raw, list):
        out = [{"token": it.get("token") or {},
                "value": it.get("value"),
                "token_id": it.get("token_id")}
               for it in raw if isinstance(it, dict)]
    with _CACHE_LOCK:
        _BALANCES_CACHE[key] = (time.monotonic(), out)
    return out
