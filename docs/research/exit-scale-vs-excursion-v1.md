# HYP-022 — The exit parameters are scaled past the moves they are meant to catch

**Status: registered 2026-09-08, before any variant's return was computed.
Result recorded and then CORRECTED TWICE the same day.**

> **Read the corrections before the result.** The first run read the window this
> contract declared held out, so its scope was the whole range and not the
> discovery window. And the result was reported in means while this contract's
> registered metric is the median. Both are corrected at the end of this
> document; the result section below is left as written, with its own errors
> struck in place, because rewriting it would hide what was claimed and when.

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

### Which excursion, decided before running anything

The horizon matters more than the percentile does, and picking it after seeing
returns would be the whole experiment. Fixed here: **the 60-minute horizon**,
because 60 minutes is the window in which trailing must activate or the
no-progress cut closes the position. An excursion the price reaches in hour six
cannot activate a trail that was required to start in hour one.

Measured on the discovery window, `pump_short_v1_market_quality`, complete
outcomes only (n = 31,166):

| Horizon |   p25 |       p50 |    p75 |
| ------: | ----: | --------: | -----: |
|  60 min | 1.65% | **3.99%** |  7.69% |
| 480 min | 5.66% |    11.08% | 17.49% |

The production activation threshold is 8%. Against the 60-minute distribution
that sits **above the 75th percentile**: three quarters of positions cannot
reach it before the cut fires, whatever the price does afterwards.

That is a stronger statement than the one this hypothesis was opened on. The
opening argument compared 8% against the family's mean realised MFE of 7.51%,
which is itself truncated by the exit under test and therefore partly circular.
The 60-minute outcome MFE is measured independently of any exit policy.

The three variants therefore are: **`scaled_p25`** activation 1.65%, trail
0.83%; **`scaled_p50`** activation 3.99%, trail 2.00%; **`scaled_p75`**
activation 7.69%, trail 3.85%. Trail is half of activation in all three, as
declared.

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

---

# Result, 2026-09-08

Run on production at `2831746`. Decision fingerprint `db70a8d30e7cd9c6`,
market-path fingerprint `0042b91c3907be46`.

## Verdict: `inconclusive`

Twice over, and neither reason is that the result looked bad.

The registered margin was **1.0 percentage point** over the baseline.
`scaled_p25` came in at **+0.64**. And the family again returned readiness
`insufficient_resolution` and withheld its formal intervals, which the contract
names as inconclusive on its own.

## What the run actually showed

| Policy                                 |   Mean net | Profit factor |  Win rate | Duration | Max drawdown |
| -------------------------------------- | ---------: | ------------: | --------: | -------: | -----------: |
| **scaled_p25** (act 1.65 / trail 0.83) | **+0.10%** |      **1.07** | **68.4%** |    19.7m |    85.82 USD |
| recent_progress_extension              |     -0.51% |          0.86 |     47.6% |   117.4m |   239.50 USD |
| baseline                               |     -0.53% |          0.85 |     47.6% |   114.5m |   248.10 USD |
| breakeven_after_activation             |     -0.57% |          0.83 |     49.5% |   110.9m |   257.24 USD |
| scaled_p50 (act 3.99 / trail 2.00)     |     -0.58% |          0.76 |     55.6% |    32.6m |   272.26 USD |
| scaled_p75 (act 7.69 / trail 3.85)     |     -0.64% |          0.77 |     50.5% |    44.3m |   280.45 USD |
| no_progress_60m                        |     -0.76% |          0.76 |     42.5% |    80.2m |   335.99 USD |
| production                             |     -0.77% |          0.74 |     45.2% |    63.1m |   341.08 USD |
| breakeven_no_progress_60m              |     -0.77% |          0.75 |     44.5% |    77.0m |   336.94 USD |

851 resolved episodes, paired.

**`scaled_p25` is the first policy this repository has measured above a profit
factor of 1.** It is also the tightest scale tested, and the three scaled
variants are monotone in the direction of tightness: +0.10, -0.58, -0.64 as
activation rises from 1.65% to 3.99% to 7.69%.

~~Every one of the six round-number policies sits below all three on
drawdown.~~ **False, contradicted by the table directly above it: baseline
248.10 against scaled_p50's 272.26 and scaled_p75's 280.45. Only `scaled_p25`
beats the baseline on drawdown.** The monotonicity claim in the same paragraph
is also an artifact of the wrong metric; see correction 2.

