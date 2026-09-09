# HYP-023 — Are the score's ingredients informative even though the score is not

**Status: registered 2026-09-08, before any component's relationship to outcome
was computed.**

> **Amended 2026-09-08, before any outcome was joined.** The family rules in
> [entry-signal-family-rules-v1.md](entry-signal-family-rules-v1.md) bind this
> contract and override anything below that contradicts them: the unit of
> observation is the episode and not the decision, the floors count episodes and
> asset clusters, a negative verdict needs as much data as a positive one, no
> outcome may straddle a window boundary, and a feature counts only if it was
> available when the decision was made.
>
> **Amended again 2026-09-09, and for THIS hypothesis that is after the result
> was read, not before.** Rule 6 came out of the withdrawal recorded below, so
> calling it a pre-registration here would be a false journal entry. It is a
> post-result check, admissible for one reason only: it can withdraw a candidate
> and can never create one, and there is a test asserting that. The artifact
> computed under the previous contract is kept beside the corrected one. For
> HYP-024 and HYP-025, neither of which is implemented, the same rule genuinely
> is a pre-registration.

## Family declaration, read this first

This is one of **three** hypotheses registered together on 2026-09-08 (HYP-023,
HYP-024, HYP-025), all searching the same recorded data for entry signal. That
matters for how any single result is read: three independent searches produce a
"finding" at a 5% threshold about 14% of the time by chance alone.

So a positive result in one of the three is **weaker evidence than the same
result would be from a single registered search**, and each of the three carries
the same held-out window requirement. None of them promotes anything to
production on a discovery-window result.

## What is already established

HYP-019 read the composite score and found it ranks candidates backwards.
HYP-020 found the ordering survives the stop while the edge does not.

Neither asked about the ingredients. Every decision records the score's
components separately:

`pump_age`, `funding_rate`, `price_extent`, `oi_trend`, `retrace_from_peak`,
`mad_score`.

62,168 decisions carry them for `pump_short_v1_market_quality` since 2026-08-01.

## Question

Does any single component have a monotone relationship with forward outcome that
the composite does not?

The two possibilities this separates are worth different things. If no component
is informative, the ingredients are wrong and the score's weighting is beside the
point. If one is informative while the composite is not, the weighting is
destroying signal that is already being collected -- a much cheaper thing to fix.

`mad_score` is present on only 4,067 of 62,168 decisions. That coverage gap is
reported and the component is analysed on its own subset, never merged into a
comparison with components that have full coverage.

## Which number a component is, decided before any relationship was computed

The first run crashed before computing anything, and the crash was informative:
five of the six components are not numbers. They are objects carrying both

- `value`, the raw measurement in the component's own units (`oi_trend` 321.74
  percent, `pump_age` 7.34 hours), and
- `points`, the discretised 0 to 2 contribution the composite actually sums.

`mad_score` is a bare float.

The registration said "components" and did not distinguish these, which is an
ambiguity in the contract rather than in the data. Resolved here, before any
component was related to any outcome:

**This study reads `value`.** The ingredient is the measurement. `points` is the
composite's own discretisation, and HYP-019 already found the composite ranks
backwards, so measuring `points` would be asking the question that has been
answered. Reading `value` is what makes "the ingredients carry signal the
weighting discards" answerable at all.

Reading both would double a search that already carries a family correction: six
components read two ways is twelve searches wearing the costume of six. Which
part of the machinery loses the signal, the bucketing or the weights, is a
separate hypothesis with its own id.

**What the crashed run saw:** the SQL executed and returned rows. No component
was related to any outcome, no statistic was computed, and no value was
displayed. The window is not spent.

## Population

`app.trade_decisions` with `strategy_version = 'pump_short_v1_market_quality'`,
joined to `app.trade_decision_outcomes` at the **60-minute** horizon, complete
outcomes only.

60 minutes because that is the window a decision can actually act inside: the
production exit closes an unactivated position there, so a relationship that only
appears at eight hours cannot be traded by this system as built.

## Windows

- **Discovery:** `2026-07-29` to `2026-08-25`, half-open.
- **Held out, not read in this pass:** `2026-08-25` onward.

The same split as HYP-022, deliberately: a component that looks informative here
and fails there has been found by the same window that produced it.

## Primary metric, declared before reading

For each component independently: **the spread in median net short return
between its top and bottom quintile**, at the 60-minute horizon, net of the
shared cost model.

Quintiles of the component's own distribution on the discovery window, fixed
before the outcome is joined.

