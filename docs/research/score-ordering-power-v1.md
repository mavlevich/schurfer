# HYP-019 — Does the score rank outcomes at all?

**Status: pre-registered. No outcome value from this window has been read at the time of
writing.**

Registered 2026-09-07, after HYP-018 and before any query against the window below.

## Why this and not the HYP-018 confirmation

HYP-018 ended `candidate` on the threshold-6 window, and its contract earns a read of the
held-out threshold-5 window. This hypothesis deliberately spends that window on a
different, more fundamental question instead, and the HYP-018 confirmation is **not**
pursued.

The reason: HYP-018's own result showed the metric is not monotonic in the score --
level 4, the largest bucket, returned less than levels 3 and 5. A threshold is only
meaningful on top of an ordering. If the score does not rank outcomes, then where the
threshold sits is the wrong question, and confirming a threshold candidate would be
refining an answer to it.

One window, one question. Spending it here is a choice, recorded as such.

## Question

Does a higher score correspond to a better short outcome?

## Window

`2026-08-18` through `2026-09-07`, half-open: the `threshold = 5` regime. Untouched by
HYP-018, which read only the threshold-6 regime.

## Population

`app.trade_decisions` rows in the window with a non-null `score`, joined to
`app.trade_decision_outcomes` at `horizon_minutes = 240` with `status = 'complete'`.
Both skipped and opened decisions are included, since the question is about the score's
ordering across its whole range and restricting to one side would truncate it.

## Primary metric, declared before reading

**Spearman rank correlation between `score` and net short return at 240 minutes**, with a
95% interval from a bootstrap resampled **over base symbols, not over decisions**.

Clustering is not optional here. A handful of tokens contribute thousands of decisions
each -- CZ alone has 2,210 skipped-by-score rows -- so an interval computed over
independent decisions would be meaningless pseudo-replication.

Net uses `schurfer_performance.accounting.DEFAULT_COSTS` as-is, the same as HYP-018.

## Decision rule, declared before reading

- **Rejected as an ordering device** if the absolute correlation is below 0.05, or if the
  cluster interval contains zero.
- **Confirmed** if the correlation is at least 0.10 in the expected direction with the
  cluster interval excluding zero.
- **Inconclusive** otherwise.

A negative correlation excluding zero would be a finding in its own right, not a failure:
it would mean the score is ranking candidates backwards.

## Secondary, context only

Median net return per score level, and the number of distinct base symbols per level.
These describe the shape; they do not decide the verdict.

## What this pass may not do

It may not adjust the score formula, propose weights, or re-run with a different horizon
after seeing the result. Those need a new hypothesis and an untouched window. It also
inherits HYP-018's limitation: price paths with no order book, so nothing here is an
executable claim.
