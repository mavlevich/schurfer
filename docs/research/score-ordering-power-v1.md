# HYP-019 — Does the score rank outcomes at all?

**Status: read on 2026-09-07. The score ranks candidates BACKWARDS. The contract below was committed (`bb51127`) before any query against this window.**

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

---

# Result, 2026-09-07

158,169 decisions across 439 base symbols, `threshold = 5` window, complete 240-minute
outcomes only.

## Primary metric

**Spearman rank correlation between score and net short return: −0.0997**
**Cluster bootstrap 95% CI over 439 bases: [−0.1398, −0.0609]**

The interval excludes zero and sits entirely on the negative side. Per the pre-declared
rule this is not "rejected as an ordering device": the score _does_ order outcomes, in
the **opposite** direction to its intent. The contract named this case in advance as a
finding rather than a failure.

## The shape

| score |      n | bases | median net % |
| ----: | -----: | ----: | -----------: |
|     1 |  7,532 |   176 |   **+2.669** |
|     2 | 30,614 |   360 |   **+1.543** |
|     3 | 58,394 |   399 |   **+0.584** |
|     4 | 40,702 |   304 |       −0.038 |
|     5 | 15,090 |   211 |   **−0.442** |
|     6 |  4,564 |    83 |   **−0.805** |
|     7 |  1,176 |    20 |       +1.031 |
|     8 |     97 |     4 |       −3.197 |

Monotonically decreasing from 1 to 6, on hundreds of distinct symbols per level. Scores 7
and 8 rest on 20 and 4 symbols respectively and are noise at that width.

## What this means for the gate

`SCORE_THRESHOLD = 5` admits scores 5 and above and rejects everything below. In this
window that is precisely inverted: the admitted band has a **negative** median net return
(−0.44 at 5, −0.81 at 6) and the rejected band a **positive** one (+2.67, +1.54, +0.58 at
1, 2, 3).

This is consistent with HYP-018 on the _other_ window, which found the same
non-monotonicity and the best returns at the lowest score level. Two windows, two
regimes, same direction.

## What it does not establish

- **Hold-to-horizon, no stop.** HYP-018 showed 32% to 51% of such positions reach a 10%
  adverse excursion before 240 minutes, and the live stop is 10%. Whether the low-score
  band survives its own stop better than the high-score band is a separate question this
  pass did not ask.
- **No order book.** Price paths only: no spread, no depth, no fill.
- **Mechanism unknown.** A plausible story is that a high score marks a stronger, still-
  running move, which is bad for an immediate short. That is a hypothesis, not a result.
- **Correlation is modest.** −0.10 is a real but weak ordering; the per-level medians are
  the more legible statement.

## What this authorizes

Nothing automatic. It does not license inverting the score, moving the threshold, or
trading the low band. Each of those is a strategy change needing its own registered
hypothesis and an untouched window.

What it does do is redirect the question. Until now the open item was where to put the
threshold. The finding says the ordering underneath it is pointed the wrong way, so
tuning the threshold on the current score would be optimising the wrong dial.
