# Use cases: 8 agent scenarios

Each scenario: the user's question → the tool(s) to call → an example call →
abbreviated output → what to look at. Output snippets are abbreviated JSON;
single-quote values (scenario 1) are real fixture data from the live capture
of 2026-09-03, and scenarios 6-8 (on-chain holder, wallet and transfer
reads) are real captures from the live on-chain session of 2026-09-04 —
batch and sector examples are illustrative (only AAPL has a captured price
fixture). Live numbers will differ.

---

## 1. "What's Apple trading at?" → `quote("AAPL")`

```python
quote(symbol="AAPL")
```

```json
{
  "symbol": "AAPL",
  "name": "Apple • Robinhood Token",
  "bid_raw": 327.77, "ask_raw": 327.78, "spread_raw": 0.01,
  "bid_adjusted": 327.955544, "ask_adjusted": 327.96555, "mid_adjusted": 327.960547,
  "daily_volume": 25806784,
  "is_halted": false,
  "trading_capabilities": {"fractional": true, "all_day": true, "extended_hours": true},
  "multiplier": {"current": 1.000566, "pending": null, "effective_time": null},
  "generated_at": "2026-09-03T19:43:54.478722091Z"
}
```

**What to look at:** `is_halted` first (halted tokens still quote stale
prices). Report `*_raw` as the REST price and `*_adjusted` (multiplied by
`multiplier.current`) when comparing with on-chain amounts — they are always
returned together. `spread_raw` = ask − bid.

---

## 2. "Show me all halted stocks" → `market_status()` + `quotes()`

`market_status().halted` only scans quotes **already cached** in this process —
it's cheap (zero requests) but opportunistic. For a real scan, quote the
sectors you care about first, then re-read status:

```python
market_status()
# -> {"total_tokens": 194, "active": 194, "untradable": 3,
#     "halted": [], "extended_hours_open": true, ...,
#     "note": "halted is opportunistic ... for a full halt scan call
#              quotes() on the symbols you care about (max 20 per call)"}

quotes(symbols=["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META",
                "AMD", "NFLX", "AVGO", "INTC", "QCOM", "MU", "ORCL", "CRM",
                "ADBE", "CSCO", "IBM", "SMCI", "JBL"])   # your top-20, max 20
market_status()   # -> {"halted": ["NVDA"], ...}  if NVDA quoted halted
```

A halted row inside `quotes()`:

```json
{"quotes": [{"symbol": "NVDA", "is_halted": true,
             "bid_raw": 88.10, "ask_raw": 88.14, "...": "..."}],
 "errors": []}
```

**What to look at:** `is_halted: true` is the prominent top-level flag on
every quote. Batch unknown symbols land in `errors` instead of failing the
call. More than 20 symbols per `quotes()` call raises — split the batch.

---

## 3. "Any upcoming splits?" → `corporate_actions()` + `token_detail`

```python
corporate_actions(limit=10)
token_detail(symbol="SPLT")     # suspect token -> dossier with warnings
```

```json
{
  "symbol": "SPLT",
  "multiplier": {"current": 1.0, "pending": 4.0,
                 "effective_time": "2026-11-06T00:00:00Z"},
  "warnings": ["pending split: 1→4.0 on 2026-11-06T00:00:00Z"],
  "corporate_actions": [
    {"symbol": "SPLT", "type": "SPLIT", "status": "PENDING",
     "process_date": "2026-11-06T00:00:00Z",
     "details": {"old_rate": 1.0, "new_rate": 4.0}}
  ]
}
```

**What to look at:** `warnings` — a pending multiplier change always surfaces
there as `pending split: 1→X on DATE` (this is the spec-mandated warning).
`multiplier.pending` is non-null only while a change is queued. After the
split lands, `bid_raw` jumps ×4 while `bid_adjusted` stays comparable — cite
both.

---

## 4. "Compare Tech vs Finance sector" → `quotes()` then `sector_view()`

