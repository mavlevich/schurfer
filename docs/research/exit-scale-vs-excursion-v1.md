# HYP-022 — The exit parameters are scaled past the moves they are meant to catch

**Status: registered 2026-09-08, before any variant's return was computed.**

## What is already established, and is not the hypothesis

Three numbers from HYP-021's run, all facts about code and measured output
rather than claims:

1. Mean favourable excursion across the family is **7.51%**; mean adverse
   excursion is **4.64%**. The price paths in this cohort are asymmetric in our
   favour.
2. The smallest activation threshold in the production bracket is **8%**, and
   the trail widths are **12 / 15 / 20%**.
3. All six policies in the registered family share that same scale, and all six
   lose money: profit factor 0.74 to 0.85.

So the mean favourable move is smaller than the smallest threshold that would
start trailing it. `not_activated` fires on 423 of 837 episodes and `max_hold`
on 343 of 837 for the baseline, while `trailing_stop` fires on 227.

## Question

Does an exit whose activation and trail are set from the observed excursion
distribution, rather than from round numbers, capture a materially different
share of the favourable move than the registered family does?

This is deliberately not "does it make money". A cohort whose every tested exit
loses is unlikely to become profitable by rescaling one, and promising that
would be the same mistake as the last three passes. What is being asked is
narrower and answerable: **is the family's scale the reason nothing in it
works, or is the scale irrelevant and the entry is the whole story?**

## Population

The exit-policy family's own cohort, unchanged: `ReplayFilters` defaults,
strategy `pump_short_v1_market_quality`, cohort start `2026-07-29`,
`allow_fallback` false. Using the family's registered cohort rather than
inventing one is what makes this comparable to HYP-021 at all.

## The variants, fixed here

Exactly three, all derived from the discovery window's own excursion
distribution rather than chosen by hand:

- **`scaled_p50`**: activation at the median favourable excursion, trail at half
  of it.
- **`scaled_p25`**: activation at the 25th percentile, trail at half.
- **`scaled_p75`**: activation at the 75th percentile, trail at half.

Three, not a sweep. The percentiles bracket the distribution rather than search
it, and "trail at half of activation" is one rule applied to all three rather
than a second free parameter. Any finer search is a different hypothesis with a
different id and an untouched window.

The percentiles are computed on the **discovery window only** and then applied
unchanged; they are not refitted per window.

## Windows

- **Discovery:** `2026-07-29` to `2026-08-25`, half-open.
- **Held out, not read in this pass:** `2026-08-25` onward.

If discovery shows no separation, the held-out window is not read.

## Primary metric, declared before reading

**Median net return per completed virtual trade**, per policy, using the shared
cost model, exactly as the family computes it for its existing six.

## Secondary, context only

Share of trades closed by each exit reason, median holding time, and captured
share of MFE. These describe how a policy got its number and may not be promoted
to the primary metric afterwards.

## Decision rule, declared before reading

- **Scale matters** if any scaled variant's median net return exceeds the
  baseline's by more than **1.0 percentage point** on at least 200 completed
  trades. That does not authorize changing production: it earns a read of the
  held-out window.
- **Scale is not the problem** if every scaled variant lands within ±0.5 points
  of the baseline. The family's failure is then not about where the thresholds
  sit, and the entry becomes the only remaining explanation worth pursuing.
- **Inconclusive** otherwise, or on fewer than 200 completed trades, or if the
  family's own readiness ladder withholds formal inference as it did for
  HYP-021.

  1.0 point rather than something smaller: the spread across the existing six
  policies is 0.26 points, so anything inside that band is indistinguishable from
  the variation the family already shows.

## What this pass may not do

It may not sweep percentiles, change the trail-to-activation ratio after seeing
a result, or reintroduce a variant that lost. It may not be read as evidence
about entry selection: every variant here shares the same entries, so nothing it
produces can speak to which candidates should have been traded.

It also may not be read as evidence about live results. The replay's agreement
with the paper broker is currently `inconclusive` at 94.4% on 72 trades, below
its own 80-trade floor, and no live position exists at all.
