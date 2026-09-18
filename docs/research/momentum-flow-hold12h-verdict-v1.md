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

## Implementation status (2026-09-18) -- DRAFT, NOT REGISTERED

The verdict FORM is now built and unit-tested; the overall HYP-015 contract stays
UNREGISTERED (provisional constants + the actual-funding prerequisite below). Modules:

- `momentum_flow_hold12h_verdict.py` -- `Hold12hVerdictContract` (thresholds + sha; the
  diversity/concentration/improvement values are PROVISIONAL, to be sized from the
  outcome-blind pre-start accrual) and the pure ordered-gate `decide_verdict` (A-E, with
  negative EV binding before the diversity floor). Not named FROZEN.
- `momentum_flow_hold12h_verdict_report.py` -- the pure reader layer: `watch_id`
  pairing, the common-entry 240m counterfactual (no look-ahead; a pre-240m exit is
  shared), ACTUAL funding over `(entry_at, exit_at]` with the long sign, the all-WATCH
  funnel (no complete-case dropping), the deterministic fixed-bank portfolio replay
  (chronological drawdown proxy + losing streak), cluster-bootstrap CI assembly, the
  first-writer cohort registration, cohort filtering, and a deterministic fingerprint.

**Actual-funding source (decided 2026-09-18): prospective capture + fail-closed.** There
is no clean per-instrument settlement series in the DB -- `funding_rate_snapshots` and
`funding_rate_history` (in `pump_derivatives_context_samples`) are both pump-EVENT
anchored, so using them for an arbitrary hold interval risks a biased complete-case
sample. They are used ONLY for coverage/readiness diagnostics and calc verification,
never primary formal evidence. The primary actual-funding source is a small prospective
per-instrument capture for the exact HYP-015 instruments (a separate prerequisite PR):
at probe open persist an immutable route (exchange, canonical instrument/market id,
native + unified symbol, market_type, entry, expected exit); after the settlement/exit
publication lag fetch the interval's funding events; store exact venue+instrument
identity, settlement_at, rate, source/observed/fetched timestamps, native payload or
checksum, capture/source version, and a coverage run (requested bounds + terminal
status); DB-unique per `(exchange, instrument, settlement_at, source_version)`; no fixed
8h -- actual timestamps + venue schedule; charge events in `(entry_at, exit_at]`, a long
debited when the rate is positive and credited when negative; an empty set is zero
funding ONLY with proven full coverage, else `accounting_incomplete`. Until this lands, a
`formal_run` FAIL-CLOSES: `NoRegisteredFundingSource` makes every probe
`accounting_incomplete`, so no return enters formal evidence.

**PR ordering (three PRs).** (1) THIS verdict PR = contract/scaffolding + reader + tests,
DRAFT / NOT FROZEN, formal fail-closed; remaining in-PR item = the SQL loader mapping the
real Postgres rows into the reader dataclasses + its real-PostgreSQL integration test
(the row-mapping choices -- isolating the ex-funding return, the canonical-asset identity,
the exit semantics -- are called out for review first). (2) the funding-capture
prerequisite PR (schema + collector/resolver + health + PG integration tests). (3) a small
freeze PR that fixes the funding version, the final constants (from the accrual), and a
literal future UTC cohort boundary; only data after it is formal.

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

- **source (SUPERSEDES the earlier `funding_rate_snapshots` idea -- see Implementation status)**: a
  NEW prospective per-instrument settlement capture for the exact HYP-015 instruments. The existing
  pump-anchored tables (`funding_rate_snapshots`, `funding_rate_history`) are diagnostics/readiness
  and calc-verification ONLY, never primary formal evidence (their conditional coverage would bias a
  complete-case sample);
- **identity join**: exact venue + instrument (canonical instrument/market id), never a base/ticker;
- **settlement inclusion**: charge every settlement event whose settlement timestamp falls in
  `(entry_at, exit_at]` (half-open), using each event's own observed rate, never a count-based proxy;
- **sign**: a long pays when the funding rate is positive and receives when negative;
- **dedup**: exactly one row per (exchange, instrument, settlement timestamp, source version);
- **missingness**: an empty set is zero funding ONLY with proven full coverage and no unaccounted
  settlement boundary; otherwise the probe is `accounting_incomplete` -- excluded from the analyzable
  pairs but kept in the funnel, never silently back-filled.

The fixed 5 bps/8h model is retained only as a clearly labelled DIAGNOSTIC, never the primary read; a
`formal_run` fail-closes until the prospective capture is the registered source.

## Required capital-time / portfolio gate (fix #5)

Per-trade EV is insufficient: a 720m hold occupies capital ~3x longer, so it can win per-trade yet
lose per-$300-bank by holding slots through subsequent WATCH decisions. The verdict therefore
requires a **fixed-bank portfolio replay** run identically for the 240m and 720m policies:

- same $300 bank, $50 per position, max 6 concurrent slots -- all FROZEN in the contract sha, with
  `position_usd * max_concurrent_slots <= bank_usd` enforced;
