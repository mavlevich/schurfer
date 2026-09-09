# HYP-027 -- Two groups the score cannot tell apart, and whether their outcomes differ

**Status: registered 2026-09-09, before any outcome in the window below was
joined to any age group.**

Bound by [entry-signal-family-rules-v1.md](entry-signal-family-rules-v1.md): the
unit of observation is the episode, floors count episodes and asset clusters, a
negative verdict needs as much data as a positive one, no outcome may straddle a
window boundary, and a feature counts only if it was available when the decision
was made.

## Where this comes from

HYP-023 read six score components against forward outcome and reported `pump_age`
as a candidate. That candidate was **withdrawn** before publication: `pump_age`
carries 142 distinct values across 821 episodes, 368 of them reading exactly 0.6
minutes, and all four quintile boundaries fell inside a tied value. The
monotonicity was a property of the sort order, not of the component.
See [score-components-v1.md](score-components-v1.md).

One claim survived that withdrawal, and it does not depend on the broken
partition. It is narrower and sharper than the one first written down, and this
is its registration.

## Amended 2026-09-09, before any outcome in this window was joined

**`pump_age` does not measure the pump's age.** Traced through
`apps/api-gateway/internal/pumps/handler.go`: the value is hours since
`signalStrategyAnchorAt`, which is `entry_qualified_at` when the episode has one
and `first_seen_at` otherwise. On all **821** discovery episodes the anchor was
`entry_qualified_at`, uniformly, so there is no mixed-anchor problem -- but the
quantity is **time since the system qualified the episode for entry**, not time
since the pump began in the market.

That changes what the two groups mean, and the change is recorded here rather
than discovered while reading a result:

- Group A is not "young pumps". It is episodes **decided on essentially the first
  pass after qualification**.
- Group B is not "older pumps". It is episodes that **stayed qualified and were
  re-evaluated for minutes without being opened**.

The test itself is untouched: both groups still score zero, and the question is
still whether two groups production cannot distinguish have different outcomes.
Only the interpretation changes, and it must change before the read rather than
after.

It also raises the stakes on the surrounding code, which is a separate claim with
its own evidence and not part of this contract. The score's own notes read
"extended pump (%.1fh), high time risk" and "early pump (%.1fh), may continue",
and its thresholds sit at 1 and 4 hours. Those are descriptions and cutoffs for a
pump's market age. The variable they are applied to is a system-observation
delay.

## The claim

Production's scoring code buckets pump age like this
(`apps/api-gateway/internal/pumps/handler.go`):

```
AgeHours > 4      -> 2 points   "extended pump, high time risk"
1 < AgeHours <= 4 -> 1 point    "pump maturing"
AgeHours <= 1     -> 0 points   "early pump, may continue"
```

**692 of the 821 discovery episodes were decided within an hour of qualifying.**
They all scored zero on this component, so as far as the composite is concerned they are the same
episode. Meanwhile the entire measured spread of ages in that mass runs from zero
to a few minutes.

The claim is therefore not "the score is inverted". It is:

> Within the sub-hour mass that production scores identically, pump age separates
> forward outcome. The bucketing collapses the whole informative range into one
> bucket, and a score cannot rank on a distinction it does not represent.

## The episode's decision is chosen before any outcome is joined

Also from review, and the sharpest of the three. The first implementation
filtered to decisions with a completed outcome and only then applied "the first
decision that opened something, else the earliest". An episode whose first
`opened_paper` decision was unresolved came back represented by a later `skipped`
one -- at a different age, and therefore in a different group.

Measured on the HYP-023 window: 24 of 822 episodes were substituted, and **19 of
those crossed this contract's own 0.6-minute boundary**. The substitution also
disappears as outcomes resolve, so the same contract over the same window would
measure a different population depending on the day it ran.

The rule now runs in SQL before the outcome is joined (`episode_selection.py`).
An episode whose own decision has no completed outcome is coverage: dropped,
counted, printed, never replaced.

## Population

`app.trade_decisions` with `strategy_version = 'pump_short_v1_market_quality'`.
One decision per episode, chosen first: the first that opened something, else the
earliest, the same rule as the replay and HYP-023. That decision's own outcome at
the **60-minute** horizon is then joined, and an episode whose decision has no
complete outcome is coverage rather than a substitution.

60 minutes because that is the window a decision can act inside: production's
exit closes an unactivated position there.

## The two groups, fixed before any outcome was read

Absolute cutoffs, taken from the tied groups the discovery window revealed. They
are not recomputed per window and no quantile is involved, which is what the
withdrawn result got wrong.

| Group | Age at decision            | Production points |
| ----- | -------------------------- | ----------------: |
| **A** | at most 0.6 minutes        |                 0 |
| **B** | above 0.6 up to 60 minutes |                 0 |

**Both groups score zero.** That is the whole design: production assigns them the
same contribution, so if their outcomes differ, the difference is invisible to
the score by construction rather than by weighting.

Episodes over 60 minutes old are **excluded**, not reported. They are the ones
production does score, and testing them is a different question with a different
window; measuring them here descriptively would spend their data before that
contract exists.

