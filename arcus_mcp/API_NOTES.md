# rhj API schema notes (live capture 2026-09-03)

Non-obvious details for anyone building on `arcus_mcp/api.py`. Fixtures:
`tests/fixtures/assets.json`, `tests/fixtures/prices_AAPL.json`.

1. **Every numeric field is a STRING.** `bid: "327.77"`,
   `dailyTradingVolume: "25806784"`, and
   `currentMultiplier: "1.000000000000000000"` — fixed 18-decimal
   strings (token-style scaling, though `tokenDecimals` is also 18).
   `float(x)` before any arithmetic; never `int()` directly.
2. **`pendingMultiplier` can be `""`** — empty string, not `null`/`0`,
   when no multiplier change is queued. Truthiness (`if a["pendingMultiplier"]`)
   is the right liveness check; never parse it unconditionally.
3. **`/prices/{symbol}` wraps the quote in a list** — `{"quotes": [ {...} ]}`
   with exactly one element per fixture. Take `quotes[0]`, but guard for an
   empty list. There is **no name or multiplier in the quote**: a full
   ticker view = join `assets()` ↔ `prices()` on `tokenSymbol`.
4. **`generatedAt` carries nanosecond precision**
   (`2026-09-03T19:43:54.478722091Z`). Python 3.11+ `datetime.fromisoformat`
   handles it after replacing the `Z` suffix with `+00:00` (3.11+ also
   accepts the bare `Z`).
5. **`/corporate-actions` wraps items as `{"corpActions": [...]}`** (live
   capture 2026-09-04, 45 items, fixture
   `tests/fixtures/corporate_actions.json`). Every item: `id`, `type`
   (`CORPORATE_ACTION_TYPE_CASH_DIVIDEND` in the capture), `status`,
   `processDate` (protobuf Timestamp dict `{year, month, day}`), and
   `details.cashDividend.rate` — a per-share STRING amount (`"0.53"`).
   **The API exposes the dividend RATE per share but NO total cash
   amount** (verified 2026-09-04): `_action_row` surfaces it as
   `details.rate`. Symbol filtering is done locally (query params not
   contractual).
6. **Unknown symbol on `/prices/{x}` returns 404** — `prices()` maps that
   to `None` (distinguishing "no quote" from "API down" is left to `get()`'s
   exceptions). `asset()` likewise returns `None` for unknown symbols.
7. **Blockscout v2 requires a browser User-Agent** (live capture
   2026-09-04): plain HTTP clients (curl, urllib with a library UA) get a
   Cloudflare **403 with a "Just a moment…" HTML challenge page** instead of
   JSON. `explorer.py` therefore sends a full browser UA
   (`Mozilla/5.0 … Chrome/128 …`) on **every** request and detects the
   challenge by 403 + `"Just a moment"` in the body → error kind
   `cloudflare`. A non-JSON body on any other status is treated as
   `explorer-http`.
8. **drpc.org fallback has no `eth_getLogs` and no `eth_call`** — both
   return a JSON-RPC error ("method not available", code −32601). It is
   only usable for `eth_chainId` and `eth_blockNumber`, which is exactly
   where `rpc.py` wires the fallback (both endpoints verified 2026-09-04).
9. **publicnode `eth_getLogs` serves only a floating ~45–60-block window
   behind latest** — wider or older ranges return HTTP 403 "Archive
   requests require a personal token" (backend is Alchemy). The window
   **drifts minute to minute**, so a range that worked once may 403 the
   next minute. `get_transfers()` never trusts one wide query: it walks
   back in windows starting 48 blocks wide, shrinking 48→32→16→8 on each
   403 and retrying the same top block, capped at ~14 getLogs requests.
   A 403 even at width 8 means "older blocks unreachable here", reported
   as `stopped_reason: "window-closed"` — **silence beyond the window is
   an access limit, not an absence of transfers** (older history needs
   the explorer's `/tokens/{a}/transfers`).
10. **`eth_getLogs` log objects carry `blockTimestamp` directly** (hex
    seconds, e.g. `"0x6a9af0e3"`) — no per-log `eth_getBlockByNumber`
    round-trip is needed to build timestamps. Fixture:
    `tests/fixtures/rpc_transfer_logs.json`.
11. **`/addresses/{a}/token-balances` is fast on EOAs (~0.5 s) but times
    out on huge contract addresses (40 s+, live-verified 2026-09-04)** —
    the explorer walks every token holding of the address. `explorer.py`
    uses a 15 s timeout and surfaces this as error kind
    `explorer-timeout` (with a hint to try an EOA), rather than stalling
    the caller. Also note: holders/token pages return numerics as raw
    unscaled 18-decimal STRINGS (`total_supply:
    "16871853100370000000000"`) — divide by 10**18 yourself.
