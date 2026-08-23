# early_momentum v4 quality-policy calibration

**Date:** 2026-08-23
**Branch:** `fix/early-momentum-input-quality-v1`
**Policy this justifies:** `EARLY_MOMENTUM_V4_QUALITY_POLICY` in
`apps/execution/schurfer_execution/early_momentum.py`

This note exists so the frozen thresholds baked into
`EARLY_MOMENTUM_V4_QUALITY_POLICY` have a traceable origin. Without it,
`max_oi_age_seconds_by_exchange = {"binance": 180, "bybit": 600}` and
`max_bucket_lag_seconds = 180` are just numbers in a comment a month from
now. Any future change to these values must update this note (or add a
new dated section below) and bump the policy's hash/cohort — see the
policy's own docstring.

All queries were run directly against the production `schurfer-postgres`
instance (`ssh schurfer` → `docker exec schurfer-postgres psql ...`),
read-only `SELECT`s only.

## 1. Metric naming correction

`open_interest_event_at - bucket_start` is **not** ingestion lag — it only
shows when within the minute the last OI observation landed. True
ingestion lag is `open_interest_observed_at - open_interest_event_at`:

```sql
SELECT exchange,
  count(*) AS complete_rows,
  percentile_cont(0.5) WITHIN GROUP (
    ORDER BY EXTRACT(EPOCH FROM (open_interest_observed_at - open_interest_event_at))
  ) AS p50_true_ingestion_lag_s,
  percentile_cont(0.95) WITHIN GROUP (
    ORDER BY EXTRACT(EPOCH FROM (open_interest_observed_at - open_interest_event_at))
  ) AS p95_true_ingestion_lag_s,
  percentile_cont(0.99) WITHIN GROUP (
    ORDER BY EXTRACT(EPOCH FROM (open_interest_observed_at - open_interest_event_at))
  ) AS p99_true_ingestion_lag_s,
  max(EXTRACT(EPOCH FROM (open_interest_observed_at - open_interest_event_at))) AS max_true_ingestion_lag_s
FROM timeseries.bybit_momentum_bars_1m
WHERE price_complete = true AND open_interest_complete = true AND trades_complete = true
  AND bucket_start >= now() - interval '1 day'
GROUP BY exchange;
```

| exchange | p50   | p95   | p99   | max   |
| -------- | ----- | ----- | ----- | ----- |
| binance  | 6.4s  | 21.6s | 36.1s | 63.3s |
| bybit    | 0.09s | 0.10s | 0.10s | 10.5s |

Binance re-polls OI on a fixed REST cadence, so `event_at` always advances
even when the value doesn't change. Bybit is delta-push over WebSocket:
`observed_at` is near-instant local receipt, but `event_at` **only
advances when the OI field is actually present in a message** — an old
`event_at` on bybit does not by itself mean the feed is broken.

## 2. Historical bucket-lag calibration (3 days, not a single snapshot)

```sql
WITH per_minute AS (
  SELECT exchange, bucket_start, count(DISTINCT symbol) AS symbols_written,
    max(created_at) AS last_write_at
  FROM timeseries.bybit_momentum_bars_1m
  WHERE bucket_start >= now() - interval '3 days'
  GROUP BY exchange, bucket_start
)
SELECT exchange,
  count(*) AS minutes,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (last_write_at - bucket_start))) AS p50_write_lag_s,
  percentile_cont(0.95) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (last_write_at - bucket_start))) AS p95_write_lag_s,
  percentile_cont(0.99) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (last_write_at - bucket_start))) AS p99_write_lag_s,
  max(EXTRACT(EPOCH FROM (last_write_at - bucket_start))) AS max_write_lag_s,
  count(*) FILTER (WHERE EXTRACT(EPOCH FROM (last_write_at - bucket_start)) > 180) AS minutes_over_180s
FROM per_minute
GROUP BY exchange;
```

| exchange | minutes | p50   | p95   | p99   | max   | minutes >180s |
| -------- | ------- | ----- | ----- | ----- | ----- | ------------- |
| binance  | 4319    | 69.2s | 69.2s | 69.3s | 69.3s | 0             |
| bybit    | 4319    | 65.4s | 65.4s | 65.5s | 65.5s | 0             |

Essentially zero variance, zero minutes over 180s in 3 full days of
history. `max_bucket_lag_seconds = 180` has real historical margin, not an
operational guess.

## 3. Bybit "stale OI" investigation

