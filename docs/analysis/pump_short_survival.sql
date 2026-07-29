-- Pump-short v1 survival screen.
--
-- One-off diagnostic, NOT a versioned confirmatory report. A fast pre-check before
-- investing in more measurement/inference: is there a raw mean-reversion pulse on the
-- TRADEABLE subset that clears a modeled cost screen, and how many opportunities
-- per week does that universe actually produce?
--
-- This is a SCREEN, not a verdict. A negative screening margin is a strong warning,
-- not proof that no exit policy saves it: fixed-horizon forward return does not model
-- the real stop-loss, trailing exit, or entry timing. A positive screen means run the
-- full virtual replay next; a clearly negative one means don't bother.
--
-- Run on prod (read-only; TEMP VIEW + SELECT only, writes nothing):
--   docker exec -i schurfer-postgres psql -U schurfer -d schurfer -f - < docs/analysis/pump_short_survival.sql
--
-- Units: short_return_pct / mfe_pct / mae_pct are PERCENT (10 = +10%). Impacts and
-- spread are bps. Modeled costs = recorded entry (bid) + exit (ask) impact at the
-- decision-time book + 20 bps taker round trip + 5 bps/8h funding for the horizon.
-- Exit slippage is held constant = entry-time book (the assumption this project has
-- never validated), so the result is neither an observed fill nor a calibrated
-- upper/lower bound. Exit-time quote capture is required to determine its bias.

\pset null 'NULL'

-- Locked scope (widen/narrow deliberately, not by accident).
\set cohort_start '2026-01-01'
\set resolver_version 'forward_v1'
\set strategy_version 'pump_short_v1_market_quality'

-- Per-decision projection. Three-state tradeable_status: NULL/absent quality is
-- 'unknown', never silently folded into untradeable. Episode key required.
DROP VIEW IF EXISTS _survival_decisions;
CREATE TEMP VIEW _survival_decisions AS
SELECT
    d.decision_id,
    d.pump_event_id,
    d.base,
    d.ts,
    CASE
        WHEN (d.liquidity->'quality'->>'allowed') = 'true'  THEN 'tradeable'
        WHEN (d.liquidity->'quality'->>'allowed') = 'false' THEN 'untradeable'
        ELSE 'unknown'
    END                                                          AS tradeable_status,
    d.liquidity->'quality'->>'reason'                            AS quality_reason,
    NULLIF(d.liquidity->'quality'->>'spread_bps','')::float8     AS spread_bps,
    NULLIF(d.liquidity->'quality'->>'bid_impact_bps','')::float8 AS bid_impact_bps,
    NULLIF(d.liquidity->'quality'->>'ask_impact_bps','')::float8 AS ask_impact_bps
FROM app.trade_decisions d
WHERE d.decision_id IS NOT NULL
  AND d.pump_event_id IS NOT NULL
  -- Do NOT filter on a present quality snapshot: decisions without one (no_exchange,
  -- fetch_failed, etc.) must stay in the denominator as 'unknown', otherwise the
  -- tradeable rate is optimistically inflated.
  AND d.strategy_version = :'strategy_version'
  AND d.ts >= :'cohort_start'::timestamptz;

-- ===========================================================================
-- 1. UNIVERSE & SCOPE
-- ===========================================================================
SELECT
    'universe' AS section,
    :'strategy_version' AS strategy_version,
    :'resolver_version' AS resolver_version,
    count(*)                       AS decisions,
    count(DISTINCT pump_event_id)  AS events,
    count(DISTINCT base)           AS asset_clusters,
    min(ts)::date                  AS first_day,
    max(ts)::date                  AS last_day
FROM _survival_decisions;

-- ===========================================================================
-- 2. ABSOLUTE OPPORTUNITY FLOW — the number that makes a low % acceptable or not.
--    A tradeable event = an episode with >=1 tradeable decision that week.
-- ===========================================================================
-- observed_days guards against reading the (partial) first and last weeks as a rate.
-- calendar_days = span first..last observed date in the week (interior dead days
-- kept). observed_days shows sparsity. Rate uses calendar_days so quiet days are not
-- dropped from the denominator. Neither can distinguish "no pumps" from "scanner down".
SELECT
    'opportunity_per_week' AS section,
    date_trunc('week', ts)::date AS iso_week,
    count(DISTINCT ts::date)                    AS observed_days,
    (max(ts::date) - min(ts::date) + 1)         AS calendar_days,
    count(DISTINCT pump_event_id)               AS events,
    count(DISTINCT pump_event_id) FILTER (WHERE tradeable_status = 'tradeable') AS tradeable_events,
    round(
        count(DISTINCT pump_event_id) FILTER (WHERE tradeable_status = 'tradeable')::numeric
        / NULLIF(max(ts::date) - min(ts::date) + 1, 0), 2
    ) AS tradeable_per_calendar_day
