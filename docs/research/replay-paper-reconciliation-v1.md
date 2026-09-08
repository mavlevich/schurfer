# Replay against paper: does the simulator reproduce what actually happened

**Status: contract registered 2026-09-08, before any agreement rate was computed.**

This is a correctness check, not a hypothesis about edge. It asks whether the
offline replay reproduces the decisions the paper broker actually made, and the
pass criterion is written down first so the answer cannot be graded after the
fact.

## Why now

Every research conclusion in this repository about exit policy comes from the
replay. The replay has never been compared against anything that actually ran.
HYP-021 compared six simulated policies to each other and reported that all six
lose money; that statement is only worth as much as the simulator behind it.

This became possible after `refactor(exit)` (#361) made the exit decision a
single shared function, and after the paper broker's own exit reason turned out
to be recorded in `app.trades.notes`.

## What is being compared

For each closed paper `pump_short` trade since 2026-08-18, the date production
gained the no-progress exit:

- **recorded**: `notes` (the reason string produced by `evaluate_exit`),
  `exit_at`, `exit_price`;
- **replayed**: the same episode simulated with `PRODUCTION_EXIT_POLICY`, entered
  at the trade's own recorded entry time and price.

The entry is forced to match. Without that, the two runs are different trades:
the paper broker waits for a retrace before entering, while the replay enters at
the next complete five-minute bar, and every stop level is relative to entry.
Forcing the entry isolates the exit rule, which is the thing under test.

## What cannot match, and is not counted as disagreement

The paper broker evaluates on ticker updates, roughly once a minute, at prices
that never appear in a five-minute bar. The replay sees four numbers per bar. So
exact times and prices cannot agree, and the comparison is:

- **primary:** does the exit _reason_ match;
- **secondary, reported not graded:** the distribution of exit-time differences
  and exit-price differences, for the trades whose reason matched.

LBank trades cannot be replayed at all: ccxt's LBank `fetchOHLCV` targets the
spot kline endpoint, so a perpetual-only market has no candles (CCXT-003, and
ENG-032 for the measurement). They are reported as coverage, not as
disagreement.

## Pass criterion, declared before measuring

- **Agrees** if the exit reason matches on at least **85%** of reconcilable
  trades, with at least 80 of them. The simulator is then usable for the
  questions it has been used for, with its bar-resolution limits stated.
- **Disagrees** if the match rate is below **70%**. Every replay-derived
  conclusion about exit policy, HYP-021 included, is then provisional and must
  say so.
- **Inconclusive** between those, or on fewer than 80 reconcilable trades.

85% rather than a higher bar because of the resolution gap above: a trade whose
price crossed a stop between bar samples will legitimately exit for a different
reason in the two runs, and that is a property of bar replay rather than a
defect. A rate near 100% would be more suspicious than reassuring.

## What this pass may not do

It may not tune the replay to improve the match after seeing the result. It may
not reclassify a disagreement as acceptable once its cause is known. If the
cause is a defect, the defect is fixed and the comparison is re-run under this
same contract; if the cause is bar resolution, that is a limitation to state,
not a reason to move the threshold.

It also says nothing about live execution. There is no live execution: every one
of the 936 trades in the database is `paper: true` with no exchange order id. A
successful reconciliation establishes that the replay agrees with the paper
broker, and nothing about slippage, fills, or depth.
