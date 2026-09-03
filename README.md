# arcus-agent-gateway

An MCP (Model Context Protocol) server that gives AI agents read-only, keyless
access to market data for the **194 tokenized US equities** on **Robinhood Chain
(Arcus)** — quotes, corporate actions, trading capabilities, multipliers and a
13-sector map. No API keys, no auth, no writes: every tool is a GET against the
public `api.robinhood.com/rhj` REST surface, cached and rate-limited so an
enthusiastic agent can't hammer the upstream.

## Quickstart

Run over stdio (the default, for local agents):

```bash
uvx arcus-agent-gateway
```

Claude Desktop / Cursor config (`claude_desktop_config.json` or
`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "arcus": {
      "command": "uvx",
      "args": ["arcus-agent-gateway"]
    }
  }
}
```

Hosted form — streamable HTTP on port **8902**:

```bash
uvx arcus-agent-gateway --http               # 127.0.0.1:8902
curl http://127.0.0.1:8902/health            # -> {"ok": true, "service": "arcus-agent-gateway"}
```

## Tools

All 9 tools are read-only (annotated `readOnlyHint: true`). Names and
parameters are exactly as registered by `arcus_mcp/server.py`.

| # | Tool | Signature | What it does |
|---|------|-----------|--------------|
| 1 | `token_list` | `token_list(status="ACTIVE", limit=100)` | Tokenized equities, one row per token (symbol, name, status, multiplier, tradable); `status` filters the `ASSET_STATUS_*` prefix, `'ALL'` disables. Start here for valid symbols. |
| 2 | `quote` | `quote(symbol)` | Live quote joined with asset metadata: raw + multiplier-adjusted bid/ask/spread, `is_halted`, trading capabilities, multiplier block. Unknown symbol raises with a pointer to `token_list()`. |
| 3 | `quotes` | `quotes(symbols)` | Batch of `quote()` rows, **max 20 per call** (more raises). Unknown symbols land in `errors` without failing the batch. |
| 4 | `token_detail` | `token_detail(symbol)` | Full dossier: contract/chain/ISIN metadata, embedded quote, last 5 corporate actions, multiplier block with history note, `warnings` (pending split). |
| 5 | `market_status` | `market_status()` | Market-wide health from assets only (never fetches 194 prices): totals, untradable count, cached-halted list, extended-hours estimate. |
| 6 | `corporate_actions` | `corporate_actions(symbol=None, limit=10)` | Splits/dividends across all tokens or for one symbol; tolerant to the API's field-name variants. |
| 7 | `search` | `search(query)` | Local fuzzy search over the token list; `apple` → `AAPL`; top 10 with scores and sectors. |
| 8 | `sector_view` | `sector_view()` | 13-sector static map with sizes and — once quotes are cached — multiplier-adjusted sector averages. Makes no requests. |
| 9 | `onchain_info` | `onchain_info(symbol)` | Contract address, chain id (4663, Robinhood Chain), network, decimals, ISIN. v0.2 stub — full on-chain data (balances, Transfer/Split history) is planned for v0.2. |
| 10 | *watchlist* | — | Not a tool. Price tracking is done by your agent's scheduler (cron) calling `quotes()` on an interval — see [`.agents/skills/arcus-gateway/SKILL.md`](.agents/skills/arcus-gateway/SKILL.md). |

## Multiplier logic (read this before using prices)

Robinhood Chain tokens carry a **multiplier** — the corporate-action
adjustment factor for the token contract (`1.0` = untouched). Splits change it;
for example NVDA's 2026-11 split queues `pendingMultiplier: "4.0"`.

- **The REST API returns RAW prices.** `bid`/`ask` from `/prices/{symbol}` are
  in token-contract units and are **not** multiplier-adjusted.
- **Adjusted values are computed by this server**, never taken from upstream:
  `price_adjusted = round(price_raw × currentMultiplier, 6)`.
- **Raw and adjusted always travel together.** Every quote carries
  `bid_raw`/`ask_raw`/`spread_raw` *and* `bid_adjusted`/`ask_adjusted`/
  `mid_adjusted` next to the `multiplier` block — never one without the other.
- On-chain quantities (token balances, mint/burn volumes) are natively in
  adjusted (multiplied) units; REST prices are not. If you compare the two,
  go through the `*_adjusted` fields.

Worked example (live fixture, 2026-09-03):

```
AAPL   currentMultiplier = 1.000566080061092436
       bid_raw   = 327.77   →  bid_adjusted = round(327.77 × 1.000566…, 6) = 327.955544
       ask_raw   = 327.78   →  ask_adjusted = 327.965550
       mid                      mid_adjusted = 327.960547
```

**Pending split warning.** When `pendingMultiplier` is queued (non-empty) and
differs from the current one, `token_detail()` adds a warning like
`pending split: 1→4.0 on 2026-11-06T00:00:00Z`, and `quote()`'s multiplier
block exposes `pending` + `effective_time`. After the split lands, raw prices
jump by the ratio while `*_adjusted` fields stay comparable — another reason to
always read adjusted values next to the multiplier.

## API limits & caching

- Upstream allows **60 req/s without a key**; this client self-limits to
  **≤ 50 req/s** (a 20 ms politeness interval between requests, thread-safe).
- Transient failures (`429/502/503/504`, network errors) are retried up to 3
  times with `2s × (attempt+1)` backoff.
- Response caches (per process): `/assets` **5 min**, `/prices/{symbol}`
  **15 s**, `/corporate-actions` **1 h**. `market_status()` and `sector_view()`
  are computed from caches and assets only — they never fan out 194 price
  requests.

## Raw prices disclaimer

Prices are served **exactly as they arrive from Robinhood (RAW)** — they are
*not* multiplier-adjusted, and the `*_adjusted` fields are **our computation**,
not upstream data. All data is for information only, **not for trading
decisions**, and should be verified against the official source before you act
on it. No warranty of completeness, accuracy or timeliness.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q                      # 72 tests, all offline (fixtures + mocks)
.venv/bin/python scripts/smoke_api_offline.py     # REST client smoke, zero HTTP
.venv/bin/python scripts/smoke_server_offline.py  # 9-tool server smoke, zero HTTP
```

Layout: `arcus_mcp/api.py` (stdlib REST client), `arcus_mcp/server.py` (9 MCP
tools + FastMCP wiring), `arcus_mcp/sectors.py` (validated 13-sector map),
`arcus_mcp/paths.py` (state dir; override with `ARCUS_GATEWAY_DATA`).
Live-captured schema fixtures live in `tests/fixtures/` with notes in
`arcus_mcp/API_NOTES.md`. Usage scenarios: [`examples/use-cases.md`](examples/use-cases.md).
