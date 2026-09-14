# Momentum-flow hold12h verdict v1 -- DRAFT, NOT FROZEN

> **STATUS: DRAFT FOR REVIEW -- NOT FROZEN, NOT REGISTERED.** This document is a methodology
> proposal for the HYP-015 hold-duration verdict. No constant here is authoritative yet; nothing
> may read returns against it. It is published only so the design can be reviewed before any
> contract/reader/verdict code is written. The formal cohort does not start until registration
> (see "Formal cohort start"). Incorporates the first review's seven fixes AND the second review's
> five P1 blockers (verdict precedence so negative EV cannot be masked; reproducible first-writer
> registration instead of a merge timestamp; non-circular floor with a `min_analyzable_pairs` gate;
> honest drawdown data + explicit "beats 240m" semantics; a real actual-funding contract). Open
> numeric thresholds are marked TBD, to be frozen from a pre-start outcome-blind accrual.

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
rates (`packages/performance/schurfer_performance/accounting.py`). Funding is not "1-2 rates per
12h": it accrues on the venue's ACTUAL settlement EVENTS. **Prerequisite before freeze -- a
versioned funding contract (`ACTUAL_FUNDING_VERSION`), not just a sensitivity, specifying (review
P1 #5):**

- **source**: the captured funding table (`funding_rate_snapshots`) and its exact rate column;
- **identity join**: canonical asset + instrument (venue/market_type/symbol), not a ticker string;
- **settlement inclusion**: charge every settlement event whose settlement timestamp falls in
  `(entry_at, exit_at]` (half-open), using each event's own observed rate, never a count-based proxy;
- **sign**: a long pays when the funding rate is positive and receives when negative;
- **dedup**: exactly one row per (instrument, settlement timestamp); duplicates collapsed;
- **missingness**: if any settlement in the interval has no captured rate, the probe is
  `accounting_incomplete` -- excluded from the analyzable pairs but kept in the funnel, never
  silently back-filled with the fixed model.

The fixed 5 bps/8h model is retained only as a clearly labelled sensitivity alongside the primary
actual-funding read.

## Required capital-time / portfolio gate (fix #5)

Per-trade EV is insufficient: a 720m hold occupies capital ~3x longer, so it can win per-trade yet
lose per-$300-bank by holding slots through subsequent WATCH decisions. The verdict therefore
requires a **fixed-bank portfolio replay** run identically for the 240m and 720m policies:

- same $300 bank, $50 per position, max 6 concurrent slots (TBD, tie to real min-order + fee);
- one **deterministic** selection policy when more WATCHes arrive than free slots (frozen here);
- report concurrency, slot occupancy, losing streak, and PnL **over the actual window**
  (not %/month), for BOTH policies on the same WATCH stream.

**Drawdown data honesty (review P1 #4).** Paper storage keeps per-horizon quotes and aggregated
MFE/MAE, NOT a synchronous mark-to-market series across all concurrently open positions, so a true
floating portfolio drawdown is not computable from today's data. Two honest options, decided before
freeze: (a) add a prospectively persisted portfolio mark stream, or (b) declare a **conservative
proxy** drawdown (e.g. summing each open position's worst recorded horizon-MAE within the window)
and label it as a proxy, never as the exact figure. The primary economics stand on window PnL and
occupancy; drawdown is reported at whichever fidelity is declared.

**"Beats 240m" pass semantics (review P1 #4).** Defined explicitly, not left vague: the 720m
fixed-bank window PnL must exceed the 240m fixed-bank window PnL by at least a pre-registered
**minimum dollar improvement** `MIN_PORTFOLIO_IMPROVEMENT_USD` (TBD, e.g. a fraction of the $300
bank), AND the 720m policy's declared drawdown measure must not be materially worse. Where a paired
per-decision portfolio-contribution difference is computable, its cluster-bootstrap CI lower bound
is also reported; the frozen pass rule states whether it is point-estimate or CI-based.

## Evidence floor (fix #6 + review P1 #3 -- non-circular, includes a pairs floor)

The readiness count cannot be measured "since the formal start" (the start is after freeze) --
that is circular. Instead size the floor from a **fixed pre-start operational window** (the
2026-09-13-onward operational probes up to registration), counts only, NO returns read, and freeze
all thresholds before the cohort starts:

- `min_analyzable_pairs` (economic maturity, Gate A) -- >= 100 (matches the earlier proposal);
  this is the primary maturity gate and is independent of diversity.
- `min_distinct_asset_clusters` -- a justified value from the pre-start accrual, target ~20-30
  (do NOT copy HYP-012's 7, which came from its 14-asset universe).
- `min_distinct_utc_weeks` >= 4 absolute minimum, plus a **leave-one-week-out** sensitivity.
- single-asset and single-week concentration caps (values frozen from the accrual).
- missingness ceilings on the funnel categories (rejected/stale, unresolved, accounting-incomplete)
  above which the read is `insufficient_data`.

The pre-start accrual only SIZES these numbers; they are then applied to the untouched forward
cohort.

## Verdict outcomes (fix #7 + review P1 #1 -- negative EV cannot be masked by low diversity)

Evaluated by a pure function, once, at the first pre-defined decision-time prefix. Economic
maturity is defined by a **minimum analyzable-pairs count**, SEPARATE from the diversity floor, so
that "enough trades but few weeks/clusters, and losing" resolves to a rejection, never to
"insufficient data." Ordered gates (first match wins):

1. **Gate A -- economic maturity.** analyzable pairs `< min_analyzable_pairs` (or a bootstrap CI
   cannot be computed) -> `insufficient_data`.
2. **Gate B -- negative EV binds first.** economically mature AND the standalone 720m mature
   **mean net after actual funding `<= 0`** -> `reject_hold12h`. This is checked BEFORE the
   diversity floor, so a mature-but-narrow losing sample is a rejection, not "insufficient."
3. **Gate C -- diversity / missingness floor.** clusters / UTC weeks / concentration caps /
   missingness thresholds not met -> `insufficient_data` (only reachable once EV is not negative).
4. **Gate D -- standalone significance.** standalone 720m net **95% cluster-bootstrap CI lower
   bound not > 0** (a positive point estimate whose CI still crosses zero) -> `insufficient_evidence`
   (NOT candidate).
5. **Gate E -- duration improvement + portfolio.** standalone CI lower bound > 0, but the paired
   `d(p)` CI lower bound is not strictly > 0, OR the fixed-bank portfolio does not beat 240m by the
   pre-registered minimum dollar improvement -> `no_duration_improvement`.
6. `candidate` -- standalone CI lower bound > 0 AND paired `d(p)` CI lower bound > 0 AND the
   fixed-bank portfolio beats 240m by the minimum improvement. Authorizes only the next gate (the
   episode study / a real shadow), never live trading.

CI method: shared `clustered_inference` (bootstrap version/iterations/seed/confidence frozen
there), clustered by canonical asset. Every threshold named here (`min_analyzable_pairs` and the
diversity/missingness/improvement values) is frozen before the cohort starts.

## Formal cohort start (fix #1 + review P1 #2 -- must be reproducible and immutable)

A merge timestamp cannot be frozen inside its own merge commit (it is unknown at write time and
deploy may lag). Instead, **first-writer-wins registration**: on its first execution the reader
persists `registered_at` (wall clock at first run) to a small immutable state file
(`MOMENTUM_HOLD12H_VERDICT_STATE_PATH`, on the mounted `runtime/` volume), and
`MOMENTUM_HOLD12H_VERDICT_COHORT_START` = the **next whole UTC-day boundary after `registered_at`**.
Once written the value never changes (a differing recomputation refuses the run unless an explicit,
logged re-baseline flag is passed), and it is emitted verbatim into every artifact so any reader
reproduces the same cohort. (A literal pre-chosen future UTC cutoff is the acceptable alternative.)
Probes before the cohort start -- including the 2026-09-13-onward operational history -- are
operational/readiness only, never evidence.

## Delivery order (proposed)

1. Review THIS draft (four questions especially: common-entry counterfactual, actual funding,
   all-WATCH denominator, capital-time gate).
2. After agreement, ONE PR: contract + reader + pure verdict + actual-funding reconciliation +
   tests (SQL against real Postgres, pairing, missingness, negative-EV precedence, deterministic
   portfolio) + the portfolio/capital-occupancy replay.
3. Merge timestamp becomes the formal cohort start.
4. Do not read returns until the outcome-blind floor is met.
5. Run the reader once, at the first pre-defined decision-time prefix.