Its exit reasons are a different regime rather than the same one tuned:
`trailing_stop` fires on **649 of 851** episodes against 227 for the baseline,
`initial_sl` falls from 273 to 101, and mean holding time drops from 114 minutes
to 20.

## Why this is not promoted

**+0.10% mean net is not a business.** Taken entirely at face value, on a
population that pays no spread and has no order book behind it, it is
indistinguishable from zero for any practical purpose. What it establishes is a
direction, not an edge.

**0.64 is inside the margin that was set precisely to catch this.** The margin
was 1.0 point against the 0.26-point spread the six round-number policies
already show among themselves. 0.64 is larger than that spread but smaller than
the bar, and the bar was written down before the number existed. Moving it now
because the result is interesting is the whole failure mode the contract exists
to prevent.

**The family withheld formal inference again.** Whatever the point estimate
says, the machinery that would put an interval around it declined to.

## What may not be done next, and is tempting

`scaled_p25` is the tightest variant tested and the relationship is monotone in
tightness. The obvious move is to try something tighter still. **That is a
sweep, this contract forbids it, and it would be fitting to the window that
produced the ordering.** Anything tighter is a new hypothesis with a new id and
an untouched window.

The result also may not be read as evidence about the entry. Every variant here
shares the same entries; what changed is only what happens after.

## What this does change

The claim that "the exit is not the lever" is now clearly wrong as stated, and
so is the softer version a colleague and I settled on. Within the round-number
family the exit was not a lever, because the family never varied the scale. Vary
it and the whole distribution moves: win rate from 47.6% to 68.4%, drawdown
from 248 to 86, holding time from 114 minutes to 20.

That does not make the strategy profitable. It does mean the exit is worth
another registered pass on untouched data, which is more than could be said this
morning.

---

# Correction, 2026-09-08: the held-out window was read

A colleague asked where the artifact for fingerprint `db70a8d30e7cd9c6` was and
what window it actually covered. It covered the wrong one.

## What happened

The run was `make prod-virtual-exit-policy-report`, whose `--until` defaults to
the run's own start time. Its scope line says so plainly:

```
Scope: 2026-07-29T00:00:00+00:00 <= decision < 2026-09-08T14:20:04.791299+00:00
```

This contract declared discovery as `2026-07-29` to `2026-08-25` and held out
everything after. The run read **both**. `--until` exists on that report; it was
simply not passed. The error was avoidable and nobody would have found it from
the numbers alone -- only from the scope line, which is why it was asked for.

## The number the contract actually asked for

Re-run restricted to the discovery window, fingerprint `94eba938a5678f9b`,
scope `2026-07-29 <= decision < 2026-08-25`, 556 resolved episodes:

| Policy                     |   Mean net | Profit factor |  Win rate | Max drawdown |
| -------------------------- | ---------: | ------------: | --------: | -----------: |
| **scaled_p25**             | **+0.27%** |      **1.19** | **70.7%** |    56.47 USD |
| recent_progress_extension  |     -0.25% |          0.93 |     48.0% |    87.25 USD |
| baseline                   |     -0.29% |          0.91 |     48.0% |   100.86 USD |
| scaled_p75                 |     -0.32% |          0.88 |     50.4% |   132.92 USD |
| breakeven_after_activation |     -0.34% |          0.89 |     49.6% |   109.57 USD |
| scaled_p50                 |     -0.36% |          0.84 |     55.0% |   146.84 USD |
| no_progress_60m            |     -0.41% |          0.86 |     43.2% |   131.03 USD |
| breakeven_no_progress_60m  |     -0.44% |          0.84 |     45.1% |   137.10 USD |
| production                 |     -0.45% |          0.83 |     45.9% |   150.06 USD |

Paired delta for `scaled_p25` against baseline: **+0.55 points**, against the
registered margin of 1.0.

**The verdict is unchanged: `inconclusive`.** That is luck, not diligence. Had
the contaminated number crossed 1.0 while the correct one did not, this
correction would have retracted a conclusion rather than a scope line.

## What the contamination does and does not cost

The variant parameters were derived from the discovery window only: the
percentile query was bounded at `2026-08-25`.

