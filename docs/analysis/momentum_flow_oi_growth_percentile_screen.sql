-- OI-growth-percentile tightening screen (Bybit, full universe).
--
-- One-off discovery diagnostic, NOT a versioned confirmatory report. Frozen
-- momentum_flow_watch_v1 (apps/analytics/schurfer_analytics/momentum_flow_watch_
-- contract.py) requires a symbol's own 60-minute OI growth to be in the top 10% of
-- that same minute's cross-section (`OI_GROWTH_PERCENTILE = 0.90`) as one of three
-- AND-ed entry conditions. Question: would tightening that cutoff (top 5%, top 2%)
-- actually improve forward returns?
--
-- IMPORTANT SCOPE CAVEAT: this measures OI-growth-percentile ALONE, univariate --
-- it does NOT replay the real prepare_symbol_evaluation/build_cross_section_
-- thresholds code, and does NOT apply the contract's other two AND-ed conditions
-- (buy_imbalance_15m, flow_acceleration_15m_vs_prior_45m) or any quality gate. A
-- hand-rolled approximation, not the frozen contract itself -- acceptable for a
-- fast first-pass screen (unlike PR4's coverage report, which specifically had to
-- be contract-faithful), not for anything confirmatory. Full universe (no
-- pump_events restriction) since this is a cross-sectional percentile computed
-- per-minute, not a per-symbol trailing-window computation -- much cheaper than
-- the volume-burst screen's own 1440-row trailing window (~27s over ~5.5M rows for
-- the whole Bybit v1 universe at time of writing).
--
-- 2026-08-17 finding (Bybit, full ~738-symbol universe, whatever history capture_
-- version v1 has accumulated): the EXACT OPPOSITE of the naive expectation.
-- Forward returns get MONOTONICALLY WORSE as the OI-growth cutoff tightens:
--   below top10  (n=4,932,313): 60m -0.007%  240m -0.018%  720m -0.068%
--   top10 (live) (n=  274,388): 60m -0.027%  240m -0.114%  720m -0.252%
--   top5         (n=  159,631): 60m -0.049%  240m -0.184%  720m -0.384%
--   top2         (n=  110,517): 60m -0.102%  240m -0.386%  720m -0.805%
-- High OI growth alone is, if anything, a mild BEARISH tilt on Bybit, and the more
-- extreme the growth, the stronger the tilt -- not a case for tightening this
-- threshold in isolation. Does not by itself mean the live 3-condition contract is
-- wrong (the other two AND-ed conditions and quality gates are not modeled here at
-- all), but does mean "just require a higher OI-growth percentile" is not a
-- promising lever on its own.
--
-- Run on prod (read-only; CTEs + SELECT only, writes nothing):
--   docker exec -i schurfer-postgres psql -U schurfer -d schurfer -f - < docs/analysis/momentum_flow_oi_growth_percentile_screen.sql

\pset null 'NULL'
\set exchange 'bybit'
\set capture_version 'v1'
\set min_cross_section_size '100'

WITH bars AS (
  SELECT symbol, bucket_start, close_price, open_interest
  FROM timeseries.bybit_momentum_bars_1m
  WHERE exchange = :'exchange' AND capture_version = :'capture_version'
    AND complete = true AND close_price > 0
),
oi_growth AS (
  SELECT symbol, bucket_start, close_price, open_interest,
         LAG(open_interest, 60) OVER (PARTITION BY symbol ORDER BY bucket_start) AS oi_60m_ago,
         LEAD(close_price, 60) OVER (PARTITION BY symbol ORDER BY bucket_start) AS price_fwd_60m,
         LEAD(close_price, 240) OVER (PARTITION BY symbol ORDER BY bucket_start) AS price_fwd_240m,
         LEAD(close_price, 720) OVER (PARTITION BY symbol ORDER BY bucket_start) AS price_fwd_720m
  FROM bars
),
scored AS (
  SELECT symbol, bucket_start, close_price,
         100.0 * (open_interest / NULLIF(oi_60m_ago, 0) - 1) AS oi_growth_60m_pct,
         price_fwd_60m, price_fwd_240m, price_fwd_720m
  FROM oi_growth
  WHERE open_interest IS NOT NULL AND oi_60m_ago IS NOT NULL AND oi_60m_ago > 0 AND open_interest > 0
),
ranked AS (
  SELECT *,
         PERCENT_RANK() OVER (PARTITION BY bucket_start ORDER BY oi_growth_60m_pct) AS pct_rank,
         count(*) OVER (PARTITION BY bucket_start) AS cross_section_size
  FROM scored
)
SELECT
  CASE WHEN pct_rank >= 0.98 THEN '4_top2'
       WHEN pct_rank >= 0.95 THEN '3_top5'
       WHEN pct_rank >= 0.90 THEN '2_top10_live_contract'
       ELSE '1_below_top10' END AS tier,
  count(*) AS n,
  round(avg(100.0 * (price_fwd_60m / close_price - 1))::numeric, 4) AS avg_60m,
  round(avg(100.0 * (price_fwd_240m / close_price - 1))::numeric, 4) AS avg_240m,
  round(avg(100.0 * (price_fwd_720m / close_price - 1))::numeric, 4) AS avg_720m
FROM ranked
WHERE cross_section_size >= :min_cross_section_size
  AND price_fwd_60m IS NOT NULL AND price_fwd_240m IS NOT NULL AND price_fwd_720m IS NOT NULL
GROUP BY tier
ORDER BY tier;