A live snapshot flagged 26 bybit symbols whose `open_interest_event_at` was

> 180s old. For each, `last_ticker_event_at`/`last_trade_event_at`/
> `last_price_event_at` (from the same row) were also pulled:

```sql
WITH latest AS (
  SELECT DISTINCT ON (exchange, symbol)
    exchange, symbol, bucket_start, open_interest, open_interest_event_at, open_interest_observed_at,
    last_ticker_event_at, last_trade_event_at, last_price_event_at,
    price_complete, trades_complete, open_interest_complete
  FROM timeseries.bybit_momentum_bars_1m
  WHERE bucket_start >= now() - interval '30 minutes' AND exchange = 'bybit' AND market_type = 'linear'
  ORDER BY exchange, symbol, bucket_start DESC
)
SELECT symbol, open_interest,
  round(EXTRACT(EPOCH FROM (now() - open_interest_event_at))::numeric) AS oi_age_s,
  round(EXTRACT(EPOCH FROM (now() - last_ticker_event_at))::numeric) AS ticker_age_s,
  round(EXTRACT(EPOCH FROM (now() - last_trade_event_at))::numeric) AS trade_age_s,
  round(EXTRACT(EPOCH FROM (now() - last_price_event_at))::numeric) AS price_age_s
FROM latest
WHERE price_complete = true AND trades_complete = true AND open_interest_complete = true
  AND now() - open_interest_event_at > interval '180 seconds'
ORDER BY oi_age_s DESC;
```

Result: every one of the 26 symbols had `ticker_age_s`/`price_age_s` of
~40-90 seconds (feed alive, healthy) while only `open_interest_event_at`
was old (195s to 18,485s). The surrounding feed was never broken.

**Independent live REST cross-check** for the worst case, `USD1USDT`
(18,485s stale by WS data): `curl` against Bybit's public
`/v5/market/open-interest` and `/v5/market/tickers` (no keys, read-only)
returned `openInterest=594980` — the exact same value the "stale" WS row
already had. The value genuinely had not changed.

**24h update-frequency history** for the 8 symbols that would still be cut
at a 600s threshold:

```sql
SELECT symbol, count(DISTINCT open_interest_event_at) AS distinct_oi_events_24h, count(*) AS bars_24h,
  min(open_interest), max(open_interest)
FROM timeseries.bybit_momentum_bars_1m
WHERE exchange='bybit' AND symbol IN ('USD1USDT','USDEUSDT','BOBAUSDT','10000NEXUSDT','VELOUSDT','XVSUSDT','ILVUSDT','SNTUSDT')
  AND bucket_start >= now() - interval '1 day'
GROUP BY symbol ORDER BY distinct_oi_events_24h;
```

`distinct_oi_events_24h` ranged from 99 (`USD1USDT`, a stablecoin pair —
updates roughly every 14-15 min) to 897 (`VELOUSDT` — roughly every 1.6
min) out of 1439 possible minutes. Genuinely low-frequency legitimate
deltas, not a dead feed.

## Conclusion

```python
max_bucket_lag_seconds = 180
max_oi_age_seconds_by_exchange = {"binance": 180, "bybit": 600}
```

Both binance values and the bucket-lag threshold are backed by 3-day
historical distributions with wide margin. The bybit OI threshold (600s)
is the more conservative of two candidates considered (300s was the
original, tighter proposal) — chosen because the causal evidence above
(REST ground-truth match + feed-liveness cross-reference + update-frequency
history) showed the tighter threshold would have mislabeled quiet-but-alive
instruments as broken. A symbol quiet enough to sit outside even a 600s
window is almost certainly also flat enough to fail the strategy's own
`>5%` 2h OI-growth signal regardless, so this costs little in practice —
the freshness gate's job here is mainly to keep the `rejected_stale_oi`
health counter honest, not to protect signal recall.

**Open item, not fully closed by this session's evidence:** a true
per-decision-minute time series of how many symbols get cut, over multiple
days and market regimes, was not run — what's here instead (REST
ground-truth + feed-liveness cross-reference + update-frequency history)
answers the causal question directly. If a future review wants the literal
historical cut-rate series before tightening 600s further, that's a
straightforward follow-up query against this same table, not a rerun of
the causal investigation above.

**Any future change to either threshold must bump
`EARLY_MOMENTUM_V4_QUALITY_POLICY`'s hash and therefore the strategy
cohort** — see the policy's own docstring in `early_momentum.py`.
