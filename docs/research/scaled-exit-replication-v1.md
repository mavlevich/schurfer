# HYP-026 — Does the tight exit replicate on data it was not derived from

**Status: registered 2026-09-08, before the held-out window was read.**

## Why this exists

HYP-022 found `scaled_p25` (activation 1.65%, trail 0.83%) at profit factor
1.07, win rate 68.4%, mean net +0.10%, maximum drawdown 86 USD against 248 for
the baseline. First policy this repository has measured above a profit factor of

1.

It was `inconclusive` under its own rule: the margin was 1.0 percentage point
and the result was +0.64, and the family withheld formal inference. It therefore
did not earn a read of its held-out window, and this is not that read smuggled
in under a new name.

This is a different question. HYP-022 asked whether scale matters, comparing
three variants derived from the discovery window's own excursion percentiles.
This asks the only question that decides whether any of it is real:

**Does `scaled_p25` behave the same way on data its parameters were not derived
from?**

## Why not simply try something tighter

Because the arithmetic says the monotone trend cannot continue far. A round trip
costs 20 bps of taker fee plus funding; against a 0.83% trail that is already a
quarter of the move being captured. Tightening further runs into a cost floor
rather than into more edge, and testing the shape of that floor is a sweep on a
window that has already produced one ordering.

Replication is cheaper and answers the prior question. If the effect does not
survive untouched data, the shape of the optimum does not matter.

## Population and window

The same cohort and machinery: `pump_short_v1_market_quality`, the replay's own
eligibility, `allow_fallback` false.

**Window: `2026-08-25` onward, half-open, ending at the run.** This is the
window HYP-022 declared held out and did not read. It is read exactly once,
here.

The parameters are **not** re-derived. Activation stays 1.65% and trail 0.83%,
the values computed from the discovery window's 60-minute excursion percentiles.
Recomputing them on this window would make it a second discovery pass wearing a
confirmation's clothes.

## Primary metric, declared before reading

**Mean net return per completed virtual trade for `scaled_p25` minus the same
for `baseline`**, paired per episode, using the shared cost model.

One comparison, two policies. The other seven registered policies are not run
here: including them would turn a confirmation into a best-of-nine.

## Secondary, context only

Profit factor, win rate, holding time, maximum drawdown and exit-reason
distribution for both. These describe the regime and do not decide anything.

## Decision rule, declared before reading

- **Replicates** if the paired mean delta is **at least +0.30 percentage
  points** on at least **150 completed trades**. That is roughly half the
  discovery window's +0.64: an effect that is real but smaller out of sample is
  the normal shape of a real effect, and demanding the full magnitude would
  reject almost anything genuine.
- **Does not replicate** if the delta is **at or below zero**. The discovery
  result was then a property of the window that produced it, and the tight-exit
  direction is closed until something new justifies reopening it.
- **Inconclusive** between those, or on fewer than 150 completed trades.

## What this pass may not do

It may not adjust the parameters, add a variant, extend the window, or re-run
after seeing the result. Any of those turns the last untouched window in this
line of work into another discovery window, and there is not a second one.

A replication is also not a licence to trade. Mean net +0.10% on the discovery
window is indistinguishable from zero for any practical purpose, the population
pays no spread and has no order book behind it, and the replay's own agreement
with the paper broker is still `inconclusive` at 94.4% on 72 trades against its
80-trade floor.
