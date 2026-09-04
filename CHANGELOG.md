# Changelog

## 0.2.1 (2026-09-04)

- **tests/test_online.py**: opt-in online suite (7 live sanity checks:
  quote, token_list, corporate_actions, holder_snapshot,
  transfer_history, price_history degradation, market_status);
  deselected by default via existing addopts, run with
  `pytest tests/test_online.py -m online`; honest
  skip-on-degradation, never a false fail. Not in CI by design.

## 0.2.0 (2026-09-04)

On-chain layer: three new read-only tools reading the keyless public RPC
and the Blockscout v2 explorer, with honest per-field degradation.

- **onchain_info** (extended): keeps the v0.1 fields (contract, chain id,
  network, decimals, ISIN) and adds `total_supply` via `totalSupply()`
  eth_call (source `rpc`), `holders_count` + `circulating_market_cap`
  (source `explorer`), and a `supply_crosscheck` of the REST-implied
  capitalization (`total_supply × multiplier × mid`) vs the explorer's —
  a >1% divergence lands in `warnings[]`. Every derived field carries a
  per-field `source` tag; each of the three sources fails independently
  to a warning, never silently.
- **holder_snapshot** (`symbol, limit=20`): top holders from the
  explorer (one page, max 50 rows, 600 s cache) with rows `address`,
  `value` (raw/1e18), `share_pct` = value / total_supply × 100,
  `is_contract`; total_supply from the RPC with an explorer fallback
  (source-tagged), `share_pct: null` + warning when no supply is
  available at all.
- **wallet_holdings** (`address`): explorer `token-balances` intersected
  with the 194-token `assets()` universe; `est_position_usd` and
  `portfolio_usd_total` computed ONLY from quotes already in the price
  cache (no quote fan-out), with an explanatory note when quotes are
  missing or stale. Cached 120 s.
- **transfer_history** (`symbol, limit=25, min_value=None`): recent
  ERC-20 Transfer events via the public RPC's adaptive walk-back
  (windows start 48 blocks wide, shrink 48→32→16→8 on archive-403s,
  ≤14 getLogs requests). Rows: `ts` (ISO from the log's own
  `blockTimestamp`), `from`, `to`, `value` (raw/1e18), `tx_hash`,
  `block`; `min_value` filters in token units; a window exhausted with
  0 logs produces an explicit note pointing at the explorer's transfer
  list. Cached 60 s.
- **New stdlib clients**: `arcus_mcp/rpc.py` (JSON-RPC with 429/5xx
  retry + backoff, drpc fallback for chainId/blockNumber, RpcError
  taxonomy `rpc-timeout|rpc-http|rpc-network|rpc-limit`) and
  `arcus_mcp/explorer.py` (Blockscout v2 with the mandatory browser
  User-Agent — plain clients get a Cloudflare 403 challenge —
  ExplorerError taxonomy `cloudflare|explorer-timeout|explorer-http|
  explorer-network`).
- Version bumped to 0.2.0 (FastMCP + pyproject).

## 0.1.1 (2026-09-04)

- **price_history tool** (`symbol`, `timeframe="daily"|"raw"`, `limit=90`):
  OHLCV bars from the optional recorder's local parquet store; honest
  degradation (missing pyarrow / no data → actionable error dict, never a
  silent empty list).
- **Optional price-history recorder** (`scripts/recorder.py`, opt-in,
  disabled by default): 5-minute snapshot loop over ACTIVE tradable tokens
  (~194) into monthly `snapshots_YYYYMM.parquet` + idempotent daily OHLCV
  rollup (`volume` = max of cumulative daily volume). pyarrow is an optional
  `[recorder]` extra; systemd units ship in `deploy/` **disabled**.
- **Parallel quotes**: `quotes()` fans out with an asyncio semaphore (8
  concurrent); 10 cold symbols ≈ 0.3–0.4 s typical (was ~3.1 s serialized;
  live-verified 2026-09-04). An occasional upstream 429 under the burst
  costs one 2 s backoff retry - all results still returned. Global
  50 req/s client cap unchanged.
- **search() limit** parameter (default 10, cap 50).
- **sector_view(warm=False, sector=None)**: `warm=True` fans out fresh
  quotes for one sector (or all, with a cost warning) and reports
  `warmed` + `requests_made`; default stays cache-only.
- **Dividend rates**: `/corporate-actions` live-verified (2026-09-04) —
  exposes per-share `details.cashDividend.rate` ("0.53"), no total cash
  amount; surfaced as `details.rate` in action rows.
- Tests: 75 → 99 offline (recorder append/rollup, price_history
  degradation, quotes parallelism timing, search limit, sector warm,
  dividend rate mapping).

## 0.1.0 (2026-09-03)

- Initial release: 9 read-only MCP tools (token_list, quote, quotes,
  token_detail, market_status, corporate_actions, search, sector_view,
  onchain_info), stdlib REST client with caching/rate-limit/retries,
  validated 13-sector map, 75 offline tests, live-smoked (19/19, 49/49).
