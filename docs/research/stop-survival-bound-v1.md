# HYP-020 — Does the low-score band survive its own stop? A bound.

**Status: pre-registered as an exploratory bound. No stop-aware figure has been computed
at the time of writing.**

## Why this is a bound and not an answer

`app.trade_decision_outcomes` stores `mfe_pct` and `mae_pct` as extremes over the horizon
with **no timestamps**. There is no `mfe_at` or `mae_at`, so for a position that touched
both a favorable target and the stop level, the table cannot say which came first.

A stop-aware result therefore cannot be computed from this table. What can be computed is
the **pessimistic bound**: assume every position whose adverse excursion ever reached the
stop was stopped there, regardless of what the price did before.

That bound is one-sided and useful in exactly one direction:

- If the low-score band is **still positive** under it, the finding survives the harshest
  stop assumption available, and a prospective cohort is worth registering.
- If it is **negative**, the pass is inconclusive, because the true value lies somewhere
  between this bound and hold-to-horizon. It would not refute anything.

## Window and its status

The same `threshold = 5` window HYP-019 read, `2026-08-18` through `2026-09-07`.

**This window has already been viewed.** This pass is therefore exploratory by
construction and cannot confirm anything. Its only job is to decide whether registering a
forward cohort is worth the wait. Recorded plainly rather than dressed as a second
independent read.

## Method, declared before computing

Per score level, over complete 240-minute outcomes:

- if `mae_pct >= 10` (the live `initial_sl_pct` default), the position contributes
  `-10 - 0.225`, a stop-out plus the same round-trip cost model HYP-018 and HYP-019 used;
- otherwise it contributes `short_return_pct - 0.225`.

Reported as the median and the stop-out rate per level. Unlevered, so both sides scale
together.

## Decision rule, declared before computing

- **Register a forward cohort** if the median bounded return is above zero for at least
  one score level below the current threshold, on at least 100 complete outcomes and 30
  distinct base symbols.
- **Do not register** otherwise, and record that the score-ordering finding, while real,
  does not survive the stop the strategy actually uses.

## What this may not do

It may not tune the stop level to find one that works: that is fitting on a viewed
window. A single stop value, the production default, is used.
