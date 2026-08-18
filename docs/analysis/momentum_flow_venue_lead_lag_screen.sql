-- Bybit vs Binance lead-lag screen.
--
-- One-off discovery diagnostic, NOT a versioned confirmatory report. This is
-- literally 30-PR-roadmap Phase 5 / PR14 ("venue overlap/lead-time analysis"),
-- pulled forward informally: now that both venues have real continuous per-minute
-- captured bars (not just above-threshold scanner snapshots), the lead-lag question
-- OBS-5 (2026-07-20, memory: htx led binance/bybit on one observed pump, using only
-- 24h%-change snapshots) explicitly said needed "continuous per-exchange ticker
-- data" to answer properly is now answerable directly from our own data.
--
-- Method: for every symbol captured on BOTH venues at the same wall-clock minute,
-- compute 1-minute returns, then correlate Bybit's own return at t against
-- Binance's return at t-3..t+3 (pooled across all symbols/minutes, not per-symbol --
-- a per-symbol breakdown is a natural follow-up once a venue-level lead is found,
-- not before). Whichever lag maximizes the correlation indicates which venue's
-- price move tends to happen first.
--
-- Scope: Binance's own capture only goes back to ~2026-08-15 (see capture uptime),
-- so this necessarily reuses whatever overlap window exists at run time -- expect
-- the correlation values themselves to sharpen as more overlapping history
-- accumulates. Not restricted to pumped_bases (unlike the volume-burst screen):
-- lead-lag is a market-structure question, not conditional on already knowing a
-- pump happened, so the full common-symbol universe is the right population here.
--
-- Run on prod (read-only; CTEs + SELECT only, writes nothing):
--   docker exec -i schurfer-postgres psql -U schurfer -d schurfer -f - < docs/analysis/momentum_flow_venue_lead_lag_screen.sql

\pset null 'NULL'
\set capture_version 'v1'

WITH bybit_bars AS (
    SELECT symbol, bucket_start, close_price
    FROM timeseries.bybit_momentum_bars_1m
    WHERE exchange = 'bybit' AND capture_version = :'capture_version'
      AND complete = true AND close_price > 0
),
binance_bars AS (
    SELECT symbol, bucket_start, close_price
    FROM timeseries.bybit_momentum_bars_1m
    WHERE exchange = 'binance' AND capture_version = :'capture_version'
      AND complete = true AND close_price > 0
),
common AS (
    SELECT b.symbol, b.bucket_start, b.close_price AS bybit_price, n.close_price AS binance_price
    FROM bybit_bars b
    JOIN binance_bars n ON b.symbol = n.symbol AND b.bucket_start = n.bucket_start
),
rets AS (
    SELECT symbol, bucket_start,
           100.0 * (bybit_price / NULLIF(LAG(bybit_price) OVER w, 0) - 1) AS bybit_ret,
           100.0 * (binance_price / NULLIF(LAG(binance_price) OVER w, 0) - 1) AS binance_ret
    FROM common
    WINDOW w AS (PARTITION BY symbol ORDER BY bucket_start)
),
lagged AS (
    SELECT symbol, bucket_start, bybit_ret,
           LAG(binance_ret, 3) OVER w AS binance_lag3,
           LAG(binance_ret, 2) OVER w AS binance_lag2,
           LAG(binance_ret, 1) OVER w AS binance_lag1,
           binance_ret           AS binance_lag0,
           LEAD(binance_ret, 1) OVER w AS binance_lead1,
           LEAD(binance_ret, 2) OVER w AS binance_lead2,
           LEAD(binance_ret, 3) OVER w AS binance_lead3
    FROM rets
    WINDOW w AS (PARTITION BY symbol ORDER BY bucket_start)
)
-- Column names read as "which venue's move at the labeled offset correlates best
-- with Bybit's own return right now." binance_leads_3m high means Binance's return
-- 3 minutes AGO predicts Bybit's return now -- i.e. Binance moved first.
-- bybit_leads_3m high means the reverse -- Bybit moved first.
SELECT
    round(corr(bybit_ret, binance_lag3)::numeric, 5) AS binance_leads_3m,
    round(corr(bybit_ret, binance_lag2)::numeric, 5) AS binance_leads_2m,
    round(corr(bybit_ret, binance_lag1)::numeric, 5) AS binance_leads_1m,
    round(corr(bybit_ret, binance_lag0)::numeric, 5) AS contemporaneous,
    round(corr(bybit_ret, binance_lead1)::numeric, 5) AS bybit_leads_1m,
    round(corr(bybit_ret, binance_lead2)::numeric, 5) AS bybit_leads_2m,
    round(corr(bybit_ret, binance_lead3)::numeric, 5) AS bybit_leads_3m,
    count(*) FILTER (WHERE bybit_ret IS NOT NULL AND binance_lag0 IS NOT NULL) AS n_contemporaneous_pairs
FROM lagged;
