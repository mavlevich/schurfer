# HYP-025 — Is the largest table in the database worth what it costs to collect

**Status: registered 2026-09-08, before any relationship to outcome was
computed.**

> **Amended 2026-09-08, before any outcome was joined.** The family rules in
> [entry-signal-family-rules-v1.md](entry-signal-family-rules-v1.md) bind this
> contract and override anything below that contradicts them: the unit of
> observation is the episode and not the decision, the floors count episodes and
> asset clusters, a negative verdict needs as much data as a positive one, no
> outcome may straddle a window boundary, and a feature counts only if it was
> available when the decision was made.
>
> **Amended again 2026-09-09, still before any outcome was joined.** Rule 6 was
> added after HYP-023's `pump_age` candidate was withdrawn: a quintile boundary
> falling inside a tied value splits equal measurements by sort order, so a
> candidate now requires every adjacent pair of compared buckets to differ in the
> feature, and every report states the distinct-value count and the largest tied
> group. This binds here too, and it matters most for any feature recorded at
> coarse resolution.

## Family declaration

One of three hypotheses registered together on 2026-09-08 (HYP-023, HYP-024,
HYP-025), all searching the same recorded data for entry signal. Three
independent searches produce a "finding" at a 5% threshold about 14% of the time
by chance. A positive result here is weaker than the same result from a single
registered search, and promotes nothing without the held-out window.

## What is already established

`app.pump_derivatives_context_samples` is **3,386 MB**, the largest table in the
application schema, larger than `app.trade_decisions` and
`app.trade_decision_outcomes` combined.

It is read by two reports, `long_short_ratio_regime_report` and
`derivatives_regime_feasibility`, and by nothing in the decision path. The
scanner has never consulted it.

## Question

Two, and the second one only if the first fails.

1. Does any recorded derivatives-context field separate episodes by their forward
   60-minute excursion?
2. If not, is there a reason to keep collecting it at this volume?

The second question is unusual for a registered hypothesis and is included on
purpose. A dataset that costs the largest share of the database and feeds no
decision is either an untapped asset or an ongoing cost, and "we might need it
later" has been the answer for over a month without anyone testing it.

## Population

Decisions for `pump_short_v1_market_quality` with a derivatives-context sample
within the ten minutes before the decision, joined to outcomes at the 60-minute
horizon, complete only.

The field inventory is established first, from the table itself, and recorded in
the result before any outcome is joined. This hypothesis does not name the fields
in advance because they are not documented anywhere; enumerating them is part of
the work and is done without looking at outcomes.

## Windows

- **Discovery:** `2026-07-29` to `2026-08-25`, half-open.
- **Held out, not read in this pass:** `2026-08-25` onward.

## Primary metric, declared before reading

For each field independently: **the spread in median net short return between its
top and bottom quintile** at the 60-minute horizon, net of the shared cost model.

The number of fields examined is reported alongside the result, because a
best-of-N is a search and reporting the winner without N would misrepresent it.

## Decision rule, declared before reading

- **Unavailable** before anything else: a field whose availability at decision
  time cannot be established under family rule 5 is excluded from the metric
  and reported separately as a research approximation. It is neither a
  candidate nor evidence of absence. This is the rule that decides this
  hypothesis: the table holds historically reconstructed values, and
  `source_at` preceding a decision does not mean the value existed then.
- **Insufficient** next: below **150 episodes** per compared quintile or **30
  asset clusters** across them, `inconclusive` in both directions.
- **Candidate** if a field's top-to-bottom quintile spread exceeds **2.0
  percentage points**, monotone across all five quintiles. Higher than the other
  two hypotheses' 1.5 because this one searches an unknown number of fields
  rather than a fixed six or a single statistic.
- **Not worth its cost** if no _available_ field clears 0.5 points, and only
  where those fields cleared the sufficiency floor. That is not a recommendation
  to drop the table -- retention and collection are an owner's decision -- but it
  is the evidence that would inform one.
- **Inconclusive** otherwise.

## What this pass may not do

It may not construct a composite from the fields, may not change the lookback,
and may not report the best field without the count of fields tried. It may not
be read as a conclusion about the two existing reports that do use this table,
which measure different things.
