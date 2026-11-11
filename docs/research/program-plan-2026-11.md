# Program plan after the method retrospective (proposal, 2026-11-11)

Status: **proposal for one review round (the colleague and two outside reviewers), then
the owner's decision.** If adopted, it amends the [2026-11-09 program review](program-review-2026-11-09.md),
which stopped the program, and [strategy v2](research-strategy-v2.md) where section 1
says so. Until then nothing below runs. It follows the
[method retrospective](method-retrospective-v1.md) and the
[resume readiness note](resume-readiness-v1.md).

## Goal and stop rule

**Goal:** within three months, one candidate that has passed an untouched window and is
in paper or LIVE_PROBE at USD 50, or a precise statement of what is missing to test one.

**Stop rule:** if by **2027-02-11** no candidate has passed an untouched window, the
program returns to maintenance until HYP-012 v2's read, and the owner decides in March
whether to close the project or make one different attempt.

## 1. Rules: laboratory and exam

1. **Laboratory = the discovery year,** decisions 2024-10-01 to 2025-09-30, Bybit public
   history. A stopped family's returns may be read there under a written laboratory
   protocol, fixed before the read: horizons, costs, exits and variants. Every variant
   is counted in the [discovery ledger](discovery-ledger.md); a laboratory result is
   never evidence by itself.
2. **The primary measure is net, as traded:** the cluster mean net at the protocol's cost,
   funding included. Beside it, the **market-adjusted net** (minus BTCUSDT, and minus the
   median of liquid perpetuals over the same minutes, with the median's pool fixed point
   in time before the read) must stay positive. A matched control and its excess are
   diagnostics and gate nothing.
   A **portfolio report** is required beside the per-event net: slot occupancy, every
   skipped signal with its reason, daily PnL on the available capital and drawdown, with
   the number of slots set from the event rate before the read. A rarer filter can raise
   the mean trade and lower the money made on the capital, and the report must show it.
3. **The guards stay:** the floor of counts, symbols and weeks; the ISO-week block bound;
   without the top 5 symbols and the top 5 weeks; both halves.
4. **Exams are scarce and assigned now:**

   | Exam window                                   | Assigned to                           | Read                                       |
   | --------------------------------------------- | ------------------------------------- | ------------------------------------------ |
   | HYP-031's test year, 2025-10-01 to 2026-09-28 | track B (HYP-031 family) only         | once, as one frozen batch (below)          |
   | Backward holdout, 2023-02-01 to 2024-09-15    | track A, one frozen batch             | once for the whole batch                   |
   | Forward data after v2's terminal state        | renewable: one fresh quarter per exam | from the quarter after v2's terminal state |

5. **One signal code** for history, paper and live: the laboratory, the exam and the
   paper or live run call the same function on the same record of what was known at
   decision time. A strategy whose paper or live signals cannot be reproduced from its
   recorded inputs stops at paper.
6. **Unchanged:** the HYP-012 v2 blind window (nothing on or after 2026-09-29), the
   seen-windows registry row before every fetch, Bybit USDT perpetuals as the only
   execution venue, per-action approvals (below).

## 2. How the tracks run

Track A's HYP-033 is the **first practical run**. Track B's first step, B1, runs **in
parallel as a feasibility study**: B2 and later start only if B1 rebuilds the signal
without future information. The first week therefore aims at
two concrete answers: a checked calculation for HYP-033, and whether the data supports a
pre-settlement funding signal at all.

## 3. Track B: HYP-031 family, entry before settlement

**Question.** On Bybit USDT perpetuals with extreme negative funding, does a long opened
by a resting limit order about 30 minutes before settlement and closed at or just before
settlement earn more than its costs, on the test year?

**Why it is worth a feasibility study.** The minute discovery's pre-settlement
diagnostic was **+35.5 bps gross** (long, t - 30 min to t, 4,928 events, 357 symbols, 53
weeks; `result.json`). That number **does not carry over**: its events were selected by
the rate settled at t, which a decision at t - 30 min does not have. The mechanism is a
schedule (funding collectors buy before the snapshot), our size fits thin instruments,
and 30 minutes means no latency race.

**What can kill it, in order:**

1. **The signal at decision time (B1, a short study of its own,
   [design](bybit-presettlement-feasibility-v1.md)).** The signal is the **uncapped rate
   implied by the premium** observed by t - d, computed with the published formula, plus
   a filter of the strategy's own: **extreme funding in the past week** (a settlement at
   or below -0.30% within the 7 days before t - d). Historical caps are not published and
   a past settlement does not prove the cap in force, so B1 does not claim to rebuild the
   capped rate, nor the rate Bybit displayed; the displayed rate is a live check for the
   shadow stage. Forecasts are rebuilt at each decision time from the settlements known
   then. S1 first checks that the formula reproduces settled rates (2 bps, and the -0.30%
   classification on each side of the trigger), with minimum counts and coverage. The
   event list is rebuilt from scratch; nothing of the old 4,928 events or of +35.5 bps is
   reused as a result. **B2 starts only on B1's verdict `pass`;** `insufficient_validation`,
   `not_reconstructible` and `insufficient_events` stop the track or send it back to
   design.
2. **Costs.** At a 30 bps taker round trip about +5 bps would remain, which does not
   trade. The case rests on a **maker entry** (0.02% fee, no spread paid), simulated with
   the strategy's **order book fill model** (`ob500` from 2023-01-18, volume-limited
   primary, looser model as sensitivity). The protocol fixes the maximum wait of the
   limit order, what happens to an unfilled or partly filled remainder, and counts every
   missed signal: a result on filled orders only is not a result. Limit orders fill most
   often when the price moves against them; the fill model must show the edge survives
   that.