~~So the 295 episodes after that date were never used to choose anything.
Nothing was fitted to them.~~ **Too strong, and withdrawn.** The numbers were
not fitted to those episodes, but `scaled_p25` was picked as the variant worth
confirming _after_ seeing all three variants scored on the full range. Choosing
a winner is using the data, whatever the parameters were derived from. That is
the substantive reason the confirmation is withdrawn, and it would hold even if
the window boundary had been honoured for everything else.

What is gone is their value as an unread confirmation. They have been seen, and
a window cannot be un-seen. Any comparison against them from here carries the
knowledge that the result was already visible when the comparison was designed.

## Consequences

**HYP-026 is withdrawn as registered.** Its premise was that `2026-08-25` onward
was untouched. It was not.

The only genuinely unread window in this line of work is data that does not
exist yet. A replication has to be registered against a future date and waited
for, which is slower and is the actual cost of this mistake.

## Artifacts

Both runs are preserved on the production host under
`/opt/schurfer/backups/reports/hyp022/`, and are therefore inside the research
archive family:

| File                            | Fingerprint        | Scope                             |
| ------------------------------- | ------------------ | --------------------------------- |
| `hyp022-full-range-db70a8d3.md` | `db70a8d30e7cd9c6` | 2026-07-29 to 2026-09-08T14:20:04 |
| `hyp022-discovery-94eba938.md`  | `94eba938a5678f9b` | 2026-07-29 to 2026-08-25          |

Command in both cases: `make prod-virtual-exit-policy-report`, the second with
`ARGS="--until 2026-08-25"`, at code revision `2831746`.

---

# Correction 2, 2026-09-08: the registered metric is the median, and it was not used

A colleague read the contract against the result and found that the two do not
measure the same thing. The contract's primary metric is **median** net return
per completed virtual trade, and its decision rule is a median exceeding the
baseline's by more than 1.0 point. Everything reported above is **mean** net
return, and `+0.64` is a difference of means compared against a margin written
for medians.

## The registered metric, computed from the saved discovery run

From `hyp022-discovery-94eba938.md`, 556 episodes completed under every policy:

| Policy         |  Median net | Difference of medians vs baseline | Mean net |
| -------------- | ----------: | --------------------------------: | -------: |
| baseline       |     -0.315% |                                -- |   -0.29% |
| production     |     -0.315% |                            +0.000 |   -0.45% |
| **scaled_p25** | **+0.950%** |                        **+1.265** |   +0.27% |
| **scaled_p50** | **+0.965%** |                        **+1.280** |   -0.36% |
| scaled_p75     |     +0.105% |                            +0.420 |   -0.32% |

**On the registered metric two variants clear the 1.0 margin, and the correct
number for `scaled_p25` is +1.27 rather than +0.64.** The error understated the
effect rather than inflating it, which is luck again and not a defence.

## Three things this changes

**The verdict's reason.** `inconclusive` still stands, but not because the
margin was missed. It stands on the other registered condition: the family
returned `insufficient_resolution` and withheld formal inference. That is now
the only thing between this and a candidate.

**The monotonicity claim is false on the registered metric.** On means the three
variants were monotone in tightness. On medians they are not: `scaled_p50`
(+1.280) edges out `scaled_p25` (+1.265), and `scaled_p75` is far behind at
+0.420. The tidy story about tightness was an artifact of the metric I was not
supposed to be using.

**The median paired delta is zero.** Across 556 episodes the per-episode
difference is negative on 237, zero on 61 and positive on 258, so the middle of
that distribution sits inside the zero block. **The typical episode is not
improved at all.** What moves is the shape of the tails: fewer deep losses, and
the mean and median both shift because of what happens away from the centre.

That last point is the most useful thing in this correction and would not have
surfaced without it. A policy that helps the median episode and one that only
truncates the left tail are different propositions, and only the second is
supported here.

## The drawdown claim was also wrong

The result above states that all three scaled variants beat all six
round-number policies on maximum drawdown. The tables say otherwise, on both
runs. On the discovery window: baseline 100.86 USD, `scaled_p25` 56.47,
`scaled_p50` 146.84, `scaled_p75` 132.92. Only `scaled_p25` beats the baseline.

Corrected: **`scaled_p25` has the lowest maximum drawdown of any policy in the
family. The other two scaled variants do not.**
