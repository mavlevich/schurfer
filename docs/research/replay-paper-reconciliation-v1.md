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

---

# Result, 2026-09-08

Run on production at `b17b386`, policy `production_no_progress_v2`.

## Verdict: `inconclusive`

Not because the agreement looked bad. **The reasons matched on 68 of 72
reconcilable trades, 94.4%**, comfortably above the 85% the contract calls
agreement.

The evidence floor is what binds: the contract required at least **80**
reconcilable trades and the run produced **72**. That floor was written down
before any rate was computed, precisely so a good-looking number on a thin
sample could not be promoted afterwards. It is not promoted here.

## Coverage

| Status                        | Trades |
| ----------------------------- | -----: |
| compared                      |     72 |
| unreplayable_exchange (LBank) |     33 |
| episode_unavailable           |     32 |
| market_path_unavailable       |      2 |
| entry_outside_market_path     |      1 |

`episode_unavailable` is the replay dataset's own eligibility filter: those
episodes are excluded from the exit-policy family, so they are excluded here
too. Reconciling them would mean comparing against episodes the machinery under
test does not itself accept.

**Loosening that filter to reach 80 is exactly what this pass may not do.** It
would be choosing the population after seeing the result, and it is the reason
the contract forbids tuning for a better match.

## Descriptive agreement, not a verdict

| Recorded      | Replayed      | Trades |
| ------------- | ------------- | -----: |
| no_progress   | no_progress   |     48 |
| initial_sl    | initial_sl    |     11 |
| trailing_stop | trailing_stop |      6 |
| max_hold      | max_hold      |      3 |
| no_progress   | trailing_stop |      2 |
| max_hold      | trailing_stop |      1 |
| no_progress   | initial_sl    |      1 |

All four disagreements have the shape bar resolution predicts: the replay sees
a trail or a stop trigger inside a bar whose extremes the broker's ticker
samples never visited, and closes earlier than the broker did. None of them is
the replay inventing a rule that did not fire; every replayed reason is a rule
the policy really contains.

Among the 68 that agreed, the replay exits a median **1.5 minutes later** at a
median **0.22% lower** price. Both are reported, not graded. A zero here would
have been the surprising result, not a reassuring one.

## What happens next

Re-run under this same contract once the sample clears 80. Roughly half of
closed trades are reconcilable and the broker closes on the order of eight a
day, so that is a few days of waiting rather than a change of method.

Until then, every replay-derived statement about exit policy, HYP-021 included,
carries this: the simulator has not been _shown_ to agree with the broker, it
has been _observed_ to, on a sample the contract calls too small.

## What this still does not say

Nothing about live execution. All 936 trades in the database are `paper: true`
with no exchange order id. Agreement here would establish that the replay
reproduces the paper broker, and nothing about slippage, fills, or depth.

---

# Recount, 2026-09-08, after the pairing defects were fixed

A colleague reproduced two defects in the version that produced the result
above: the market path's venue was never compared to the trade's, so a Bybit
trade could be measured against Binance candles and come back `compared` with a
matching reason; and an unmatched `decision_id` silently fell back to the
episode's first decision, whose own `pump_pct` can select a different pump band
and therefore different exit thresholds.

Both were fixed and both are now coverage statuses that cannot reach the
agreement rate. The result above was computed with both present, so it was
re-run at `291f3c8`.

## The number did not move

|                 | Before the fix |          After |
| --------------- | -------------: | -------------: |
| Reconcilable    |             72 |         **73** |
| Reasons matched |             68 |         **69** |
| Match rate      |          94.4% |      **94.5%** |
| Verdict         | `inconclusive` | `inconclusive` |

Neither `exchange_mismatch` nor `decision_unmatched` appears in the new coverage
table at all. **On this cohort the two defects never fired.** Every trade's
episode path came from its own venue, and every recorded `decision_id` resolved
inside its episode.

I expected coverage to fall and said so. It did not, and the honest reading is
that the previous number survives by luck rather than by having been safe: the
defects were real, a colleague reproduced both in isolation, and nothing in the
data prevented them from firing on the next run instead of this one.

The extra trade is one that closed between the two runs.

## The verdict still rests on the floor, not the rate

73 reconcilable against a registered floor of 80. The rate has cleared the 85%
agreement bar on both runs and the sample has not cleared the evidence bar on
either.

Artifact preserved at
`backups/reports/reconciliation/reconciliation-guarded-291f3c8.md`, inside the
research archive family.
