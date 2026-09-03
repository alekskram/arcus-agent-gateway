---
name: arcus-gateway
description: Market data for Robinhood Chain (Arcus) tokenized US equities via the arcus-agent-gateway MCP server. Use for quotes, corporate actions, splits, market status, and sector data on the 194 tokenized stocks. Includes the watchlist/price-tracking cron recipe.
---

# Arcus Gateway (Robinhood Chain tokenized equities)

## What this is

`arcus-agent-gateway` is a read-only, keyless MCP server exposing market data
for the 194 tokenized US equities on Robinhood Chain (Arcus DEX). Prices come
raw from the REST API; multiplier-adjusted values are computed by the server
and always returned alongside the raw ones.

## When to use

- "What's X trading at?" / current bid-ask for a tokenized stock
- Halts, trading capabilities, market-wide status
- Upcoming splits / corporate actions (pending multiplier warnings)
- Sector comparisons (13-sector map) and symbol discovery
- Recurring price tracking of a symbol set → **watchlist recipe below**

Symbols are ticker-style (`AAPL`, `NVDA`). Discover them with `token_list()`
or `search()`; an unknown symbol raises an error that mentions `token_list()`.

## Tools

| Tool | Call | Notes |
|------|------|-------|
| `token_list` | `token_list(status="ACTIVE", limit=100)` | Valid symbols; alphabetical; `limit` ≤ 500. |
| `quote` | `quote(symbol="AAPL")` | Raw + adjusted prices, `is_halted`, multiplier block. |
| `quotes` | `quotes(symbols=["AAPL","NVDA"])` | **Max 20 symbols per call.** Unknowns → `errors`, batch continues. |
| `token_detail` | `token_detail(symbol="AAPL")` | Dossier: contract, ISIN, actions, `warnings` (pending split). |
| `market_status` | `market_status()` | Cheap market health; `halted` lists only cached quotes. |
| `corporate_actions` | `corporate_actions(symbol=None, limit=10)` | Splits/dividends, all or one symbol. |
| `search` | `search(query="apple")` | `apple` → `AAPL`; top 10 with sector + score. |
| `sector_view` | `sector_view()` | 13 sectors; averages populate only after `quotes()` calls. |
| `onchain_info` | `onchain_info(symbol="AAPL")` | Contract/chain 4663/ISIN — v0.2 stub. |

Reading prices: report `bid_raw`/`ask_raw` as the REST price;
`bid_adjusted`/`ask_adjusted` = raw × `multiplier.current`. Always read
adjusted values next to the multiplier (a pending split changes the ratio).

## Watchlist / price tracking

There is deliberately **no watchlist tool**. Tracking = a scheduled
`quotes()` call. Use the agent's cron (e.g. Hermes cron), not a tight loop.

**Rules:**

1. Interval **≥ 5 minutes**. The server's price cache is 15 s and the client
   caps itself at ≤ 50 req/s — polling faster than 5 min adds nothing and
   wastes the upstream budget (60 req/s keyless limit is shared).
2. **≤ 20 symbols per `quotes()` call** — more raises a `ValueError`. Split
   larger watchlists into multiple calls (or multiple cron entries).
3. One cron entry can cover the whole watchlist: `quotes()` accepts the list.

**Example** — track NVDA and TSLA every 5 minutes (Hermes cron):

```json
{
  "name": "watchlist-nvda-tsla",
  "schedule": "*/5 * * * *",
  "prompt": "Call quotes(symbols=[\"NVDA\",\"TSLA\"]) on the arcus MCP server. "
            "Alert me if any quote has is_halted=true or a non-null "
            "multiplier.pending; otherwise stay silent."
}
```

Equivalent plain-cron line if your scheduler prefers shell:

```
*/5 * * * *  your-agent run --tool quotes --args '{"symbols":["NVDA","TSLA"]}'
```

**Watch for in each poll:** `is_halted: true` (halt), a new
`multiplier.pending` (upcoming split — verify with
`token_detail(symbol=...)`), `errors[]` (a symbol was delisted/renamed —
re-check with `token_list()`).

## Caveats

- Read-only market data, **not trading advice**; verify against the official
  source before acting.
- `market_status().halted` and `sector_view()` averages are cache-driven —
  prime with `quotes()` for a real scan.
- `onchain_info` is a stub until v0.2 (no balances/history yet).
