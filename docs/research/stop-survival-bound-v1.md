# HYP-020 — Does the low-score band survive its own stop? A bound.

**Status: computed 2026-09-07. The declared rule fires, but the result argues against acting on it. The contract below was committed (`25dd6bf`) before any stop-aware figure existed.**

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

---

# Result, 2026-09-07

| score |      n | bases | % stopped | **median bounded %** | mean bounded % |
| ----: | -----: | ----: | --------: | -------------------: | -------------: |
|     1 |  7,532 |   176 |      39.0 |               −0.073 |         +0.206 |
|     2 | 30,618 |   360 |      30.1 |           **+0.396** |         −0.111 |
|     3 | 58,411 |   399 |      19.6 |           **+0.155** |         −0.131 |
|     4 | 40,712 |   304 |      19.1 |               −0.365 |         −0.504 |
|     5 | 15,094 |   211 |      15.6 |               −0.730 |         −0.882 |
|     6 |  4,564 |    83 |      19.7 |               −1.126 |         −1.668 |

## The declared rule fires

Scores 2 and 3 have a positive median bounded return on far more than 100 outcomes and 30
symbols. By the contract above, that says register a forward cohort.

## Why the rule was underspecified, said plainly

The contract chose the **median** and did not name the mean. That was a mistake in the
contract, and it is recorded rather than quietly corrected:

- score 2: median **+0.396**, mean **−0.111**
- score 3: median **+0.155**, mean **−0.131**

A positive median with a negative mean is the signature of a long left tail, which is
exactly what a stop produces: many small wins, a minority of −10% losses. A repeated bet
compounds the mean, not the median. On that statistic the band does not pay.

Switching the criterion after seeing the numbers would be the fitting this ledger exists
to prevent, so the verdict stands as the contract wrote it: **the rule fires**. The
recommendation below is separate, post-hoc, and labelled as such.

## What survived and what did not

**The ordering survived.** Under the stop bound the levels still fall away from 1 through
6, so HYP-019's finding that the score points the wrong way is not an artefact of ignoring
the stop.

**The money did not.** Score 1, the best band at hold-to-horizon (+2.67%), collapses to
−0.073% once every position that touched −10% is treated as stopped; 39% of them did. The
stop consumes the entire edge.

## The bound is one-sided in both directions

The pessimistic assumption means a **negative** figure here proves nothing either: the
true value lies between this bound and hold-to-horizon. Scores 2 and 3 sit near zero from
both directions, which is the least useful place for a bound to land.

## What is actually needed

Not another aggregate over this table. The question "did the stop or the target come
first" is unanswerable from stored extremes, and no amount of re-querying changes that.

It needs **per-minute path replay** for these decisions, which this repository already has
a protocol for (`episode-replay-protocol-v1.md`). The constraint is coverage:
`timeseries.bybit_momentum_bars_1m` holds exact minute bars for Bybit only, while these
decisions are spread across lbank, mexc, bingx and others. That is `ENG-019` again, and it
is now the binding constraint on answering a strategy question, not just an evidence-
quality concern.

## Recommendation, post-hoc and not part of the contract

Do not register a forward cohort for the low band on this basis. The bounded edge is
within noise of zero on the median and negative on the mean, and a forward cohort would
spend weeks to measure something this thin.

Spend the effort on path replay coverage instead. Until "which came first" is answerable,
every stop-related question about this strategy family stays bounded rather than
answered.

---

# Correction, 2026-09-07, before merge

The stop model above is **not the policy this system runs**, and the numbers should be
read accordingly.

`exit.exit_params` returns a bracket, sized by pump magnitude:

| pump    | initial SL | trail activates | trail | tighten after | max hold | no progress |
| ------- | ---------: | --------------: | ----: | ------------: | -------: | ----------: |
| < 50%   |         8% |              8% |   12% |        90 min |  180 min |      60 min |
| 50-100% |        10% |             12% |   15% |       120 min |  240 min |      60 min |

Three things follow, all of which weaken this pass:

1. **The stop level is not a constant 10%.** It is 8% for the smaller pumps that make up
   much of the low-score band, so this pass applied too wide a stop to exactly the rows
   its conclusion rests on.
2. **It is a trailing bracket, not a fixed stop held to the horizon.** After the trail
   activates, the exit level moves with the position, so a run that goes favourable and
   then reverses exits far above the initial stop.
3. **There are two time-based exits** -- `max_hold_min` and `no_progress_min` -- that end
   positions before 240 minutes regardless of price.

The earlier claim that this is a "pessimistic bound whose truth lies between it and
hold-to-horizon" is therefore wrong in both directions: a tighter initial stop makes it
optimistic for some rows, while a trailing exit makes it pessimistic for others. It is
not a bound at all. It is a calculation against a policy that does not exist.

What survives the correction: the **ordering** finding from HYP-019, which does not depend
on any exit model. What does not survive: the specific claim that the edge is eaten by the
stop. That question is now open again, and answering it needs a replay of the real bracket
against minute bars rather than any aggregate over stored extremes.

Recorded here rather than by quietly editing the numbers above, because the mistake is the
useful part: an analysis is only as good as its model of what the system actually does,
and this one was checked against the code too late.