`sector_view()` computes averages **only from cached quotes** — prime the two
sectors first (each ≤ 20 symbols; Tech has 50 → two calls):

```python
quotes(symbols=["AAPL", "MSFT", "GOOGL", "META", "AMZN", "NFLX", "..."])  # Tech pt.1
quotes(symbols=["IBM", "SNOW", "DDOG", "..."])                            # Tech pt.2
quotes(symbols=["NU", "SOFI", "FUTU", "FIG", "P", "NAVN", "CRCL", "FISV", "INFQ"])
sector_view()
```

```json
{
  "sectors": {
    "Tech":          {"symbols": 50, "quoted": 20,
                      "avg_bid_adjusted": 312.447100, "avg_ask_adjusted": 312.457290,
                      "halted": []},
    "Finance/Fintech": {"symbols": 9, "quoted": 9,
                        "avg_bid_adjusted": 41.872215, "avg_ask_adjusted": 41.881004,
                        "halted": []}
  },
  "note": "call quotes([...sector symbols...]) first to populate live averages
           (max 20 per call); averages are multiplier-adjusted"
}
```

**What to look at:** `quoted` vs `symbols` — if `quoted` is 0 the averages
are `null` (cold cache), not "sector died". Averages are already
multiplier-adjusted, so they're directly comparable across tokens. Sector
names come from the 13-sector map in `sectors.py` (Tech, Semiconductors,
Finance/Fintech, …).

---

## 5. "Track NVDA and TSLA every 5 minutes" → cron + `quotes()`

