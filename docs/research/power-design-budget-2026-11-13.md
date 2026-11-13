# Research power: changes that would alter the budget

Status: methodology planning, 2026-11-13. The owner asked whether a different
approach could provide adequate power. This note uses existing HYP-037 results
only. It opens no strategy, changes no threshold and reads no market data.

## What the HYP-037 stop means

The registered conservative proxy rule gave MDE 123.03 bps for A and 73.23 bps
for B, against a 50 bps planning limit. These are conditional planning estimates,
not measurements of a negative strategy return. A passes under four individual
proxies, but not under the registered maximum rule; that rule stays unchanged.

For an otherwise unchanged design, MDE scales approximately as residual standard
deviation divided by the square root of independent information. It does not
scale with the number of highly correlated rows alone.

| Planning calculation from the sealed estimates                            |      A |     B |
| ------------------------------------------------------------------------- | -----: | ----: |
| Existing MDE, bps                                                         | 123.03 | 73.23 |
| Independent-information multiplier for 50 bps: (MDE/50)^2                 |   6.05 |  2.15 |
| Residual standard-deviation ratio needed at unchanged information: 50/MDE |  0.406 | 0.683 |
| Corresponding variance reduction                                          |  83.5% | 53.4% |

These ratios assume unchanged effect, dependence, event frequency and cost model.
They are not a forecast that six calendar years or a BTC hedge will solve A.

## Changes worth evaluating before another strategy registration

1. **Reduce a real source of variance.** A predeclared tradable market hedge could
   reduce common-market exposure. Estimate hedge parameters using pre-decision
   data only. Include both legs' fees, funding, spread, sizing constraints and
   capital needs. Residualizing returns statistically is not the economics of an
   executed hedge. A hedge changes the strategy and needs a new design decision.
2. **Add independent regimes or time.** Additional same-hour altcoin signals may
   add little information. A longer clean history can help only with known
   point-in-time coverage and a reason to transfer the effect across regimes.
   Reserved windows are not released by this note.
3. **Match the estimand to the money question.** Decide before outcomes whether
   the target is an event mean or a capital-constrained daily portfolio. Keep the
   full signal denominator. Daily aggregation reduces row inflation; it does not
   automatically provide power or turn 51 weeks into thousands of observations.
4. **Audit the inference method on synthetic panels.** Before adopting a less
   conservative dependence model, verify false-positive control, confidence
   coverage and power with synchronized events and serial market shocks. Fix the
   simulation grid and acceptance rules before running it. Never choose the
   method because it makes an already stopped strategy pass.

## What does not solve this stop

Faster computation, more bootstrap draws, relabelling overlapping events as
independent, screening more features, or choosing the most favorable proxy do
not create independent information. A shorter hold, volatility targeting,
different exit or stronger filter may change variance and expected return
together; each is a new economic design, not a free power correction.

HYP-037 A/B remain closed. After the current B2 checkpoint, a separate methodology
audit may be proposed under a fixed work budget. No outcome read, new funding/OI
variant, ML model or independent-window expenditure follows from this note.
