# HYP-018 — What the score threshold costs

**Status: read on 2026-09-07. Verdict `candidate`, with a caveat that matters more than the verdict. The contract above was committed (`1ae4945`) before any outcome value was queried.**

Registered 2026-09-07, before any return, MFE or MAE for the population below was
queried. Only row counts and outcome-status counts were inspected, to establish that the
sample exists and is large enough.

## Question

The scanner rejects candidates whose score is below `SCORE_THRESHOLD`. Production data
shows that rejection applying to very large moves: `score 3 < threshold 5` appears with a
recorded pump of up to 602%, and `score 4 < threshold 5` up to 480%.

That is not evidence of a mistake. A large move is not the same as a profitable short,
and CZ's own episodes retrace hard, which is what the strategy expects. The question is
whether the threshold rejects decisions whose short outcome would have been **net
positive after costs**, and if so by how much.

## Population

`app.trade_decisions` rows with `action = 'skipped'` and `reason LIKE 'score % < threshold %'`,
joined to `app.trade_decision_outcomes` on `decision_id`.

## Windows

- **Discovery (this pass):** the `threshold = 6` regime, `2026-07-17` through
  `2026-08-18`, half-open. About 39,500 decisions across the score levels.
- **Held out, not read in this pass:** the `threshold = 5` regime, `2026-08-18` through
  `2026-09-07`. The threshold changed on 2026-08-18, so the two regimes are a natural
  experiment rather than an arbitrary split.

If the discovery window shows nothing, the held-out window is not read at all.

## Inclusion and exclusion

- Only outcomes with `status = 'complete'` enter the metric.
- `complete_fallback_unsupported`, `market_path_unavailable`, `fetch_failed`,
  `missing_ohlcv`, `missing_price` and `partial` are **counted and reported as coverage**
  and never merged into the metric's denominator. Mixing proxy paths with exact native
  evidence is what AI_RULES forbids, and the September audit found this class of error
  elsewhere.
- Coverage is reported per score level, because a coverage difference between levels
  would itself explain a difference in outcome.

## Primary metric, declared before reading

**Median net short return at the 240-minute horizon**, per score level.

Net means gross `short_return_pct` minus the shared cost model in
`schurfer_performance.accounting.DEFAULT_COSTS`: 10 bps taker fee per side (20 bps round
trip) and 5 bps funding per 8 hours, prorated over the horizon. That model is used as-is,
not re-derived here.

240 minutes is the longest horizon the outcome resolver stores. Choosing it is declared
here rather than after seeing which horizon looks best.

## Secondary, context only

Median MFE and MAE at the same horizon, and the same statistics at 15, 30 and 60 minutes.
These do not decide the verdict and may not be promoted to the primary metric after the
fact.

## Variants

One. No sweep over thresholds, horizons or score levels beyond reporting each level
separately, so there is no multiple-comparison correction to apply and no room to pick a
winner after the fact.

## Decision rule, declared before reading

- **Rejected** if the median net return at 240 minutes is at or below zero for every
  score level. The threshold is then not leaving money on the table, and the question is
  closed for this window.
- **Candidate** only if some score level shows a median net return above **+0.5%**, which
  is roughly two round trips of cost, with at least 100 complete outcomes at that level.
  A candidate does not change any threshold: it earns a read of the held-out window under
  its own registered rules.
- **Inconclusive** otherwise, including any level that clears the margin on fewer than
  100 complete outcomes.

## What this pass may not do

It may not tune the threshold, propose a new score formula, or re-run with a different
horizon or cost assumption after seeing the result. Any of those is a new hypothesis with
a new id and an untouched window.

It also may not be read as an estimate of profit forgone. These are price-path outcomes
with no order book behind them: no spread, no depth, no fill. A positive median here
means "worth investigating with execution evidence", not "money we lost".

---

# Result, 2026-09-07

## Coverage, discovery window

| Score level | complete | other status | % complete |
| ----------- | -------: | -----------: | ---------: |
| 0           |       53 |           33 |       61.6 |
| 1           |      800 |          331 |       70.7 |
| 2           |    3,681 |        1,212 |       75.2 |
| 3           |   10,985 |        2,743 |       80.0 |
| 4           |   11,545 |        4,110 |       73.7 |
| 5           |    2,983 |          698 |       81.0 |

Coverage ranges 62% to 81%, so between-level comparisons carry that difference and are
not clean.

## Primary metric: median net short return at 240 minutes

| Score level |      n | gross % | **net %** | median MFE | median MAE |
| ----------- | -----: | ------: | --------: | ---------: | ---------: |
| 0           |     53 |   3.299 |     3.074 |      11.53 |       6.14 |
| 1           |    800 |   3.492 | **3.267** |      12.95 |      10.47 |
| 2           |  3,681 |   1.373 | **1.148** |       9.69 |       8.92 |
| 3           | 10,985 |   1.228 | **1.003** |       8.13 |       7.39 |
| 4           | 11,545 |   0.581 |     0.356 |       8.24 |       7.28 |
| 5           |  2,983 |   1.094 | **0.869** |       8.27 |       6.36 |

By the pre-declared rule this is `candidate`: levels 1, 2, 3 and 5 clear the +0.5% margin
on far more than 100 complete outcomes. Level 4 does not clear it and level 0 is below
the evidence floor.

## The caveat that outweighs the verdict

`short_return_pct` at 240 minutes is **enter and hold for four hours with no stop**. The
strategy does not trade that way: it places a protective stop at `initial_sl_pct`,
default 10%.

| Score level | % whose adverse excursion reached 10% | median MAE |
| ----------- | ------------------------------------: | ---------: |
| 0           |                                  39.6 |       6.14 |
| 1           |                              **51.4** |      10.47 |
| 2           |                                  46.1 |       8.92 |
| 3           |                                  39.0 |       7.39 |
| 4           |                                  38.6 |       7.28 |
| 5           |                                  31.6 |       6.36 |

Between 32% and 51% of these positions would have been stopped out before the horizon.
Level 1, which shows the best median net return, is also the one that would have been
stopped most often: its median adverse excursion alone is 10.47%, past the stop.

So the positive medians are not a claim that these trades were available. They are a
claim that the price path, held without a stop, ended positive at the median.

## Also worth recording

The metric is **not monotonic in the score**. Level 4, the largest bucket, has the worst
net return of any level above the floor, below both level 3 and level 5. Whatever the
score is ordering in this window, it is not four-hour short outcome.

## What happens next, per the contract

A candidate earns a read of the held-out `threshold = 5` window under its own registered
rules. It changes no threshold, and this pass may not be re-run with a different horizon,
cost model or stop assumption to obtain a better-looking answer.

The obvious next question -- what these look like under the actual exit bracket rather
than hold-to-horizon -- is a **different** hypothesis with its own id and its own
untouched window, because the exit policy is a strategy parameter and tuning it against
this already-viewed window would be exactly the fitting this ledger exists to prevent.
