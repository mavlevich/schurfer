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
