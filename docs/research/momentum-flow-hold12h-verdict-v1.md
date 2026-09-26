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
- **coverage (capture side, `hold12h_actual_funding_v2`)**: a window is recorded `complete` when
  the exact NATIVE market id was queried on the Bybit v5 funding history, every page returned
  cleanly for the requested bounds and pagination was not truncated, EVERY fetched row parsed and
  its raw item is stored, the settlements reach across both bounds (at least one at/before
  `entry_at` and one at/after `exit_at`), and a re-fetch does not contradict a stored rate. A gap
  overlapping `(entry, exit]` longer than 8h (the longest standard Bybit interval) or a settlement
  off the hour is an anomaly -> `incomplete`; passing these anomaly checks proves nothing about
  completeness. The request window is padded on each side so bracketing is possible.
- **residual risk (documented assumption, not a proof)**: `complete` assumes a fully fetched v5
  history lists every settlement in range. Bybit changes the cadence without notice (for example
  to hourly when the rate hits its cap, then back), and the endpoint returns events that
  happened, not the schedule. No cadence rule can both accept a real 4h -> 8h transition and
  detect one missing event inside an equally long gap, so a missing 04:00 on a 4h schedule would
  pass. v1 (#428) inferred one global cadence from the smallest gap and marked every real
  transition as a hole; on production data this made exactly the extreme-funding windows
  (IOST/MTL/B3) falsely `incomplete`, a missingness biased toward the cost it is meant to measure.
  v1 and v2 runs are never mixed: v2 is a new source version and every window is re-captured.
- **rate-change integrity (blocking)**: a re-fetch returning a different rate for a stored
  settlement records a `integrity_conflict` coverage run; the reader treats any `integrity_conflict`
  run OVERLAPPING an interval as invalidating every `complete` run there, so the interval is
  `accounting_incomplete` until a human resolves it. The stored value is never overwritten.
- **capture identity + scheduling**: the v5 endpoint is queried by the probe's native `market_id`
  with its market category, never through a CCXT market lookup (v1 used the unified symbol, so an
  instrument delisted after the position closed, such as ICXUSDT, could not be fetched at all). Runs on a bounded systemd timer
  (`prod-hold12h-funding-capture-*`, `--max-windows`) with a `--capture-start` boundary (ancient,
  unrecoverable history is never attempted) and a FAIR QUEUE (never-attempted then
  least-recently-attempted first) so a backlog of stuck windows can never starve fresh ones. Emits
  a JSON health summary (`pending`/`complete`/`incomplete`/`integrity_conflicts`).

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

**Adverse excursion is a diagnostic, not a gate.** Paper storage keeps per-horizon quotes and the
minimum return FROM ENTRY, not a synchronous mark-to-market series or peak-to-trough, so no honest
drawdown bound exists. The report shows `adverse_from_entry_diagnostic_v1` (worst simultaneous
from-entry excursion); the verdict does NOT gate on it. The primary economics are window PnL,
displacement (`skipped_slots_full`) and slot occupancy for both policies.

**"Beats 240m" pass semantics.** The 720m fixed-bank window PnL must be positive AND exceed the 240m
window PnL by at least `min_portfolio_improvement_usd` ($15, 5% of the bank), after the paired
`d(p)` cluster-bootstrap CI lower bound is already > 0.

**Incomplete taken slots (freeze review).** A taken slot whose pair did not resolve keeps its slot
until its policy's own exit: the 720m slot until the actual or nominal 720m exit, the 240m slot
until its observed 240m mark (or an earlier actual stop), else the nominal 240m bound -- never the
720m exit. Unproven funding cannot be bounded verifiably (Bybit changes rate caps and cadence
without notice), so `funding_bound_rule = no_registered_bound_v1`: any such slot leaves that
policy's window PnL undetermined, Gate E cannot pass, and the result is `insufficient_data` at
Gate E. Zero funding for those slots is reported only as a labelled sensitivity. Undetermined is
deliberately not NaN: a NaN would trip the integrity gate BEFORE Gate B and hide a mature negative
EV. Taken slots without a complete result above 5% (worst of the two policies) are
`insufficient_data` at Gate C. This is an honest limit of the standard and is recorded as such.

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
   OR it does not beat 240m by the minimum dollar improvement -> `no_duration_improvement`. A window
   PnL left undetermined by an incomplete taken slot -> `insufficient_data` (Gate E). Adverse
   excursion is reported, never gated.
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

## One read and operational checkpoints (freeze review)

- The contract freezes BOTH `cohort_start_iso` and `decision_prefix_end_iso` (four full ISO weeks,
  Monday 00:00 UTC to Monday 00:00 UTC). The formal CLI refuses an unregistered contract, any other
  prefix, and any read earlier than `decision_prefix_end + min_read_delay_hours` (36h: the last
  720m positions close and the funding capture passes its lag and one queue cycle; a schedule
  margin, not a guarantee under a capture backlog).
- The single read is enforced by a DURABLE claim in `app.hold12h_formal_read_claims` (migration
  0053), inserted and committed BEFORE any return is read. It is unique per cohort (contract
  version + both frozen bounds), not per chosen output directory and not per contract sha, so
  neither another directory nor an edited contract can read the same cohort again. The local
  artifact directory is additionally created exclusively. A floor not met at the prefix is
  `insufficient_data`, never a later, friendlier prefix.
- Outcome-blind health checkpoints (`hold12h-verdict-reader --health-since ... --decision-prefix-end
...`) run every Monday of the cohort and once over a fixed 48h window after the worker fix is
  deployed (before the cohort boundary is frozen). They read statuses, claim latency, funding
  coverage and accounting status only. The denominator is EVERY eligible WATCH; per worker, a WATCH
  that is stale or was never claimed is a lost entry, so a stopped worker cannot drop out of the
  check. Registered rule: the hold12h lost-entry fraction exceeds the baseline worker's by at most
  2 percentage points and never exceeds 5%. Funding coverage is counted as in the formal rule (a
  `complete` v2 run spanning the interval and no overlapping `integrity_conflict`). A breach is
  logged; thresholds are never changed and the cohort is never restarted because of it.

## Delivery order (three PRs -- supersedes the earlier ONE-PR plan)

1. **Verdict PR #427 (merged; method NOT FROZEN)**: contract, pure verdict, reader, SQL loader and
   real-PostgreSQL integration test. `formal_run` remains fail-closed without a registered funding
   source and a frozen cohort boundary.
2. **Funding prerequisite PR** (#428): the prospective per-instrument settlement capture -- schema +
   collector/resolver + bounded systemd timer/health + PostgreSQL integration tests (exact-instrument
   join, `(entry,exit]` boundaries, long sign, variable cadence, duplicate, pagination/incomplete
   window, boundary-bracket + interior-gap proof, blocking rate-conflict integrity failure that
   invalidates a prior `complete`, fair-queue that never starves a fresh window, retry/idempotency).
   Coverage is proven, never assumed (see the capture-side rule above).
3. **Freeze PR** (small): fix the funding version, the final constants (from the outcome-blind
   pre-start accrual), and the literal future UTC `cohort_start_iso`. Only data on/after it is formal;
   already-accrued probes stay operational/readiness. Read returns only once, at the first pre-defined
   decision-time prefix meeting the floor.