There is **no watchlist tool** by design. Price tracking = your agent's
scheduler calling `quotes()` on an interval. Full recipe (cron line, limits,
do/don't) lives in the skill: [`.agents/skills/arcus-gateway/SKILL.md`](../.agents/skills/arcus-gateway/SKILL.md).

```python
# every 5 minutes, from the agent's cron:
quotes(symbols=["NVDA", "TSLA"])
```

```json
{"quotes": [
  {"symbol": "NVDA", "bid_raw": 88.10, "bid_adjusted": 88.10,
   "is_halted": false, "multiplier": {"current": 1.0, "pending": null}},
  {"symbol": "TSLA", "bid_raw": 213.55, "bid_adjusted": 213.55,
   "is_halted": false, "multiplier": {"current": 1.0, "pending": null}}],
 "errors": []}
```

**What to look at:** interval ≥ 5 min (prices cache is 15 s — tighter polling
just burns your upstream budget), ≤ 20 symbols per call, and the 15-second
price cache means repeated calls inside one poll are free. Alert on
`is_halted: true` or a `multiplier.pending` appearing mid-watch.

---

## 6. "How concentrated is AAPL ownership?" → `holder_snapshot("AAPL")`

```python
holder_snapshot(symbol="AAPL", limit=3)
```

```json
{
  "symbol": "AAPL",
  "contract": "0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9",
  "count": 3,
  "holders": [
    {"address": "0x8366a39CC670B4001A1121B8F6A443A643e40951",
     "value": 6164.89213, "share_pct": 35.366316, "is_contract": true},
    {"address": "0x498752D5fa0600CBd613074C151Abe15B3FeC7CB",
     "value": 3185.821363, "share_pct": 18.276194, "is_contract": true},
    {"address": "0xAae0d815EE56e4092a5E5C2911E676Fea50B2d6D",
     "value": 915.887898, "share_pct": 5.254201, "is_contract": true}
  ],
  "total_supply": 17431.536275,
  "total_supply_source": "rpc",
  "next_page": true,
  "note": "page capped at 50 rows by design (no pagination loops); more holders exist beyond this page"
}
```

**What to look at:** the top 3 alone hold ~58.9% of supply
(35.366316 + 18.276194 + 5.254201 = 58.896711) — `share_pct` is
`value / total_supply * 100`, so it's directly comparable across tokens.
`is_contract: true` on **every** top holder means this concentration sits
in contracts (custody, treasury, AMM-style infrastructure), **not**
individual whales — an EOA (`is_contract: false`) in the top ranks would be
the actual "whale" signal. `total_supply` is source-tagged
(`total_supply_source: "rpc"`, with explorer fallback): when supply is
unavailable from both sources, every `share_pct` is `null` and a warning
fires — the holder rows themselves are still fresh.

---

## 7. "What's in this wallet?" → `wallet_holdings(address)`

Pick the address from a fresh transfer (the scenario 8 flow), then `quote()`
the symbols **first** to warm the price cache — `est_position_usd` /
`portfolio_usd_total` are computed **only** from quotes already cached in
this process (cache-only, no fan-out):

```python
quote(symbol="AAPL")   # warm the cache first (15 s TTL)
# -> {"bid_raw": 320.55, "ask_raw": 320.57, "mid_adjusted": 320.741463,
#     "generated_at": "2026-09-04T19:56:09Z", ...}
wallet_holdings(address="0xb8cdbcbc6f6f271906fcffd9235850b3b66cf653")
```

```json
{
  "address": "0xb8cdbcbc6f6f271906fcffd9235850b3b66cf653",
  "count": 1,
  "holdings": [
    {"symbol": "AAPL", "name": "Apple • Robinhood Token",
     "value": 13.968641, "est_position_usd": 4480.32}
  ],
  "portfolio_usd_total": 4480.32,
  "priced_positions": 1
}
```

**What to look at:** `est_position_usd` appears only for positions with a
**fresh** cached quote (15 s TTL) — cold or stale quotes drop the estimate
to `null` and a note tells you to `call quotes() on those symbols first`;
that's why the `quote()` call precedes the holdings read. Positions are
sorted by estimate descending, so the priciest position leads. `count` joins
against **only** the 194-token universe — random ERC-20s the wallet holds
are excluded, and a transit address that already forwarded everything shows
the honest `count: 0`. With `quote("AAPL")` at `mid_adjusted` 320.741463,
13.968641 tokens ≈ $4,480.32 — the estimate is `value × mid_adjusted`.

---

## 8. "Any whale movements in AAPL?" → `transfer_history("AAPL")`

Agent alert recipe: poll `transfer_history` with a `min_value` threshold on
an interval; every qualifying row is an alert candidate.

```python
transfer_history(symbol="AAPL", limit=10, min_value=10)
```

```json
{
  "transfers": [],
  "note": "5 transfer(s) below min_value=10.0 filtered out; older transfers beyond the RPC window via explorer /tokens/{contract}/transfers",
  "stopped_reason": null
}
```

That honest empty result **is the story**: in the ~1 h public-RPC visibility
window there was no ≥ 10 AAPL move. The same window captured unfiltered
tells you what *was* moving:

```json
{"ts": "2026-09-04T19:53:42Z",
 "from": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
 "to": "0xb8cdbcbc6f6f271906fcffd9235850b3b66cf653",
 "value": 13.956813,
 "tx_hash": "0x6a811ffafad11c3fd9735baafd76a3b162f3ff792f8fcbc15b551066435499c1",
 "block": 54529214}
```

plus `0.065467` and `0.000614` hop-chained remints later in the window —
the 13.956813 AAPL contract→wallet transfer is the whale candidate the
scenario 7 wallet got its position from.

**What to look at:** `min_value` filters in **token units** (`10` = 10 AAPL,
not $10). The public RPC only serves a floating ~45-60-block window (~1 h
of history); older transfers are reachable only via the explorer link the
`note` points at. `stopped_reason: "window-closed"` (in `window`) means the
walk-back hit the archive wall — partial data, not an error. And "empty
`transfers` + a filtered-out `note`" is a healthy no-news answer, not a
failure — the filter provably ran; nothing above threshold moved.

