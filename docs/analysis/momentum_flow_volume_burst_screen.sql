-- Volume-burst-vs-forward-return screen (Bybit + Binance).
--
-- One-off discovery diagnostic, NOT a versioned confirmatory report. Inspired by a
-- reference Telegram channel's own alert shape ("$X bought in N min = Y% of 24h
-- volume") -- a signal type Schurfer's own pump scanner cannot produce at all today
-- (it is a pure 24h-%-price-change threshold; see apps/analytics/schurfer_analytics/
-- scanner.py -- no order-flow-concentration/volume-burst metric exists anywhere in
-- the codebase). This screen asks: on already-captured momentum-flow bars, does a
-- burst of buy notional (last 5 minutes) relative to a symbol's own trailing 24h
-- volume actually precede positive forward price movement?
--
-- Restricted to symbols that pumped at least once in the window (per app.pump_events),
-- not the full universe: cheaper to compute, but a real, UNRESOLVED selection-bias
-- risk flagged during the original run -- these are already-volatile-by-construction
-- symbols, so an observed positive pattern here does not yet prove burst_pct itself
-- is discriminative versus "this coin is generally volatile." The full-universe
-- unconditional baseline (~738 Bybit symbols vs. the ~80 used here) was not run.
--
-- 2026-08-17 finding (Bybit, 7 days, capture_version v1): a real, roughly monotonic
-- rise in average forward return from baseline (~0%) through ~14% burst_pct
-- (+1-4%), strongest in the 20%+ bucket (+3.95%/+6.02%/+3.62% at 60/240/720min).
-- BUT that top bucket's 90 "extreme minutes" collapse to only 13 independent
-- episodes once declustered by symbol -- nowhere near enough to act on (see the
-- declustering query at the bottom). Binance replication (~2 days of history,
-- much smaller per-symbol sample) showed a partially consistent rise through the
-- 6-14% range but did not confirm the 20%+ tail (n=11, slightly negative) --
-- inconclusive, not contradictory, simply too little data yet.
--
-- Run on prod (read-only; CTEs + SELECT only, writes nothing):
--   docker exec -i schurfer-postgres psql -U schurfer -d schurfer -f - < docs/analysis/momentum_flow_volume_burst_screen.sql
--
-- Change :'exchange' to 'binance' to rerun on the other venue; change the interval
-- in pumped_bases and the n_minutes_24h/forward-horizon floors to match however much
-- history that venue actually has (Binance's own capture is much younger than
-- Bybit's -- see the comment on n_minutes_24h below).

\pset null 'NULL'
\set exchange 'bybit'
\set capture_version 'v1'
\set since_days '7'

WITH pumped_bases AS (
    SELECT DISTINCT base
    FROM app.pump_events
    WHERE first_seen_at >= now() - (:'since_days' || ' days')::interval
      AND exchanges @> ('[{"exchange": "' || :'exchange' || '"}]')::jsonb
),
bars AS (
    SELECT b.symbol, b.bucket_start, b.close_price, b.buy_total_notional_usd, b.sell_total_notional_usd
    FROM timeseries.bybit_momentum_bars_1m b
    JOIN pumped_bases p ON b.symbol = p.base || 'USDT'
    WHERE b.exchange = :'exchange' AND b.capture_version = :'capture_version'
      AND b.complete = true AND b.close_price > 0
),
windowed AS (
    SELECT symbol, bucket_start, close_price,
           SUM(buy_total_notional_usd) OVER w5 AS buy_notional_5m,
           SUM(buy_total_notional_usd + sell_total_notional_usd) OVER w1440 AS total_volume_24h,
           -- COUNT(*) OVER w1440, not a literal 1440: this window naturally clips at
           -- the partition's own start, so it reports however many minutes of real
           -- trailing history actually exist yet for that symbol, not a fixed 24h.
           COUNT(*) OVER w1440 AS n_minutes_trailing,
           LEAD(close_price, 60) OVER (PARTITION BY symbol ORDER BY bucket_start) AS price_fwd_60m,
           LEAD(close_price, 240) OVER (PARTITION BY symbol ORDER BY bucket_start) AS price_fwd_240m,
           LEAD(close_price, 720) OVER (PARTITION BY symbol ORDER BY bucket_start) AS price_fwd_720m
    FROM bars
    WINDOW w5 AS (PARTITION BY symbol ORDER BY bucket_start ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
           w1440 AS (PARTITION BY symbol ORDER BY bucket_start ROWS BETWEEN 1439 PRECEDING AND CURRENT ROW)
),
scored AS (
    SELECT symbol, bucket_start,
           100.0 * buy_notional_5m / NULLIF(total_volume_24h, 0) AS burst_pct_5m,
           100.0 * (price_fwd_60m / close_price - 1) AS fwd_ret_60m_pct,
           100.0 * (price_fwd_240m / close_price - 1) AS fwd_ret_240m_pct,
           100.0 * (price_fwd_720m / close_price - 1) AS fwd_ret_720m_pct
    FROM windowed
    -- 1440 here assumes Bybit-depth history (7+ days). Drop to n_minutes_trailing >=
    -- 60 and the 60m-only horizon for a venue with only a couple days of capture
    -- (e.g. Binance today) -- 200+240=440 minutes of combined trailing+forward
    -- requirement silently returns zero rows once a symbol's own total history is
    -- shorter than that sum (this is what happened on the first Binance attempt:
    -- looked like a bug, was actually a real data-availability ceiling).
    WHERE total_volume_24h > 1000 AND n_minutes_trailing >= 1440
)
SELECT
    width_bucket(burst_pct_5m, 0, 20, 10) AS bucket,
    round(min(burst_pct_5m)::numeric, 2) AS bucket_min,
    round(max(burst_pct_5m)::numeric, 2) AS bucket_max,
    count(*) AS n,
    round(avg(fwd_ret_60m_pct)::numeric, 4) AS avg_fwd_ret_60m_pct,
    round(avg(fwd_ret_240m_pct)::numeric, 4) AS avg_fwd_ret_240m_pct,
    round(avg(fwd_ret_720m_pct)::numeric, 4) AS avg_fwd_ret_720m_pct
FROM scored
WHERE fwd_ret_60m_pct IS NOT NULL AND fwd_ret_240m_pct IS NOT NULL AND fwd_ret_720m_pct IS NOT NULL
GROUP BY bucket
ORDER BY bucket;

-- Declustering check for the top bucket (burst_pct_5m >= 20): run this separately
-- to see how many INDEPENDENT episodes actually back that row above, not just how
-- many correlated per-minute observations. A single pump episode contributes many
-- consecutive extreme minutes, which inflates n without adding real independent
-- evidence -- this is what took the 2026-08-17 Bybit top bucket from a
-- seemingly-solid n=90 down to a real n=13 once declustered by symbol.
--
-- WITH pumped_bases AS ( ... same as above ... ),
-- bars AS ( ... same as above ... ),
-- windowed AS ( ... same as above, without the forward-return LEADs ... )
-- SELECT symbol, count(*) AS n_extreme_minutes, min(bucket_start), max(bucket_start)
-- FROM windowed
-- WHERE total_volume_24h > 1000 AND n_minutes_trailing >= 1440
--   AND 100.0 * buy_notional_5m / NULLIF(total_volume_24h, 0) >= 20
-- GROUP BY symbol ORDER BY n_extreme_minutes DESC;
