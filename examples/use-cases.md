# Use cases: 5 agent scenarios

Each scenario: the user's question → the tool(s) to call → an example call →
abbreviated output → what to look at. Output snippets are abbreviated JSON;
single-quote values (scenario 1) are real fixture data from the live capture
of 2026-09-03 — batch and sector examples are illustrative (only AAPL has a
captured price fixture). Live numbers will differ.

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
