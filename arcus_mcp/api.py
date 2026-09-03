"""Stdlib-only client for Robinhood Chain (Arcus, rhj) public REST API.

No keys, no POSTs - GET only. Cached read helpers on top of `get()`:

    assets()            GET /assets                     (TTL 300s)
    asset(symbol)       -> entry from assets()           (cached)
    prices(symbol)      GET /prices/{SYMBOL}             (TTL 15s)
    corporate_actions() GET /corporate-actions           (TTL 3600s)

Schema gotchas (live capture 2026-09-03, see tests/fixtures/ and
API_NOTES.md): all numerics arrive as STRINGS ("327.77",
currentMultiplier with 18 decimals), pendingMultiplier can be "" while
live, prices come as {"quotes": [one element]} with no name/multiplier -
join with assets() on tokenSymbol.
"""
import json
import threading
import time
import urllib.error
import urllib.request

BASE = "https://api.robinhood.com/rhj/"
UA = {"User-Agent": "arcus-agent-gateway/0.1"}
_last_req = 0.0
_MIN_INTERVAL = 0.02  # >=20ms between requests: 50 req/s cap
_LOCK = threading.Lock()

_ASSETS_TTL = 300.0
_PRICES_TTL = 15.0
_ACTIONS_TTL = 3600.0
_ASSETS_CACHE: tuple[float, list] = (float("-inf"), [])  # -inf: always cold at start
_PRICES_CACHE: dict[str, tuple[float, dict]] = {}
_ACTIONS_CACHE: tuple[float, list] = (float("-inf"), [])
_CACHE_LOCK = threading.Lock()


def get(path: str, params: dict | None = None, retries: int = 3) -> dict | list:
    """GET {BASE}{path}[?params] with rate limiting and retries.

    Backs off 2s*(n+1) on 429/502/503/504 and network errors; raises
    ValueError on a final HTTP error and RuntimeError when unreachable.
    """
    global _last_req
    qs = ""
    if params:
        qs = "?" + "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    url = f"{BASE}{path}{qs}"
    for attempt in range(retries):
        with _LOCK:  # thread-safe politeness window
            wait = _MIN_INTERVAL - (time.monotonic() - _last_req)
            if wait > 0:
                time.sleep(wait)
            _last_req = time.monotonic()
        req = urllib.request.Request(url, headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            hint = ("rate limited - retry after backoff" if e.code == 429
                    else e.reason)
            raise ValueError(
                f"rhj {e.code} for {url}: {hint} "
                f"(after {attempt + 1} attempt(s))") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            # URLError, socket timeouts and connection resets are retryable
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise RuntimeError(
                f"rhj unreachable after {retries} tries: {url} - "
                "check network/upstream status") from e
    raise RuntimeError(f"rhj unreachable: {url}")  # defensive: loop always exits above


def _norm(symbol: str) -> str:
    """Uppercase-stripped symbol, safe against None/garbage input."""
    return (symbol or "").strip().upper()


def assets() -> list:
    """Raw /assets list (300s cache).

    [{"id", "tokenSymbol": "AAPL", "tokenName", "deployments": [
    {"contractAddress", "chainId": 4663, "networkName"}],
    "currentMultiplier": "1.000000000000000000",  # 18-dp STRING
    "pendingMultiplier": "",  # "" while nothing is queued (STRING)
    "status": "ASSET_STATUS_ACTIVE", "tradingCapabilities": {
    "market"/"extended"/"overnight": {"whole", "fractional":
    "TRADING_STATUS_TRADABLE"|"TRADING_STATUS_UNTRADABLE"}},
    "tokenDecimals": 18, "isin": "US02079K3059"}, ...]
    """
    global _ASSETS_CACHE
    with _CACHE_LOCK:
        now = time.monotonic()
        if now - _ASSETS_CACHE[0] > _ASSETS_TTL:
            raw = get("assets")
            # tolerate both {"assets": [...]} and a bare list
            _ASSETS_CACHE = (now, raw.get("assets", []) if isinstance(raw, dict) else raw)
        return _ASSETS_CACHE[1]


def asset(symbol: str) -> dict | None:
    """Single asset by tokenSymbol, case-insensitive; None if unknown.

    Returned dict shape: see assets(). Example:
    {"tokenSymbol": "AAPL", "currentMultiplier": "1.000000000000000000",
     "pendingMultiplier": "", "status": "ASSET_STATUS_ACTIVE", ...}
    """
    want = _norm(symbol)
    if not want:
        return None
    for a in assets():
        if a.get("tokenSymbol", "").upper() == want:
            return a
    return None


def prices(symbol: str) -> dict | None:
    """First quote from /prices/{symbol} (15s cache); None if no quote.

    {"tokenSymbol": "AAPL", "bid": "327.77", "ask": "327.78",
    "currency": "USD", "dailyTradingVolume": "25806784",
    "isTradingHalt": false, "generatedAt": "2026-09-03T19:43:54.478722091Z",
    "dailyHigh": "330.81", "dailyLow": "323.35",
    "mintBurnTokenVolume": "3445.86136657",
    "mintBurnUsdVolume": "1129467.20942748175"}  # numerics are STRINGS;
    no name/multiplier here - join with asset() on tokenSymbol.
    """
    want = _norm(symbol)
    if not want:
        return None
    with _CACHE_LOCK:
        cached = _PRICES_CACHE.get(want)
        if cached and time.monotonic() - cached[0] <= _PRICES_TTL:
            return cached[1]
    try:
        raw = get(f"prices/{want}")
    except ValueError as e:
        # 404 = symbol without a quote right now, not an outage: surface
        # None so callers don't have to distinguish the two
        if getattr(e.__cause__, "code", None) == 404:
            return None
        raise
    quotes = raw.get("quotes", []) if isinstance(raw, dict) else []
    quote = quotes[0] if quotes else None
    with _CACHE_LOCK:
        if quote is not None:
            _PRICES_CACHE[want] = (time.monotonic(), quote)
        else:
            _PRICES_CACHE.pop(want, None)  # negative answers stay uncached
    return quote


def corporate_actions(symbol: str | None = None, limit: int = 10) -> list:
    """Corporate actions, newest-relevant first, capped at `limit`
    (3600s cache). With `symbol`, filtered to that tokenSymbol
    (case-insensitive); the endpoint is fetched unfiltered and filtered
    locally - its query parameters are not contractual.

    [{"tokenSymbol": "AAPL", ... action-specific fields ...}, ...] -
    tolerant to a bare list or a dict with an unknown list key, since
    the live wrapper key was not confirmed.
    """
    want = _norm(symbol) if symbol else ""
    out: list = []
    global _ACTIONS_CACHE
    with _CACHE_LOCK:
        now = time.monotonic()
        if now - _ACTIONS_CACHE[0] > _ACTIONS_TTL:
            raw = get("corporate-actions")
            items = raw if isinstance(raw, list) else next(
                (v for v in raw.values() if isinstance(v, list)), [])
            _ACTIONS_CACHE = (now, items)
        items = _ACTIONS_CACHE[1]
    for it in items:
        if not want or it.get("tokenSymbol", "").upper() == want:
            out.append(it)
            if len(out) >= limit:
                break
    return out
