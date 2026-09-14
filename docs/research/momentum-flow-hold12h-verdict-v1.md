# Momentum-flow hold12h verdict v1 -- DRAFT, NOT FROZEN

> **STATUS: DRAFT FOR REVIEW -- NOT FROZEN, NOT REGISTERED.** This document is a methodology
> proposal for the HYP-015 hold-duration verdict. No constant here is authoritative yet; nothing
> may read returns against it. It is published only so the design can be reviewed before any
> contract/reader/verdict code is written. The formal cohort does not start until the eventual
> reader/verdict PR is merged (see "Formal cohort start"). Incorporates the first review's seven
> required fixes; open items are marked TBD.

## Question

HYP-015 asks whether extending the momentum-flow WATCH hold from 240m to 720m (12h) is a better
**strategy for a fixed $300 bank** after real costs -- not merely a better per-trade outcome.
The live `momentum_flow_paper_v1_hold12h` worker (contract sha `f280bd14...`) has been recording
prospective probes since 2026-09-13, but no verdict rule is registered; this contract fixes that
before the sample matures.

## Primary estimand: within-probe common-entry counterfactual (fix #3)

The two live workers (240m default, 720m hold12h) each fetch their own book independently, so
their entry times and VWAPs differ; a raw 720m-worker-minus-240m-worker difference would confound
hold duration with entry differences. Instead the **primary comparison is internal to each
hold12h probe**, which already records the 240m horizon on its own single entry:

- `net_720(p)` = the hold12h probe's realized net return under the actual 720m policy.
- `net_240cf(p)` = the counterfactual net return of a 240m policy applied to the **same entry
  VWAP and the same quote stream** (same stop rule; if the stop fires before 240m both policies
  share the identical stop outcome; otherwise the 240m policy exits at the probe's own 240m
  horizon quote).
- Paired per-probe difference `d(p) = net_720(p) - net_240cf(p)`.

The two independent workers are kept only as an **operational sensitivity**, never the primary.

## Denominator: all shared WATCH decisions, missingness in the funnel (fix #2)

No complete-case selection. The denominator is every WATCH decision on/after the formal cohort
start. Each is classified and reported, and the classification does not silently drop from the
decision:

- both policies resolvable on the hold12h probe (the analyzable pairs);
- hold12h probe rejected / stale / quote-failure at entry;
- hold12h probe opened but exit unresolved (data ends, gap, boundary tolerance);
- accounting incomplete (e.g. funding reconciliation missing for the interval).

Paired statistics are computed on the analyzable pairs; the missingness fractions remain visible
and a floor on them gates `insufficient_data`.

## Costs and ACTUAL funding (fix #4)

The current accounting model charges a fixed `5 bps / 8h` funding assumption, NOT real settlement
rates (`packages/performance/schurfer_performance/accounting.py`). A 720m hold crosses ~1-2
settlements and pumping longs often pay above 5 bps, so the fixed model is not adequate for the
primary read. **Prerequisite before freeze:** a versioned actual-funding reconciliation
(`ACTUAL_FUNDING_VERSION`) that charges observed per-interval settlement rates per instrument.
The fixed model is retained as a labelled sensitivity, not the primary cost basis.

## Required capital-time / portfolio gate (fix #5)

Per-trade EV is insufficient: a 720m hold occupies capital ~3x longer, so it can win per-trade yet
lose per-$300-bank by holding slots through subsequent WATCH decisions. The verdict therefore
requires a **fixed-bank portfolio replay** run identically for the 240m and 720m policies:

- same $300 bank, $50 per position, max 6 concurrent slots (TBD, tie to real min-order + fee);
- one **deterministic** selection policy when more WATCHes arrive than free slots (frozen here);
- report concurrency, slot occupancy, max drawdown (including floating), losing streak, and PnL
  **over the actual window** (not %/month), for BOTH policies on the same WATCH stream.

## Evidence floor (fix #6 -- TBD from a readiness count)

Do NOT copy HYP-012's 7-cluster floor (that came from its 14-asset universe). Momentum-flow's
WATCH universe is wider. Before freezing: run an **outcome-blind readiness count** (distinct
canonical assets and UTC weeks in the WATCH stream since the formal start, counts only, no
returns) and set `min_distinct_asset_clusters` to a justified value, target ~20-30. Keep
`min_distinct_utc_weeks >= 4` as an absolute minimum plus a **leave-one-week-out** sensitivity,
and concentration caps (single-asset and single-week episode-share caps, values TBD).

## Verdict outcomes (fix #7 -- differentiated, negative-EV binds first)

Evaluated by a pure function, once, at the first pre-defined decision-time prefix meeting the
floor. Precedence order:

1. `insufficient_data` -- floor or missingness thresholds not met, or a bootstrap CI cannot be
   computed.
2. `reject_hold12h` -- the standalone mature after-cost EV of the 720m policy has a 95%
   cluster-bootstrap CI upper/point read that is not positive (a losing strategy is rejected even
   if it "improves" on 240m). **This binds first among the substantive outcomes.**
3. `no_duration_improvement` -- 720m standalone EV is positive, but the paired `d(p)` CI lower
   bound is not strictly positive, or the fixed-bank portfolio does not beat the 240m portfolio.
4. `candidate` -- standalone 720m EV positive AND paired `d(p)` CI lower bound > 0 AND the
   fixed-bank portfolio economics beat 240m. A candidate authorizes only the next gate (the
   episode study / a real shadow), never live trading.

CI method: shared `clustered_inference` (bootstrap version/iterations/seed/confidence frozen
there), clustered by canonical asset.

## Formal cohort start (fix #1)

`MOMENTUM_HOLD12H_VERDICT_COHORT_START = the merge timestamp of the reader/verdict PR` (TBD at
merge). Probes before that (including the 2026-09-13 onward operational history) are
operational/readiness only, never evidence -- this costs a few days but removes any
late-preregistration dispute.

## Delivery order (proposed)

1. Review THIS draft (four questions especially: common-entry counterfactual, actual funding,
   all-WATCH denominator, capital-time gate).
2. After agreement, ONE PR: contract + reader + pure verdict + actual-funding reconciliation +
   tests (SQL against real Postgres, pairing, missingness, negative-EV precedence, deterministic
   portfolio) + the portfolio/capital-occupancy replay.
3. Merge timestamp becomes the formal cohort start.
4. Do not read returns until the outcome-blind floor is met.
5. Run the reader once, at the first pre-defined decision-time prefix.
