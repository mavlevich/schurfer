# net-buy accumulation discovery, v2 amendment (DRAFT, for review)

Status: DRAFT amendment to `net-buy-accumulation-discovery-v1.md`, opened
2026-09-12, revised twice on 2026-09-12 after review rounds 1 and 2. Not frozen.
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

**Bias (NOT assumed unbiased).** The mean-of-present estimator is unbiased only
under missing-completely-at-random, which is unlikely (incomplete minutes may
correlate with load-shedding during high-activity bursts). So it is an
approximation whose bias must be empirically bounded before freeze: reconstruct
the baseline on the full-B reference and on B with minutes dropped per the observed
missingness pattern, and require the induced shift in the baseline (and hence in
the fire set) below a stated tolerance. If the bias exceeds tolerance, the fraction
gate tightens toward 100% present or the family is not eligible for the
relaxation. `[artifact pending]`

**Stability check (required before freeze).** The fire set must be stable between
the estimator on the chosen-fraction B and the 100%-present reference (fire-count
and asset overlap within a stated tolerance). `[artifact pending]`

### B. Availability as a non-backfill guard (finalization lag, W AND B)

The look-ahead risk is a bar that was backfilled after the fact: written to the
store long after its minute, so it could not have been observed in real time. The
guard is therefore per-bar and relative to the bar's OWN minute, not to `t`:

- A bar for bucket `m` is available iff it was finalized within a normal latency of
  its own close: `created_at <= m + MAX_FINALIZATION_LAG`, where `created_at` is
  the bar-row finalization timestamp in the cold-bar Parquet (`SELECT *` export)
  and `MAX_FINALIZATION_LAG` is frozen from the observed lag distribution (open
  decision; `[artifact pending]`). A backfilled bar has `created_at` far past
  `m + lag` and is excluded.
- This is applied to both W and B (draft 1 wrongly exempted B: a B minute
  backfilled after `t` is future-known data and must be excluded too).
- It does NOT reference `t`, so it does not nuke the last W minute (`t-1`), whose
  `created_at` is about `(t-1) + normal_lag` and passes as long as
  `MAX_FINALIZATION_LAG` covers the normal finalization latency. The draft-1 rule
  `created_at < t` was wrong precisely because bar `t-1` finalizes at or after `t`.
- A NULL `last_trade_received_at` is never used as availability proof; a genuine
  no-trade minute is available iff its bar was finalized within lag like any other.

W and B are eligible only when all their minutes are available by this rule. The
finalization-lag choice and the residual timing assumption (that a bar finalized
within `MAX_FINALIZATION_LAG` was actionable at the next minute) are stated
explicitly and are a known limitation of minute-bar granularity, resolved properly
only by the L2/latency shadow. No-trade, backfill, and capture-gap cases are
separately tested.

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

Bootstrap (fully specified, frozen for v2; not left implicit):

- Statistic: the frozen strategy's mean `adj_return` over ALL resolved fires, per
  primary.
- Resample: block bootstrap, block = one UTC day, days resampled with replacement
  to their original count; the mean is recomputed per resample.
- `BOOTSTRAP_ITERATIONS = 10000`; `BOOTSTRAP_SEED_V2` frozen in the contract (its
  own seed, not reused from v1). One-sided `p` is the share of resample means at or
  below 0; the lower bound is the 5th percentile of resample means.
- Joint Holm across the two primaries; a candidate needs Holm-adjusted `p <= 0.05`
  AND lower bound above 0.
- Uncertainty gate: the 90% CI half-width at most `UNCERTAINTY_MAX_HALFWIDTH_PP`
  (open decision 4, from the MDE calculation).

The five score quantiles are a supporting diagnostic only; under-populated bins
never gate. This replaces the 150-per-quantile (about 750) rule.

## Open decisions (each needs the stated calculation before freeze)

1. **`B_COMPLETENESS_MIN_FRACTION`** (rule A): candidate 0.99, acceptable only after
   the estimator is fixed and the bias bound and the stability check pass their
   tolerances. `[artifact pending]`
2. **`MAX_FINALIZATION_LAG`** (rule B): frozen from the observed
   created_at-minus-bucket_start lag distribution (e.g. a high percentile of normal
   finalization), separating normal finalization from backfill. `[artifact pending]`
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

"Thresholds frozen from outcome-blind calibration" is not enough: the selection
must be a deterministic function of the data with no post-hoc human choice, or the
"one final freeze" is just an adaptive pick after seeing results. So the ENTIRE
algorithm is frozen first, then run once; its output is mechanical. Frozen inputs:

- **Threshold grid**: the exact finite `THETA_M` and `THETA_S` grids to search
  (e.g. `THETA_M` in {0.10, 0.15, 0.20, 0.25, 0.30}), fixed a priori.
- **Objective**: pick the largest (most selective) threshold whose expected
  deduplicated fire count over the sizing window is at least the sufficiency floor
  times `SIZING_MARGIN`, per primary. Most-selective-that-still-clears is the rule,
  so the objective is single-valued.
- **Tie-breaker**: if two grid points qualify equally, take the more selective
  (higher threshold); documented and deterministic.
- **Fixed nuisance parameters** (not searched): cooldown 24h (from v1), edge-trigger
  plus reset (v1), the concentration cap the candidacy uses, and the
  `MAX_WINDOW_DAYS` ceiling beyond which the family is declared too slow.
- **Sizing margin and window ceiling**: `SIZING_MARGIN` and `MAX_WINDOW_DAYS` are
  frozen constants (open decision), so window length is a function of the
  calibrated fire rate, not a hand pick.

Running this algorithm on the fixed scanner produces the triple
(`THETA_M`, `THETA_S`, window) mechanically, recorded with the calibration data and
code fingerprint. There is no second look.

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
