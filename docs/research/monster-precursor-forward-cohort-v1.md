# Monster-precursor forward cohort v1

> **SUPERSEDED / DO NOT START (2026-09-14).** This contract was pre-registered against a
> discovery result that was later RETRACTED: the discovery scripts had critical bugs (24h
> activity computed over ~1.5% of the true window, a look-ahead rank, no cooldown on the
> headline number, truncated outcomes), and a corrected replay collapsed the apparent edge to
> roughly zero with an unacceptable drawdown. The monster tradeable line is PARKED (exploratory
> economics unattractive, not a formal FAIL); see `decision-register.md`. This document is kept
> only as a record of the analysis. Any future monster work must use a new, pre-validated,
> unit-tested simulator on an UNTOUCHED window -- not this contract and not the Aug-Sep data.

## Purpose

This contract answers, on bybit/binance 1m bars never touched by the discovery
study, whether a diversified LONG on the frozen `confirmed_flow` flag holds a
real, after-cost edge over the following three days -- both in absolute money
terms and as excess over just being long the market.

It exists because the in-sample discovery (`docs/research/decision-register.md`,
Monster-precursor discovery card; scratchpad `precursor_*.py`, 2026-09-14) found
a genuine but single-regime signal: trailing-24h activity concentrates monster
pumps ~3.3x, and a `confirmed_flow` flag earned +1.86% mean excess over the
same-hour market, positive in 4 of 5 UTC weeks and robust to excluding the top
25 winners. That number is IN-SAMPLE on an August-September pump-rich window;
tail-driven means are the most overfit-prone statistic there is. This contract
freezes the flag, the outcome, and the verdict rule now, so an untouched forward
window renders the verdict rather than a re-fit of the discovery window.

It is prospective, research-only measurement. It does not modify `orders.py`,
`trader.py`, position size, leverage, or `DRY_RUN`/`AUTO_TRADE`.

## What the discovery did and did NOT show

- DID: real, broad, multi-week lift in the MEAN excess of a diversified
  many-token portfolio; executable at the $50-300 test bank (each fire is a
  drop against $100K+/day median volume, and capacity aggregates across hundreds
  of distinct tokens, not one thin name).
- DID NOT: a flag whose TYPICAL fire beats the market. The de-beta median excess
  is negative and win-vs-market is < 50%; the edge is entirely a positive-mean
  lottery carried by the rare monster tail. A quiet forward regime that thins
  that tail could flip the whole thing negative. That is exactly the risk this
  cohort tests.

## Frozen cohort and selection

All values below are the contract; a reader module
(`CONTRACT_VERSION = "monster_precursor_forward_cohort_v1"`) must mirror them and
never redefine them after seeing data.

- **Cohort start**: `MONSTER_COHORT_START = 2026-09-15T00:00:00Z`. The discovery
  data ends 2026-09-14, so every observation at or after this start is untouched
  by the discovery study. Entries whose full +3d outcome window extends past the
  data currently available are `unresolved`, never truncated.
- **Universe**: `bybit_momentum_bars_1m`, `market_type='linear'`,
  `capture_version='v1'`, `close_price > 0` -- bybit and binance perpetual bars,
  the two implemented execution venues, the same source as discovery.
- **Sampling grid**: one observation per `(exchange, symbol)` on each hourly
  boundary (`epoch(bucket_start) % 3600 = 0`).
- **Features (STRICTLY past, over the 1440m preceding to 1m preceding of the
  sample; never including the sample bar itself)**:
  - `act24` = sum of `buy_total_notional_usd + sell_total_notional_usd`.
  - `nb24` = sum of `buy_total_notional_usd - sell_total_notional_usd`;
    `buyshare = nb24 / act24`.
  - `ret24` = `close(sample) / close(24h before sample) - 1`.