## Primary metric, declared before reading

**Median net short return of group A minus median net short return of group B**,
at the 60-minute horizon, net of the shared cost model
(`conservative_costs_v1`, about 0.21 points for a 60-minute round trip).

**The direction is declared in advance: A above B.** Discovery is what a
confirmation takes its direction and its effect size from, and having taken both,
this test is one-sided. A large difference in the other direction is a refutation,
not a finding.

## Secondary, context only

Episode and cluster counts per group, the interquartile range of net return in
each, and the median forward MFE and MAE. None of these can change the verdict.

## Sufficiency, checked before either verdict

**At least 150 episodes and 30 asset clusters in each group.** As of registration
the window holds 254 episodes in A and 119 in B, so B is the binding one.

## When the read happens

The window **opens 2026-08-25**, the boundary HYP-023 held out, and its hard
outer edge is **2026-09-30**. The contract is not edited in between: the read
takes whatever falls inside those bounds at the moment it runs, and the artifact
records the latest decision timestamp it actually included.

The trigger to run it is the floors. A daily check counts episodes per group and
nothing else: it does not join an outcome, compute a metric, or look at a return.
At about eight qualifying episodes a day, group B should clear 150 a few days
after registration.

That separation is enforced rather than promised. `make prod-pump-age-readiness`
runs a mode with no code path to a median, and `make prod-pump-age-read` is the
formal measurement. Neither has a default: a run has to say which one it is.

Below the floors the read computes **no outcome statistic at all** -- not
suppressed at print time, not computed. The first implementation calculated
medians, quartiles and the difference and then printed them beside the word
`inconclusive`, which spends the window while claiming not to.

**There is exactly one read**, and the sample it measured is frozen with
`freeze_or_verify_sample` so that a second run has to admit it is one. The
manifest is a fixed path, not a flag to remember: when it was optional the
ordinary run was the unfrozen one, and two consecutive runs on different samples
both printed a result under this contract with nothing recording it. What is
frozen is the episodes **and the decisions they were measured through**, because
the same `pump_event_id` measured through a different decision is a different
measurement, at a different age, in a different group. If the
floors are still unmet on 2026-09-30 the window closes anyway and the verdict is
`inconclusive`: an open-ended wait for a sample to become convincing is the same
error as an open-ended search for a threshold.

## Decision rule, declared before reading

- **Insufficient** first: below the floors in either group, the verdict is
  `inconclusive` and nothing else is computed.
- **Confirmed** if the difference is **at least +1.0 percentage point** in the
  declared direction.
- **Refuted** if it is **-1.0 point or worse**, that is, older-inside-the-hour
  does better.
- **Inconclusive** between those.

  1.0 point rather than HYP-023's 1.5: the two thresholds measure different things.
  1.5 was for the best of six components across a five-way split, carrying a
  multiplicity correction. This is a single pre-specified two-group comparison in a
  declared direction, so the bar is set on economics instead: median net return in
  this cohort is on the order of +1 point, and a shift of a full point roughly
  doubles it while being about five times the round-trip cost.

## What this pass may not do

It may not move a cutoff, add a third group, change the horizon, or re-split
group B after seeing the result. It may not be read again on a later window under
this contract: a second read is a new contract on data this one did not include.

It may not be read as evidence of a tradeable edge. A relationship between a
decision-time value and a forward price move is not a strategy, and a confirmed
result authorizes exactly one thing: a registered design for an age gate, which
is a further hypothesis with its own window.

## Confounds, stated before the result so they cannot be chosen afterwards

**Age at decision is not a property anyone selected, and it is not the pump's
age.** It is how long the episode had been qualified for entry when the decision
was taken, so it can proxy for how long the episode kept meeting the entry
condition without being opened, for scan cadence, for how quickly a venue's data
arrives, or for which assets resolve fast. A confirmed difference would still be
actionable -- refusing an entry is something this system can do -- but the cause
would not be established by it, and "young pumps are better" is specifically not
what it would show.

**The age distribution moved between the windows.** In discovery the 60th
percentile of age was 1.2 minutes; in this window it is 0.6. The comparison is
strictly within-window for that reason, and no discovery number is carried into
the arithmetic. Only the cutoffs are carried.

## What has already been seen in this window, disclosed

This window is not virgin ground, and the parts that have been read are named
here rather than left to be discovered later.

1. **HYP-022 read it** for exit-policy performance. That is a different
   measurement: no relationship between a decision-time feature and a forward
   outcome was computed.
2. **This registration used its age distribution.** Planning the floors required
   knowing how many episodes fall in each group, so percentiles and counts of
   `pump_age` were queried here on 2026-09-09: 254 in A, 119 in B, 45 above an
   hour, and the age percentiles quoted above. The anchor check that produced the
   amendment above was run on the discovery window only. **No outcome value was
   selected, joined by group, or displayed in either window.** A sample-size
   count is not a result, but it is a look, and it is recorded as one.

Neither of those touches the quantity this contract grades. What cannot be
claimed afterwards is that the window was untouched.