3. **Exit and funding.** Exiting before t forfeits the payment; holding through t
   collects it but meets the first-minute fall. Both are fixed exits, declared before
   the read; no exit is tuned on the year.

**Steps.**

| Step | What                                                                                                      | Needs                      |
| ---- | --------------------------------------------------------------------------------------------------------- | -------------------------- |
| B1   | Feasibility: uncapped premium-implied rate at t - d plus the past-week filter; S1; event list by interval | fetch go (discovery year)  |
| B2   | Maker fill model on the order book archive for the events' minutes, discovery year (only on B1 `pass`)    | fetch go                   |
| B3   | Laboratory read: net of taker and maker versions, both exits, portfolio report, by settlement interval    | owner go to read returns   |
| B4   | Protocol frozen (trigger, entry, exit, costs, fill model), then **one exam read on the test year**        | colleague review, owner go |
| B5   | Shadow run: the order path without sending orders (placement, cancel, timeout, partial fills, recovery)   | deploy go                  |
| B6   | LIVE_PROBE at USD 50 for real fills, under the scaling rule (section 6)                                   | owner go                   |

**The test year is read once, as one frozen batch.** Every candidate that goes to it, and
its criteria, is frozen before the read, with the batch's multiplicity counted, and the
results come back together, so the first cannot steer the choice of a second. Only the pre-settlement long goes to it.

**The seconds-level short after settlement is out of this plan** (2026-11-11). Bybit's
help center warns that whether a position takes part in funding is uncertain within
+-5 seconds of the settlement timestamp, and the settled rate was published 0.4 to 3.6 s
after t in the one live check. So an entry would come after t + 5 s. That warning does
not show that the price move is gone by then, nor that a later entry is safe; how much of
the first-minute move remains was never measured. It is recorded in the ledger as
parked, untested, with that reason.

## 4. Track A: HYP-033 only, as a successor protocol

Its discovery year is **reused discovery with setup outcomes unread**: features, counts
and power were seen, no return of the setup was. The successor gets its own unused id
(working label HYP-037) and a short protocol that makes net primary (rule 2) and keeps
the rest of HYP-033, fixed before the read (minute klines of a few MB). **Three
candidates, declared now and counted together:**

- **A:** every liquid first impulse (about 12,000);
- **B:** F < 0 and G at or above its 80th percentile (HYP-033's event, 986);
- **C:** F < 0 alone, whatever G (funding only; B is the part of C with G at or above
  its 80th percentile). Chosen before any return is read.

Each is a candidate on its own net. The increments of B over A and of B over C (the
value of G given negative funding) are estimated per event (difference of mean nets,
week-block bootstrap), where A's large sample keeps
its power close to B's own; it is reported and does not gate. A one-slot daily
comparison is part of the portfolio report, not the primary: with one slot and a 4-hour
hold, A could take at most 6 of its about 34 daily impulses, so which ones it takes
would depend on tie order, not on the signal. A survivor goes into the holdout batch.
HYP-034, HYP-032, HYP-035 and HYP-036 are not in this plan, to keep the budget on one
read.

## 5. Screens excluded from this plan

- **Bybit against Hyperliquid funding spread:** excluded from the current plan
  (2026-11-11); the results of the check are awaited. Its ledger entry needs the period,
  instruments, holding rule, accumulated funding spread, the four fees and other costs,
  and the data source with its fingerprint (the check used Hyperliquid history no later
  than 2025-10-01, clear of the blind window). Until that report exists, no economic
  conclusion is drawn: four fills alone do not prove a loss.
- **Hyperliquid positions as a feature:** closed for now. The public API returns current
  positions, not their history for 2024 and 2025, so a decision-time feature cannot be
  rebuilt on history.

## 6. Capital: the registered scaling rule

No new ladder. Size moves only under [strategy v2's scaling rule](research-strategy-v2.md)
(section "The scaling rule"): checkpoints at **50, 200 and 500 trades** and only there;
an allowed loss of edge δ fixed before the first live trade; the size goes up one step
only when the lower one-sided bound of (live mean minus the exam's estimate) is above
minus δ, with the dependence between trades and the number of checkpoints accounted
for; costs within the model's tolerance; drawdown within a dollar limit set in advance.
LIVE_PROBE's loss threshold L and its records stay. With a 3% per-trade spread the early
checkpoints mostly test costs and risk, so the first steps are slow by design.

## 7. Calendar

| When           | What                                                                                                   |
| -------------- | ------------------------------------------------------------------------------------------------------ |
| 11-11 to 11-17 | this plan reviewed and decided; 11-15 restore drill; ~11-17 disk growth reading                        |
| 11-18 to 12-01 | HYP-033 amendment, synthetic check and laboratory read prepared; B1 feasibility in parallel            |
| 11-30 00:00    | HYP-012 v2 checkpoint; evaluated from 12-01 as that cut, with the registered command on the owner's go |
| 12-01 to 12-15 | HYP-033 laboratory read; B2 and B3 only if B1 passed; B4 protocol frozen                               |
| 12-15 to 01-15 | B4 exam read; a pass goes to the shadow run, then LIVE_PROBE at USD 50                                 |
| by 2027-02-11  | gate: a candidate in paper or live, or maintenance until March                                         |

## 8. Approvals and what does not change

- **Owner go per action:** every fetch, every read of returns (laboratory or exam), every
  deploy, migration and LIVE_PROBE start, every group 2 stop.
- **Colleague:** design before building and one review before merge, per track.
- **Production:** HYP-012 v2's capture keeps running; before each group 2 stop, check
  whether v2 uses that source.
- **Not in this plan:** new venues for execution, latency infrastructure, liquidation-map
  trading, machine learning before a working signal exists, HYP-024 and the other closed
  lines.
