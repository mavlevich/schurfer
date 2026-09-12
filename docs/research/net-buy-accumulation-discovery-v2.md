# net-buy accumulation discovery, v2 amendment (DRAFT, for review)

Status: DRAFT amendment to `net-buy-accumulation-discovery-v1.md`, opened
2026-09-12, revised through review round 4 (rev.5 on 2026-09-12: single entry
semantics reconciled with the prod lag data, achievable bias reference, diversity
in the calibration selection, fingerprint pins the decision). Not frozen.
This proposes the methodology changes deferred out of the coverage-funnel PR
(#411). Nothing here is frozen or authorizes a formal run until this amendment is
reviewed, the open decisions below are signed off with the calculations they
require, and the code that matches it merges before the v2 decision window starts.

v1 stays frozen and unchanged. v2 is a separate
`CONTRACT_VERSION = "net_buy_accumulation_discovery_v2"` with its own scanner
path, so a v1 formal run can never execute v2 semantics and vice versa. Until the
mandatory economics/concentration metrics exist, the v2 path is calibration and
counts only, and hard-refuses `formal_run=True` in code (see "Formal-run lock").

## Evidence discipline (applies to every number in this document)

Every measured claim here must cite a reproducible artifact: the decision window,
the code revision, and the input cold-bar manifest SHA-256 hashes plus the output
fingerprint (the provenance the #411 funnel JSON already emits). Numbers quoted
below without that citation are marked `[artifact pending]` and are NOT to be
treated as frozen evidence until the artifact is attached. The coverage funnel
figures (binance 18 vs bybit 508 eligible instruments; the B-present cliff) come
from the #411 funnel run and will be re-emitted with hashes on the prod parity
run before this amendment freezes.

## Why an amendment (what v1 got wrong)

The coverage funnel and the outcome-blind calibration surfaced four concrete
problems with v1 as specified. Each needs its artifact citation before freeze
`[artifact pending for the exact percentages]`.

1. **B 100% trades_complete is infeasible.** Real capture runs at ~99.3% complete
   per minute (bybit), so a 7-day baseline never reaches 100% complete; enforcing
   it literally yields zero eligible minutes. (v1's code silently did not enforce
   it, so v1's own result ran on B-present-not-complete; text and code disagreed.)
2. **The candidate floor smuggles the diagnostic back in as a gate.** v1 calls the
   quantile spread supporting-only yet requires 150 fires per compared quantile,
   forcing about 750 total as a hard gate not derived from a power analysis.
3. **The rarity rate is not actually coverage-normalized** (the eligible-day
   denominator cancels; the decision reduces to a calendar-week fire count).
4. **Availability and unresolved-entry are unspecified in code.**

The venue concentration is a coverage fact, not a market fact (binance drops out
at "B present" from gappy minute capture), tracked as a capture-backfill lead, not
a v2 verdict input.

## Amended rules (proposed; several gated on the open decisions)

### A. Baseline B completeness AND the score estimator under partial B

Two things must be defined together, because relaxing completeness changes how
`score_m` is computed.

**Completeness gate.** B must be 100% present (all 10080 minutes have a bar) and
at least `B_COMPLETENESS_MIN_FRACTION` trades_complete (open decision 1; candidate
0.99). W keeps 100% present and trades_complete.

**Estimator (frozen with the gate).** The baseline is an explicit per-minute mean
scaled to a day, not a raw sum over a partial window:

```
baseline_daily_activity = mean(activity over present-and-complete B minutes) * 1440
score_m                 = sum(net_buy over W) / baseline_daily_activity
```

This avoids the ~1% deflation that `sum(activity)/7` suffers on a 99% window. The
W numerator stays exact (100% complete). `score_s`'s per-minute trailing-7d mean
uses the same present-and-complete convention.

**Bias (NOT assumed unbiased), against an ACHIEVABLE reference (rev.5).** The
mean-of-present estimator is unbiased only under missing-completely-at-random,
which is unlikely (incomplete minutes may correlate with load-shedding during
high-activity bursts). A fully-complete B does not exist in the data (that is the
whole reason for v2), so the reference cannot be a 100%-complete B. Instead the
bias check is a SENSITIVITY test between two achievable completeness levels: the
chosen fraction `f` and a stricter reference fraction `f_ref` (the strictest level
still present in the data, e.g. `>= 0.999`). Compute `baseline_daily_activity` on
the `f`-complete B and on the `f_ref`-complete B over the same instrument-minutes,
and require the relative shift (and the induced fire-set shift) below a stated
tolerance. If the estimate moves too much as completeness tightens, the estimator
is too missingness-sensitive: the fraction gate tightens or the relaxation is
rejected. `[artifact pending]`

**P-SHAPE trailing windows (rev.4).** `score_s`'s `elevated_buy` compares each W
minute to its OWN trailing-7d mean, so every such trailing window needs the same
completeness rule as B: it is usable only when it is 100% present and at least
`B_COMPLETENESS_MIN_FRACTION` trades_complete, using the same mean-of-present
estimator. A W minute whose own trailing-7d window fails this is not counted as
`elevated_buy` eligible (it is a coverage miss, not a silent 0).

**Reference and tolerances frozen BEFORE the run (rev.5).** The bias and stability
checks are only meaningful against fixed targets, so before any calibration run we
freeze: the reference completeness `f_ref` (the strictest ACHIEVABLE level, e.g.
`>= 0.999`, NOT an unreachable 100%), `BASELINE_BIAS_MAX` (max acceptable relative
shift in `baseline_daily_activity` between `f` and `f_ref`), and
`FIRESET_STABILITY_MIN` (min fire-count ratio and asset-overlap between the two).
These are human-frozen constants (open decisions), not tuned on the result.
`[artifact pending]`

### B. Availability as a non-backfill guard (finalization lag, W AND B)

The look-ahead risk is a bar that was backfilled after the fact: written to the
store long after its minute, so it could not have been observed in real time. The
guard is therefore per-bar and relative to the bar's OWN minute, not to `t`:

- A bar for bucket `m` is available iff it was finalized within a normal latency of
  its own close. The lag is measured from bucket END, not start (rev.4):

  ```
  bucket_end(m) = m + 1 minute
  timely_bar(m) = created_at(m) <= bucket_end(m) + MAX_FINALIZATION_LAG
  ```

  `created_at` is the bar-row write-time in the cold-bar Parquet (`SELECT *`
  export; code-confirmed as first-write / finalization, preserved on conflict) and
  `MAX_FINALIZATION_LAG` is a delay-after-bucket-end frozen from the observed lag
  distribution (open decision; `[artifact pending]`). A backfilled bar has
  `created_at` far past `bucket_end + lag` and is excluded.

- This is applied to both W and B (draft 1 wrongly exempted B: a B minute
  backfilled after `t` is future-known data and must be excluded too).
- It does NOT reference `t`, so it does not nuke the last W minute (`t-1`), whose
  `created_at` is about `(t-1) + normal_lag` and passes as long as
  `MAX_FINALIZATION_LAG` covers the normal finalization latency. The draft-1 rule
  `created_at < t` was wrong precisely because bar `t-1` finalizes at or after `t`.
- A NULL `last_trade_received_at` is never used as availability proof; a genuine
  no-trade minute is available iff its bar was finalized within lag like any other.

W and B are eligible only when all their minutes are available by this rule.
No-trade, backfill, and capture-gap cases are separately tested.

**Decision time and entry price (rev.5, resolved with the prod lag measurement).**
The signal cannot be acted on exactly at `t`: the last feature bar (`t-1`) is only
finalized at `decision_at(t) = max created_at over W,B`. The prod lag measurement
(2026-09-12, `[artifact pending: fingerprinted]`) shows the finalization lag is
tiny and tight: `created_at - bucket_start` is ~61-67s on both venues (p999 ~63s,
max ~67s), i.e. `bucket_end + 1-7s`, with zero NULLs and no backfilled bars in the
sample. So:

```
decision_at(t) = max created_at over W,B  ~=  t + 2..7 seconds
```

`close(t-1)` is the close of bar `[t-1, t)`, finalized at `created_at(t-1) ~ t +
2..7s`, which is at or before `decision_at(t)`. So `close(t-1)` IS available at the
decision instant; the timing bias is a few SECONDS against a 240-minute hold, i.e.
negligible. v2 therefore keeps a SINGLE entry/exit semantics (no two incompatible
definitions): **entry = `close(t-1)`, exit = `close(t+239)`, hold 240m** (the v1
rule), now justified by the measured lag rather than by an assumption of instant
availability. The residual ~5s and any execution latency / slippage remain
unmeasured (`capacity_unknown`) and are resolved only by the L2/latency shadow, not
by minute bars. This supersedes the rev.4 "next available minute close", which the
lag data shows is unnecessarily conservative (it would delay entry a full minute
for a ~5s effect).

### C. Unresolved entries: opportunity rate only, never the stop floor

An edge-triggered fire with no priceable entry bar is a real, counted, unresolved
fire that contributes to the opportunity-rate / rarity count and nothing else. It
is never dropped and never fabricated. It does NOT count toward the mature-negative
stop floor, which stays at least `STOP_MIN_RESOLVED_FIRES = 100` resolved fires (a
mature-negative judgment needs realized returns, which an unresolved fire has none
of). A fire is resolved only with both a priceable entry (`t-1`) and a priceable
exit (`t+239`).

### D. Rarity as a research-throughput gate (named honestly)

`too_rare` is a research-throughput gate, not an economic-uselessness signal: it
flags that evidence would accrue too slowly to conclude in a feasible window. It is
decided on a per-calendar-week fire rate:

```
too_rare  <=>  fires < RESEARCH_THROUGHPUT_MIN * (window_days / 7)
```

Candidate `RESEARCH_THROUGHPUT_MIN` is an open decision, justified by the target
evidence-accrual time and capital occupancy, not asserted as 30. A per-1000-
eligible-instrument-day rate is reported for comparability but is explicitly NOT
the gate (the denominator cancels out of a single window). The economic
materiality question stays in `ECONOMICS.md` gate 2, separate from this throughput
gate.

### E. Verdict order: mature-negative first, then sufficiency, then candidacy

The order matters so thin diversity can never let a mature negative hide behind
`insufficient_discovery` (the v1 rule, dropped in draft 1, restored here):

1. **Mature-negative stop (binds FIRST, on resolved count ALONE):** at least 100
   resolved fires AND mean `adj_return <= 0`. This requires only the resolved-count
   floor, never the diversity floor. A cohort with 100 resolved fires and a
   non-positive mean is a stop even with few clusters or weeks.
2. **Pre-outcome sufficiency (checkable before any return, only if not a stop):**
   at least 100 resolved fires AND at least 30 distinct clusters AND at least 4
   fully-covered UTC weeks with at least 20 fires each. (Resolved count uses exit
   presence, not return values.) Unmet without a mature negative is
   `insufficient_discovery`.
3. **Post-outcome candidacy (uses returns):** mean `adj_return > 0` AND the
   day-blocked bootstrap below AND leave-one-out robustness AND the tradable-
   liquidity share AND the uncertainty gate (a post-outcome criterion, not part of
   the pre-outcome floor).

Bootstrap (fully specified, frozen for v2; rev.4 makes it a proper null test):

- Statistic: the frozen strategy's mean `adj_return` over ALL resolved fires, per
  primary.
- Resample: block bootstrap, block = one UTC day, days resampled with replacement
  to their original count; the mean is recomputed per resample.
- **Null-centered p-value (rev.4)**: test H0 (mean = 0) by centering the resample
  distribution at the null (subtract the observed mean from each resample mean), so
  `p` is the share of the null distribution at or beyond the observed statistic. The
  v1 "share of resamples <= 0" is a CI-derived value, not a null test; v2 uses the
  null-centered test and reports the 90% CI separately.
- `BOOTSTRAP_ITERATIONS = 10000`; `BOOTSTRAP_SEED_V2` frozen in the contract (its
  own seed, not reused from v1).
- **Holm family of exactly two (rev.4)**: the family is always the two primaries,
  `m = 2`, fixed. A primary that has not reached its resolved floor does NOT drop
  from the family (which would loosen Holm for the other); it is a forced
  **non-rejection** (`p = 1`), so an immature arm can never help the other pass.
- A candidate needs Holm-adjusted `p <= 0.05` AND lower bound above 0 AND the
  uncertainty gate: the 90% CI half-width at most `UNCERTAINTY_MAX_HALFWIDTH_PP`
  (open decision, from the economically-meaningful MDE, NOT derivable by the
  outcome-blind tool).

The five score quantiles are a supporting diagnostic only; under-populated bins
never gate. This replaces the 150-per-quantile (about 750) rule.

## Open decisions

Split by whether a human freezes them a priori or the calibration tool derives them
mechanically (rev.4). Human-frozen constants are set before any run; derived values
are the tool's fingerprinted output.

Human-frozen (a priori, NOT tuned on the result):

0. **`BASELINE_BIAS_MAX`, `FIRESET_STABILITY_MIN`** (rule A tolerances), the
   `f_ref` reference-completeness level, the `MAX_FINALIZATION_LAG` percentile/SLA
   rule, `SIZING_MARGIN`, `MAX_WINDOW_DAYS`, `RESEARCH_THROUGHPUT_MIN`,
   `expected_unresolved_rate` (feeds `N_TARGET`; frozen with a source, not a draft
   default), the date-rounding/tie-break rules, and the economically-meaningful MDE
   - `UNCERTAINTY_MAX_HALFWIDTH_PP` (from `ECONOMICS.md` or a separate outcome-seen
     training window, never from the outcome-blind tool).

Derived / partly-derived (mechanical, `[artifact pending]`):

1. **`B_COMPLETENESS_MIN_FRACTION`** (rule A): chosen from a human-frozen grid
   (candidate 0.99), accepted only if the bias bound and stability check pass their
   (human-frozen) tolerances. `[artifact pending]`
2. **`MAX_FINALIZATION_LAG`** (rule B): frozen from the observed
   created_at-minus-bucket_start lag distribution, separating normal finalization
   from backfill. The prod measurement (2026-09-12) put normal lag at `bucket_end +
1-7s` (p999 ~63s from bucket_start, max ~67s), so a candidate of **15s past
   bucket_end** cleanly separates normal bars from backfill. Freeze with the
   fingerprinted lag artifact. `[artifact pending: fingerprinted run]`
3. **Fire thresholds `THETA_M`, `THETA_S`**: not hand-picked; the OUTPUT of the
   frozen deterministic calibration algorithm run once on the fixed scanner,
   recorded with the calibration data and code fingerprint. 0.30 and 0.35 are
   candidates only, not frozen until that artifact exists.
4. **Uncertainty gate `UNCERTAINTY_MAX_HALFWIDTH_PP`** (rule E): from a power /
   minimum-detectable-effect calculation against the ~0.225 pp round-trip cost, not
   a picked 0.5.
5. **`RESEARCH_THROUGHPUT_MIN`** (rule D): justified by target evidence-accrual time
   and capital occupancy, not asserted as 30.
6. **`SIZING_MARGIN`, `MAX_WINDOW_DAYS`** (calibration algorithm): frozen so window
   length is a function of the calibrated fire rate, with a ceiling beyond which the
   family is declared too slow rather than extended indefinitely.
7. **v2 decision window**: the mechanical OUTPUT of the frozen algorithm (decisions
   3 and 6), frozen once, never re-sized after looking.

## Formal-run lock (technical, in code)

Until the mandatory economics/concentration metrics (profit factor, drawdown,
worst losing streak, concurrency, capital occupancy, cluster collision audit,
concentration, leave-one-out output) are implemented, the v2 scanner path exposes
calibration and outcome-blind counts only, and raises rather than ever returning
`formal_run=True`. The formal read is unlocked only once those metrics land (their
own PR after this amendment's code).

## Deterministic calibration algorithm (frozen BEFORE it is run)

The selection must be a deterministic function of the data with no post-hoc human
choice. rev.4 also removes the circularity of draft 3 (a threshold chosen by fires
"in the sizing window" while the window is itself the output): the fire rate is
measured on a FIXED calibration window, and the prospective window length is then
DERIVED from that rate, not assumed.

**Human-frozen a priori (NOT derivable from data, frozen before the run):** the
threshold grid; `BASELINE_BIAS_MAX` and `FIRESET_STABILITY_MIN`; the
`MAX_FINALIZATION_LAG` percentile/SLA rule; `SIZING_MARGIN`; `MAX_WINDOW_DAYS`;
`RESEARCH_THROUGHPUT_MIN`; the economically-meaningful MDE and
`UNCERTAINTY_MAX_HALFWIDTH_PP` (these come from `ECONOMICS.md` or a SEPARATE,
explicitly outcome-seen training window, because the outcome-blind tool never reads
returns and so cannot derive them); the date-rounding and tie-break rules.

**Calibration-derived (mechanical output of the frozen algorithm):** `THETA_M`,
`THETA_S`; the chosen `B_COMPLETENESS_MIN_FRACTION` from its grid; the finalization
-lag distribution estimate; the per-primary fire rate; the derived prospective
window length.

**Algorithm (run once, no second look):**

1. Fix the calibration window and the grids (human-frozen).
2. For each threshold, compute the deduplicated fire rate and the diversity on the
   calibration window (outcome-blind).
3. Convert the resolved floor to a fire target: `N_TARGET = 100 / (1 -
expected_unresolved_rate)`, so calibration (which counts FIRES) leaves margin
   for the resolved floor of 100 (unresolved fires do not count toward it).
4. Choose the largest (most selective) threshold that can reach `N_TARGET` within
   `MAX_WINDOW_DAYS` at its measured rate, with `SIZING_MARGIN`; tie-break to the
   more selective.
5. Compute the required prospective duration SEPARATELY per primary from its rate.
6. Prospective window = the max of the two primaries' durations, rounded by the
   frozen rule. If a primary cannot reach `N_TARGET` within `MAX_WINDOW_DAYS`, the
   family is declared too slow; the window is never hand-extended.

The output triple (`THETA_M`, `THETA_S`, window) is recorded with the calibration
data and code fingerprint.

## Versioning and window plan (non-adaptive; algorithm-first)

The sequence removes the apparent chicken-and-egg: the deterministic algorithm is
frozen before any calibration output is seen.

1. Freeze this amendment's rules AND the deterministic calibration algorithm above
   (this document), reviewed. No numeric threshold or window yet.
2. Build a calibration-only tool implementing the v2 rules (estimator,
   availability, unresolved, verdict order) with `formal_run` hard-locked. It reads
   no returns; it emits outcome-blind counts.
3. Run the frozen algorithm once to get (`THETA_M`, `THETA_S`, window) plus a
   fingerprinted artifact. This is mechanical, no human choice.
4. Release the final `CONTRACT_VERSION = net_buy_accumulation_discovery_v2` with
   those frozen numbers and the artifact hash. Merge before the window start.
   Feature history reaches back the full 24h + 7d; the outcome cutoff is
   window_end + 240m. The window is never re-sized after this freeze.
5. Collect prospectively; read once matured (after the mandatory metrics unlock
   `formal_run`). A positive result is a Discovery candidate plus a prospective
   registration and an L2/spread shadow, never "net proven" (`capacity_unknown`).

## Out of scope for this amendment

- The mandatory report metrics above (their own PR, gating the formal-run unlock).
- Binance minute-bar backfill (the coverage lead from #411), a separate capture
  workstream, not a v2 verdict change.