FROM _survival_decisions
GROUP BY 2
ORDER BY 2;

-- ===========================================================================
-- 3. TRADEABILITY RATE (three-state) — per decision and per event.
-- ===========================================================================
SELECT
    'tradeability_decisions' AS section,
    tradeable_status,
    count(*) AS n,
    round(100.0 * count(*) / NULLIF(sum(count(*)) OVER (), 0), 1) AS pct
FROM _survival_decisions
GROUP BY tradeable_status
ORDER BY n DESC;

SELECT
    'tradeability_events' AS section,
    count(*)                                                          AS events,
    count(*) FILTER (WHERE has_tradeable)                            AS tradeable_events,
    round(100.0 * count(*) FILTER (WHERE has_tradeable) / NULLIF(count(*),0), 1) AS tradeable_pct
FROM (
    SELECT pump_event_id, bool_or(tradeable_status = 'tradeable') AS has_tradeable
    FROM _survival_decisions
    GROUP BY pump_event_id
) e;

-- Why do the non-tradeable ones fail (untradeable + unknown)?
SELECT
    'failure_reasons' AS section,
    tradeable_status,
    quality_reason,
    count(*) AS n
FROM _survival_decisions
WHERE tradeable_status <> 'tradeable'
GROUP BY tradeable_status, quality_reason
ORDER BY n DESC;

-- ===========================================================================
-- 4. PULSE (per event) — forward short return by the entry decision the strategy
--    would actually take: first tradeable decision if any, else first overall.
--    Tagged by that decision's status, so tradeable vs untradeable is comparable
--    and survivorship shows up (do the untradeable ones revert MORE?).
-- ===========================================================================
WITH entry_decision AS (
    SELECT DISTINCT ON (pump_event_id)
        decision_id, pump_event_id, base, tradeable_status
    FROM _survival_decisions
    ORDER BY pump_event_id, (tradeable_status = 'tradeable') DESC, ts, decision_id
)
SELECT
    'pulse_per_event' AS section,
    o.horizon_minutes,
    e.tradeable_status,
    count(*)                        AS events,
    count(DISTINCT e.base)          AS asset_clusters,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY o.short_return_pct)::numeric, 3) AS median_ret_pct,
    round(percentile_cont(0.25) WITHIN GROUP (ORDER BY o.short_return_pct)::numeric, 3) AS p25_ret_pct,
    round(percentile_cont(0.75) WITHIN GROUP (ORDER BY o.short_return_pct)::numeric, 3) AS p75_ret_pct,
    round(avg(o.short_return_pct)::numeric, 3) AS mean_ret_pct,
    round(100.0 * count(*) FILTER (WHERE o.short_return_pct > 0) / NULLIF(count(*),0), 1) AS pct_positive,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY o.mae_pct)::numeric, 3) AS median_mae_pct
FROM entry_decision e
JOIN app.trade_decision_outcomes o ON o.decision_id = e.decision_id
WHERE o.resolver_version = :'resolver_version'
  AND o.status = 'complete'
  AND o.short_return_pct IS NOT NULL
  AND o.coverage_ratio >= 0.9
  AND o.horizon_minutes IN (15, 30, 60, 240, 480)  -- 480 = research horizon beyond v1 hold
GROUP BY o.horizon_minutes, e.tradeable_status
ORDER BY o.horizon_minutes, e.tradeable_status;

