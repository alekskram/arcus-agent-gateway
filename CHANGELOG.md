# Changelog

## 0.1.0 (2026-09-04)

Initial release.

- 9 read-only MCP tools for 194 tokenized US equities on Robinhood Chain
  (Arcus DEX): `token_list`, `quote`, `quotes`, `token_detail`,
  `market_status`, `corporate_actions`, `search`, `sector_view`,
  `onchain_info` (v0.2 stub)
- Multiplier handling: raw and adjusted price fields side by side,
  pending-split warnings (effective time), per-token multiplier history note
- Caching (300s / 15s / 3600s), client rate limit (50 req/s cap), 429 retry
  with backoff
- Honest degradation: unknown symbol errors point at `token_list()`;
  `market_status()` labels its estimates; `onchain_info` is a marked stub
- 75 offline tests including spec invariants (adjusted == raw x multiplier,
  spread >= 0, halted propagation, batch limits)