- **Flag `confirmed_flow_v1` (all three, evaluated per hour cross-section so it
  self-normalizes with no look-ahead)**:
  1. `act24` in the top 30% of all symbols sampled in that same hour
     (the discovery's activity decile >= 8), AND
  2. `buyshare` in `[0.0, 0.30]` (moderate net buying, not an extreme), AND
  3. `ret24 >= 0.05` (already turning up).
- **Entry**: `entry_price = close_price` at the sample bar. Per `(exchange,
symbol)` 24h dedup cooldown: at most one entry per token per rolling 1440m, so
  a single sustained token cannot enter every hour.
- **Exit / outcome**: primary horizon `+4320m` (3 days). `exit_price` = first
  fully-closed 1m bar at or after `entry_at + 4320m` (`ceil`, never `floor`),
  labeled a proxy (`EXIT_PRICE_SOURCE_VERSION = "ohlcv_close_proxy_v1"`); an
  episode is `unresolved` if the nearest usable bar is more than
  `MAX_EXIT_BAR_GAP_MINUTES = 5.0` from the ideal boundary. 1d and 2d horizons
  are computed and reported as secondary, never gating.
- **Costs**: this codebase's shared conservative model
  (`packages/performance/schurfer_performance.calculate_performance` /
  `DEFAULT_COSTS`) -- `taker_fee_bps_per_side` on both sides plus
  `funding_cost_bps_per_8h` prorated over `duration_minutes/480`. A 3-day long
  crosses ~9 funding settlements and pumping longs often pay positive funding, so
  funding is material here and must not be dropped. `REQUIRE_FUNDING_SENSITIVITY
= True`: the report also shows the primary read at 0 and 2x the assumed funding.
- **Monster label (diagnostic only)**: `fwd_max / entry_price - 1 >= 1.0`, where
  `fwd_max` is the max close over `+1m..+4320m`. Reported to track that the tail
  is still present forward; it never gates the verdict.

## Primary and secondary estimands

- **PRIMARY (money) `absolute_net_return_v1`**: per-episode after-cost net return
  of the long, held to the 3d proxy exit. Aggregate = mean, with a cluster
  bootstrap CI (cluster by asset) via this codebase's shared `clustered_inference`
  (`BOOTSTRAP_VERSION`/`ITERATIONS`/`SEED`/`CONFIDENCE_LEVEL` frozen there).
  This is what actually fills a $300 account.
- **REQUIRED SECONDARY (signal) `debeta_excess_return_v1`**: per-episode net
  return minus the median net return of ALL symbols sampled in the same entry
  hour held the same 3d horizon. Same cluster bootstrap. This separates a real
  flag from "the market went up," and it is the metric the discovery reported.
- **Robustness (must hold for a candidate)**: the excess mean stays positive
  after excluding the top 25 single-episode winners, AND is positive in a
  majority of the cohort's UTC weeks. Both are reported numerically.

## Evidence floor

`EVIDENCE_FLOOR`: `min_resolved_episodes = 400`, `min_distinct_asset_clusters =
30`, `min_distinct_utc_weeks = 4`. Unlike the source-lead cohort's 14-asset
universe, this universe is hundreds of tokens, so the codebase-standard 30-cluster
floor is reachable and is used. Concentration caps, applied unconditionally in
addition to the floor:

- `MAX_SINGLE_ASSET_EPISODE_SHARE = 0.35`.
- `MAX_SINGLE_WEEK_EPISODE_SHARE = 0.45`.

Evaluate once, at the earliest prefix where the episode/cluster/week floors are
all met -- never re-peeked incrementally. An early look that fails the floor is
`insufficient_data`, logged as nothing.

**A verdict here does not authorize live execution.** A `candidate` result
authorizes only registering a paper-execution shadow (real fills, real costs, no
capital), the same layered-gate pattern the source-lead and maker-entry contracts
use.

## Verdict rule

`formal_verdict` (pure function), given already-aggregated statistics:

- **`insufficient_data`** if any floor is unmet, either concentration cap is
  exceeded, or a bootstrap CI could not be computed.
- **`fail`** if the floors and caps are met and EITHER the primary absolute-return
  CI lower bound is not strictly positive OR the required-secondary excess CI
  lower bound is not strictly positive OR a robustness condition fails.
- **`candidate`** only if the floors and caps are met AND the primary
  absolute-return CI lower bound is strictly positive AND the excess CI lower
  bound is strictly positive AND both robustness conditions hold -- subject to the
  no-live-execution note above.

## Required output (once the report exists)

Coverage funnel (sampled / flagged / resolved / unresolved with reasons),
per-asset and per-week concentration against the caps, the primary
absolute-return and the excess metric each with its cluster-bootstrap CI and its
cost/funding sensitivity, the top-winner-exclusion and per-week-sign robustness,
the forward monster rate diagnostic, and per-episode results -- reading the
reader module's frozen constants and pure functions directly rather than
redefining any of them.