## Secondary, context only

Median forward MFE and MAE per quintile, and the monotonicity of the metric
across all five. A relationship that is strong only between the extremes and
non-monotone in between is reported as such and is not treated as a signal.

## Decision rule, declared before reading

- **Insufficient** before anything else: below **150 episodes** in either
  compared quintile, or below **30 asset clusters** across them, the verdict is
  `inconclusive` in both directions. This is checked first, so a component with
  poor coverage cannot become a negative result through absence of data --
  `mad_score` is recorded on 4,067 of 62,168 decisions and would have been the
  first casualty.
- **A component is a candidate** if its top-to-bottom quintile spread exceeds
  **1.5 percentage points** of median net return **and** the relationship is
  monotone across all five quintiles. A candidate earns a read of the held-out
  window, nothing more.
- **The ingredients are not the problem** if no component clears 0.5 points,
  and only where every component cleared the sufficiency floor above. The
  score's weighting is then not what is destroying the signal, because there is
  no signal in the parts either.
- **Inconclusive** otherwise.

  1.5 points rather than something smaller: it must survive a round trip of costs
  with room to spare, and it is being selected as the best of six components,
  which is itself a search.

## What this pass may not do

It may not construct a new composite from whatever wins, may not tune quintile
boundaries, and may not report the best of six as though six had not been
examined. Any weighting derived from this is a new hypothesis on an untouched
window.

It may not be read as evidence that a component is tradeable. A relationship
between a decision-time value and a forward price move is not a strategy: there
is no execution, no fill, and no position sizing in this measurement.

---

# Result, 2026-09-08