- one **deterministic, OUTCOME-BLIND** selection policy: consider arrivals in `(entry_at, watch_id)`
  order and SKIP when no slot is free (never a function of any return). The selection universe is
  EVERY point-in-time-eligible filled probe (resolved or not), so a slot an eventually-unresolved
  position held is never freed by hindsight; a taken-but-unresolved slot fails the window closed;
- **same-asset concurrency is FROZEN as ALLOWED** (`allow_concurrent_same_asset = True`), matching the
  live 360m cooldown which permits two 720m positions of one canonical asset to overlap;
- report concurrency, slot occupancy, losing streak, and PnL **over the actual window**
  (not %/month), for BOTH policies on the same WATCH stream.

**Drawdown data honesty (review P1 #4).** Paper storage keeps per-horizon quotes and aggregated
MFE/MAE, NOT a synchronous mark-to-market series across all concurrently open positions, so a true
floating portfolio drawdown is not computable exactly from today's data. RESOLVED (code chose ONE):
`drawdown_method = conservative_simultaneous_mae_v1` -- the largest total `|MAE|` of positions open at
the same instant (assuming every concurrently-open position hits its own worst excursion together).
This OVERSTATES the true floating drawdown, so it is a genuine conservative bound (a realized-close
series would understate it and is NOT used). Frozen into the contract sha and used as the Gate E risk
check. The primary economics stand on window PnL and
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

0. **Gate 0 / 0b -- integrity.** any non-finite input, OR ANY integrity failure (a NaN/invalid
   row, a non-positive notional, a duplicate settlement) regardless of fraction -> `insufficient_data`.
1. **Gate A -- economic maturity.** analyzable pairs `< min_analyzable_pairs` -> `insufficient_data`.
   Depends on the pair COUNT ONLY (NOT on the CI being computable), so the mean still reaches Gate B.
2. **Gate B -- negative EV binds first.** economically mature AND the standalone 720m mature
   **mean net after actual funding `<= 0`** -> `reject_hold12h`. The mean needs no clusters, so this
   is checked BEFORE the diversity floor AND before the CI: a mature-but-narrow losing sample is a
   rejection, not "insufficient."
3. **Gate C -- diversity / missingness floor.** clusters / UTC weeks / concentration caps /
   missingness thresholds not met -> `insufficient_data` (only reachable once EV is not negative).
4. **Gate D -- standalone significance.** the cluster-bootstrap CI is not computable (too few
   clusters), OR the standalone 720m net **95% CI lower bound not > 0** -> `insufficient_evidence`.
5. **Gate E -- duration improvement + PROFITABLE portfolio.** paired `d(p)` CI lower bound not > 0,
   OR the 720m fixed-bank window PnL is not itself positive (beating a losing 240m is not an edge),
   OR it does not beat 240m by the minimum dollar improvement, OR its conservative drawdown is
   materially worse -> `no_duration_improvement`.
6. `candidate` -- all of Gate E satisfied. Authorizes only the next gate (the episode study / a real
   shadow), never live trading.

CI method: shared `clustered_inference` (bootstrap version/iterations/seed/confidence frozen
there), clustered by canonical asset. Every threshold named here (`min_analyzable_pairs` and the
diversity/missingness/improvement values) is frozen before the cohort starts.

## Formal cohort start (fix #1 + review P1 #2 -- reproducible and immutable)

**RESOLVED (supersedes the earlier first-writer-only wording).** The FORMAL boundary is a LITERAL,
pre-chosen future UTC instant, frozen as `cohort_start_iso` in the contract sha by the freeze PR;
`formal_run` fail-closes while it is unset. The formal window is the HALF-OPEN interval
`[cohort_start, decision_prefix_end)` -- both bounds enforced. Runtime first-writer-wins registration
(atomic `O_CREAT | O_EXCL`; the first run is registration-only and exits before reading any return)
remains ONLY as a DRAFT/readiness convenience and never substitutes for the frozen literal. Probes
before the boundary -- including the 2026-09-13-onward operational history -- are operational/readiness
only, never evidence.

## Delivery order (three PRs -- supersedes the earlier ONE-PR plan)

1. **This verdict PR** (DRAFT / NOT FROZEN): contract + pure verdict + the pure reader layer + tests.
   Remaining in-PR item after this method review: the SQL loader mapping real Postgres rows into the
   reader dataclasses + its real-PostgreSQL integration test. `formal_run` fail-closes (no registered
   funding source).
2. **Funding prerequisite PR**: the prospective per-instrument settlement capture -- schema +
   collector/resolver + health + PostgreSQL integration tests (exact-instrument join, `(entry,exit]`
   boundaries, long sign, variable cadence, duplicate, pagination/incomplete window, no-event with vs
   without proven coverage, retry/idempotency).
3. **Freeze PR** (small): fix the funding version, the final constants (from the outcome-blind
   pre-start accrual), and the literal future UTC `cohort_start_iso`. Only data on/after it is formal;
   already-accrued probes stay operational/readiness. Read returns only once, at the first pre-defined
   decision-time prefix meeting the floor.
