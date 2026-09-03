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
5. **`/corporate-actions` response shape was NOT captured live** — no
   fixture exists. `corporate_actions()` therefore accepts both a bare
   JSON list and a dict-wrapped list (first list value found). Confirm the
   real wrapper key on the first live call and tighten if desired. Symbol
   filtering is done locally (query params not contractual).
6. **Unknown symbol on `/prices/{x}` returns 404** — `prices()` maps that
   to `None` (distinguishing "no quote" from "API down" is left to `get()`'s
   exceptions). `asset()` likewise returns `None` for unknown symbols.