> **Corrected 2026-09-09, before publication. The `pump_age` candidate below is
> withdrawn.** All four of its quintile boundaries fall inside a single tied
> value, so the partition the monotonicity was read across is imposed by the
> sort rather than by the component. See
> [Correction](#correction-2026-09-09-the-partition-was-not-the-components) at
> the end. The table is kept as it was computed, because a withdrawn number that
> disappears cannot be checked.

Run on production at `e2d563c`, contract `HYP-023` `d067f0542ae85115`.
Window `2026-07-29` to `2026-08-25`, horizon 60 minutes.
**821 episodes**, one decision each, after dropping those whose outcome window
runs past the window end.

Artifact preserved at `backups/reports/hyp023/hyp023-discovery-d067f054.md`.

## Verdicts

| Component         | Coverage |    Spread | Monotone | Verdict                       |
| ----------------- | -------: | --------: | -------- | ----------------------------- |
| **pump_age**      |      821 | **-2.89** | **yes**  | **candidate**                 |
| funding_rate      |      821 |     -1.93 | no       | inconclusive                  |
| oi_trend          |      821 |     -1.84 | no       | inconclusive                  |
| retrace_from_peak |      821 |     -0.85 | no       | inconclusive                  |
| price_extent      |      821 |     +0.40 | no       | no_signal                     |
| mad_score         |       83 |     -4.21 | no       | inconclusive, below the floor |

## The answer to the question that was asked

**The ingredients are not empty.** `pump_age` separates the cohort by 2.89
percentage points of median net return, monotonically across all five
quintiles, on 821 episodes with roughly a hundred distinct asset clusters in
each:

|     Quintile | Episodes | Clusters | Median net |
| -----------: | -------: | -------: | ---------: |
| 1 (youngest) |      164 |       96 | **+2.68%** |
|            2 |      164 |      104 |     +1.41% |
|            3 |      164 |       93 |     +0.86% |
|            4 |      164 |      110 |     +0.81% |
|   5 (oldest) |      165 |      108 | **-0.21%** |

Younger pumps short better, and the relationship does not reverse anywhere.

> **Wrong on two counts, corrected below.** The partition was decided by sort
> order rather than by the component, and `pump_age` does not measure a pump's
> age at all.

## What the scoring code does with that

From `apps/api-gateway/internal/pumps/handler.go`, a fact about code rather than
a claim about outcomes:

```
AgeHours > 4      -> 2 points   "extended pump, high time risk"
1 < AgeHours <= 4 -> 1 point    "pump maturing"
AgeHours <= 1     -> 0 points   "early pump, may continue"
```

**The score awards its maximum on this component exactly where the measured
outcome is worst.** An older pump scores higher, a higher score is likelier to
clear the threshold and trade, and the oldest quintile is the only one with a
negative median.

That is a concrete mechanism for HYP-019's finding that the composite ranks
candidates backwards, and it was not visible from the composite.

## What this does not establish

**It is one of six components.** The composite sums all of them, so this
explains a contribution to the inversion, not the whole of it. Three other
components show spreads between 0.85 and 1.93 points that are not monotone, and
one shows nothing.

**The mechanism is a hypothesis formed after seeing the result.** The
measurement is registered; the explanation of why the scoring code produces it
is not, and must not be treated as though it were.

**A candidate earns a read of the held-out window, nothing more.** It does not
authorize changing the score, inverting the component, or trading on it.

**The largest spread in the table is the one that was refused.** `mad_score`
shows -4.21 on 83 episodes, 16 and 17 in the compared quintiles against a floor
of 150. Under the contract as first drafted that would have been eligible and
would have been the headline. The floor a colleague insisted on caught it on the
first run.

## The held-out window is not pristine, and this has to be said

The registration named `2026-08-25` onward as held out for this hypothesis. That
period has since been read by HYP-022, for a different measurement: exit-policy
performance, not any relationship between a decision-time feature and a forward
outcome.

So no statistic bearing on this question has been seen there, but the period is
not untouched in the way the word normally implies. A confirmation read under
this contract would be weaker than one on genuinely unseen data, and the only
genuinely unseen data is what has not happened yet.

Whether that is good enough is a judgement, and it is recorded here rather than
resolved quietly.

---

# Correction, 2026-09-09: the partition was not the component's

Found while designing the held-out read, before this record was published and
before the holdout was touched. The check that found it was a power calculation:
how many episodes land in each of production's own age buckets. It returned a
distribution that makes the result above unreadable as stated.

## What the data actually looks like

`pump_age` is stored in hours and rounded to a hundredth of one
(`math.Round(hours*100)/100`), so its granularity is **0.6 of a minute** and every
value is a multiple of it. Decisions follow within about half a minute of the
thing it counts from. Across the 821 discovery episodes there are **142 distinct
values**, and three of them hold 629 episodes:

| Age at decision | Episodes |
| --------------: | -------: |
|         0.6 min |  **368** |
|         1.2 min |      141 |
|         0.0 min |      120 |

A quintile is 164 episodes. So the sorted order is 120 zeros, then 368 identical
0.6s spanning positions 121 to 488, then 141 identical 1.2s to position 629.
Laying quintile boundaries at 164, 328, 492 and 656 puts them here:

| Quintile | Positions | Value range        | Boundary that follows |
| -------: | --------: | ------------------ | --------------------- |
|        1 |     1-164 | 0.0 to 0.6 min     | 0.60 / 0.60, **tied** |
|        2 |   165-328 | **0.6 to 0.6 min** | 0.60 / 0.60, **tied** |
|        3 |   329-492 | 0.6 to 1.2 min     | 1.20 / 1.20, **tied** |
|        4 |   493-656 | 1.2 to 4.8 min     | 4.80 / 4.80, **tied** |
|        5 |   657-821 | 4.8 to 1522.8 min  | -                     |

**Quintile 2 is one value wide, and all four boundaries fall inside a tied
value.** Every cut in the partition was decided by tie order. Quintiles 1, 2 and
3 all draw from the same 0.6-minute group.

## Why that withdraws the candidate

Which side of a boundary a tied episode lands on is decided by the sort's tie
order, which here is episode id. The medians of quintiles 2 and 3, +1.41% and
+0.86%, are two arbitrary halves of one group of identical measurements. Their
difference is noise, and it happened to point the way the neighbouring buckets
already pointed, which is what produced the `monotone` verdict.

A different tie order breaks the monotonicity without any of the data changing.
So `monotone: yes` was a property of the sort, not of `pump_age`, and the
`candidate` verdict that depended on it does not stand.

`oi_trend` is worse on the same measure: **606 of 821 episodes share one value**.
Its number was already `inconclusive` for non-monotonicity, so nothing was
claimed from it, but the number itself meant nothing either.

Re-run at `b9ce265` under contract v3, which is what the corrected artifact
records:

| Component         | Distinct | Largest tie | Tied boundaries | Verdict now                   |
| ----------------- | -------: | ----------: | --------------- | ----------------------------- |
| pump_age          |      142 |     **368** | **4 of 4**      | inconclusive                  |
| funding_rate      |      431 |         121 | **2 of 4**      | inconclusive                  |
| oi_trend          |      211 |     **606** | **3 of 4**      | inconclusive                  |
| retrace_from_peak |      627 |          85 | none            | inconclusive, not monotone    |
| price_extent      |      657 |           5 | none            | no_signal                     |
| mad_score         |       83 |           1 | none            | inconclusive, below the floor |

**Three of the six have unusable partitions, not two.** `funding_rate` was the
surprise: its largest tied group is 121, smaller than a 164-episode quintile, and
I had assumed that made it safe. It does not. A tied group narrower than a bucket
can still straddle a boundary, and two of its four did. The rule has to be stated
in boundaries rather than in group sizes, which is how the check is written.

Artifact preserved at
`backups/reports/hyp023/hyp023-discovery-923c97a7-separated.md`, beside the
original.

## What was fixed

Contract v3, and a check in the study that sits beside the sufficiency floors
because it exists for the same reason:

- every quintile's value range is now recorded and reported;
- a `candidate` requires that **every adjacent pair of quintiles differ in the
  component**, not only that the medians move one way;
- the artifact prints distinct values and the largest tied group per component,
  so a coverage count of 821 can no longer stand in for 821 measurements.

The change is applied to a window that has already been read, which normally is
not allowed. The reason it is allowed here is an asymmetry that can be checked
rather than trusted: **the check can only withdraw a candidate, never create
one.** There is a test asserting exactly that.

## What survives

Two things, and they are worth separating from what does not.

**The old episodes really are the worst, and they really are the only ones the
score rewards.** This part does not depend on the broken partition, because
production's own cutoffs define the groups: 1 point above 1 hour, 2 points above
4 hours. Both sit far above quintile 5's lower edge of 4.8 minutes, so every
episode earning any `pump_age` points at all falls inside the one quintile with a
negative median.

**But that is 129 episodes out of 821.** 692 of 821 discovery episodes were
decided within an hour of qualifying and score **zero** on this component. So the mechanism stated in the
first record -- the score awarding its maximum where the outcome is worst -- is
real in direction and small in reach: on 84% of the cohort `pump_age` contributes
nothing to the composite at all. It is not the explanation of HYP-019's
inversion; it is at most a contributor on a sixth of it.

The sharper reading, and the one worth a registered test, is a different claim
than the one first written down: the informative variation in pump age lives
between zero and five minutes, and production's bucketing collapses that entire
range into one bucket. A score cannot rank on a distinction it does not represent.

## What this costs

The discovery window has now produced no candidate. The held-out window is
untouched by this hypothesis and stays that way, which is the one piece of good
news: the defect was found before it was spent.

A registered test of the surviving claim needs a partition the data can support
-- the tied groups themselves are natural buckets, and there are three large ones
-- and needs declaring before any outcome in the holdout is read. That is a new
contract, not a re-read of this one.

---

# Second correction, 2026-09-09: the variable is not what its name says

Found while amending HYP-027, and it is worth separating from the tie defect
because it survives it. Neither correction depends on the other.

`pump_age` is hours since `signalStrategyAnchorAt`
(`apps/api-gateway/internal/pumps/handler.go`), which is `entry_qualified_at`
when the episode has one and `first_seen_at` otherwise. On all 821 discovery
episodes the anchor was `entry_qualified_at`.

**So the quantity is time since the system qualified the episode for entry, not
time since the pump began in the market.** Everything above that reads it as a
pump's age is wrong, including the sentence "younger pumps short better" and the
phrase "under an hour old". The correct reading of the concentration is that
**decisions follow qualification within about 36 seconds**, which is a fact about
the scanner's cadence rather than about how old pumps are when the system meets
them.

## What that does to the mechanism claim

It sharpens it into something more specific than "the score is inverted".

The scoring code's own notes read `"extended pump (%.1fh), high time risk"` and
`"early pump (%.1fh), may continue"`, and its thresholds sit at 1 hour and 4
hours. Those are descriptions and cutoffs for a pump's **market age**. The
variable they are applied to is a **system-observation delay**.

An episode scores 2 points for "extended pump, high time risk" when it has been
sitting in the qualified state for four hours, which is not the same claim at
all and may be nearly the opposite one. 84% of episodes score zero not because
pumps are young when the system meets them, but because the scanner decides
almost immediately after qualification.

**This is a claim about code and about what a stored number means, and it is
checkable.** It is not a claim about outcomes: no relationship between this
variable and any forward return survives the tie defect above. The two findings
are independent, and neither rescues the other.

## What it does not answer

Whether scanning more often would help. `pump_age` cannot speak to detection
latency in either direction, because it starts counting at qualification rather
than at the pump. The question is open and this variable is not the instrument
for it.
