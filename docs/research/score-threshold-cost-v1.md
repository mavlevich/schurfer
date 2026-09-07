# HYP-018 — What the score threshold costs

**Status: pre-registered. No outcome value has been read at the time of writing.**

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
