# Changelog

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
  concurrent); 10 cold symbols ≈ 0.6–1 s (was ~3.1 s serialized). Global
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