-- ===========================================================================
-- 5. OUTCOME COVERAGE — what the exact-only screen (status=complete, coverage>=0.9)
--    excludes, by venue. If usable_exact is concentrated on Binance/Bybit and the
--    illiquid venues fall into complete_fallback / market_path_unavailable /
--    fetch_failed, the margin below describes a liquidity-biased subset, not v1.
-- ===========================================================================
-- LEFT JOIN over an explicit horizon set so a selected event with NO outcome row
-- shows up as status='missing' instead of silently vanishing. usable_exact matches
-- the headline screen exactly (complete + non-null return + coverage>=0.9).
WITH entry_decision AS (
    SELECT DISTINCT ON (pump_event_id) decision_id, pump_event_id
    FROM _survival_decisions
    WHERE tradeable_status = 'tradeable'
      AND bid_impact_bps IS NOT NULL AND ask_impact_bps IS NOT NULL
    ORDER BY pump_event_id, ts, decision_id
),
horizons (h) AS (VALUES (15), (30), (60), (240), (480)),
coverage AS (
    SELECT
        hz.h AS horizon_minutes,
        COALESCE(o.anchor_exchange, '(none)') AS anchor_exchange,
        COALESCE(o.status, 'missing')         AS status,
        (o.status = 'complete' AND o.short_return_pct IS NOT NULL
             AND o.coverage_ratio >= 0.9)     AS usable
    FROM entry_decision e
    CROSS JOIN horizons hz
    LEFT JOIN app.trade_decision_outcomes o
        ON o.decision_id = e.decision_id
       AND o.horizon_minutes = hz.h
       AND o.resolver_version = :'resolver_version'
)
SELECT
    'outcome_coverage' AS section,
    horizon_minutes,
    anchor_exchange,
    status,
    count(*)                            AS events,
    count(*) FILTER (WHERE usable)      AS usable_exact
FROM coverage
GROUP BY horizon_minutes, anchor_exchange, status
ORDER BY horizon_minutes, events DESC;

-- ===========================================================================
-- 6. SURVIVAL SCREENING MARGIN (per event, per row) — the headline.
--    Only over episodes the strategy would actually trade (first tradeable
--    decision), individual screening_net = 100*short_return_pct - cost_floor_bps.
--    If median AND mean are negative across the v1 horizons (<=240m) with healthy
--    coverage above, that is strong evidence against v1 as configured — run the full
--    virtual replay to confirm before shelving; it is not itself the final verdict.
-- ===========================================================================
WITH entry_decision AS (
    SELECT DISTINCT ON (pump_event_id)
        decision_id, pump_event_id, base, bid_impact_bps, ask_impact_bps
    FROM _survival_decisions
    WHERE tradeable_status = 'tradeable'
      AND bid_impact_bps IS NOT NULL
      AND ask_impact_bps IS NOT NULL
    ORDER BY pump_event_id, ts, decision_id
),
screened AS (
    SELECT
        o.horizon_minutes,
        e.base,
        100 * o.short_return_pct
            - (e.bid_impact_bps + e.ask_impact_bps + 20 + 5.0 * o.horizon_minutes / 480)
                                                              AS screening_net_bps,
        o.mfe_pct,
        o.mae_pct
    FROM entry_decision e
    JOIN app.trade_decision_outcomes o ON o.decision_id = e.decision_id
    WHERE o.resolver_version = :'resolver_version'
      AND o.status = 'complete'
      AND o.short_return_pct IS NOT NULL
      AND o.coverage_ratio >= 0.9
      AND o.horizon_minutes IN (15, 30, 60, 240, 480)  -- 480 = research horizon beyond v1 hold
)
SELECT
    'survival_screening_margin' AS section,
    horizon_minutes,
    count(*)               AS events,
    count(DISTINCT base)   AS asset_clusters,
    round(percentile_cont(0.5)  WITHIN GROUP (ORDER BY screening_net_bps)::numeric, 1) AS median_net_bps,
    round(avg(screening_net_bps)::numeric, 1)                                          AS mean_net_bps,
    round(percentile_cont(0.25) WITHIN GROUP (ORDER BY screening_net_bps)::numeric, 1) AS p25_net_bps,
    round(percentile_cont(0.75) WITHIN GROUP (ORDER BY screening_net_bps)::numeric, 1) AS p75_net_bps,
    round(100.0 * count(*) FILTER (WHERE screening_net_bps > 0) / NULLIF(count(*),0), 1) AS pct_positive,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY mfe_pct)::numeric, 3) AS median_mfe_pct,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY mae_pct)::numeric, 3) AS median_mae_pct
FROM screened
GROUP BY horizon_minutes
ORDER BY horizon_minutes;
