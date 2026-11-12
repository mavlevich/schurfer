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
   | Backward holdout, 2023-02-01 to 2024-09-15    | unassigned (track A closed)           | once for a future frozen batch             |
   | Forward data after v2's terminal state        | renewable: one fresh quarter per exam | from the quarter after v2's terminal state |

5. **One signal code** for history, paper and live: the laboratory, the exam and the
   paper or live run call the same function on the same record of what was known at
   decision time. A strategy whose paper or live signals cannot be reproduced from its
   recorded inputs stops at paper.
6. **Unchanged:** the HYP-012 v2 blind window (nothing on or after 2026-09-29), the
   seen-windows registry row before every fetch, Bybit USDT perpetuals as the only
   execution venue, per-action approvals (below).

## 2. How the tracks run (revised 2026-11-12)

On 2026-11-11 another agent read the returns of HYP-032, HYP-033, HYP-034, HYP-031 before settlement and a Bybit against Hyperliquid screen on the discovery year, in scratch, with no protocol fixed before the read. The per-line counts it reported (6, 33, 16, 10 and 5) add to 70, plus a seconds block on 8 settlements; its own total said 68. These counts mix rules and diagnostic cuts, and the split into rules, cuts and events cannot be rebuilt, so 70 is a lower bound for the variants seen. Its scripts were deleted; its command output survives in the session's logs on the owner's machine, indexed with digests in the [evidence index](exploratory-reads-2026-11-11-evidence.md). The figures below are reconstructed from that output, except the Bybit first-minute move, found only in the agent's prose. The agent's statement that the test year, the holdout and the v2 blind window are untouched is recorded as its own; the logs cannot prove a negative.

A second series followed the same night: 54 configurations of pump long, post-pump short and maker entry, scripts and results kept and their digests rechecked, no network request. The maximum t was 0.97 and nothing goes to a holdout. Its maker cells measured adverse selection: the signals whose limit order never filled in the model were the best ones. With both series, at least 124 variants of the discovery year have been seen outside a protocol. The reads are recorded in the
[seen-windows registry](seen-windows-registry.md) and the
[discovery ledger](discovery-ledger.md); they decide the plan as follows:

- **Track A is closed, as a priority decision.** No HYP-033 successor is registered on the discovery year after 33 variants of its returns were seen; its best cell (F < 0 without high G, +81 bps) is carried by 19
  market-wide hours, isolated impulses lose, and a one-slot book loses 61 bps per trade.
  HYP-034 and HYP-032 are closed too. The holdout batch is not spent on any of them. These reads do not by themselves rule out a future protocol on a clean, unseen window; the budget simply goes to track B.
- **Track B is the only track, on 4-hour contracts.** With a decision-time signal
  (F_hat at t - 1 h from hourly premium) the long into settlement made +55.3 bps gross on
  4-hour contracts (1,026 clusters, 206 symbols), +25.3 +- 19.8 after a 30 bps taker
  round trip, but -2.0 without the top 5 symbols; on 8-hour contracts it lost before
  costs (-4.7 gross). **4-hour contracts were chosen after seeing these returns;** that choice is part of the selection history and is counted in any later confirmation. B1 fetches and reports every interval, but B2 for 4-hour contracts starts only when the 4-hour group passes on its own: S1 on the 4-hour settlements with the same thresholds and minimum counts, and the 4-hour group's own event floor at the chosen d. A pooled `pass` is not enough. 1-hour and 2-hour contracts go on only if B1 shows the same for their own group.

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
   reused as a result. **B2 starts only on B1's verdict `pass` for the 4-hour group on its own (section 2);** `insufficient_validation`,
   `not_reconstructible` and `insufficient_events` stop the track or send it back to
   design.
2. **Costs.** At a 30 bps taker round trip about +5 bps would remain, which does not
   trade. The case rests on a **maker entry** (0.02% fee, no spread paid), simulated with
   the strategy's **order book fill model** (`ob500` from 2023-01-18, volume-limited
   primary, looser model as sensitivity). The protocol fixes the maximum wait of the
   limit order, what happens to an unfilled or partly filled remainder, and counts every
   missed signal: a result on filled orders only is not a result. Limit orders fill most often when the price moves against them; the fill model must show the edge survives that. The 2026-11-11 pump-short lab measured exactly this, with fills decided by its bar model. In its 16 hourly configurations 15.9 to 35.6% of signals never filled; the taker Net30 of the unfilled group was +82.1 to +239.9 bps and of the filled group -151.0 to -8.4. In its 2 minute configurations 7.0 to 15.5% never filled, at +200.1 to +253.1 against -86.3 to -64.1. B2 therefore reports, for every configuration, the modelled fill share, the taker net of the filled and of the unfilled group, and the maker result over all signals.
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

## 4. Track A: closed

See section 2. HYP-037 stopped at its calibrated power gate (#584, MDE 123 and 73 bps)
before any return was read, and it is not reopened; the three candidates declared
on 2026-11-11 (all impulses, F < 0 with high G, F < 0 alone) were read in the
exploratory session, and their results are in the ledger.

## 5. Screens excluded from this plan

- **Bybit against Hyperliquid funding and basis:** excluded from the current budget, as a priority decision; the screen does not show the trade loses. The 2026-11-11 screen (817 settlements, 68 shared coins) found the funding difference at t (+44.6 bps) mostly gone over the next 4 hours (+11.8). Its best regime (8-hour Bybit contracts, short Bybit and long Hyperliquid for 4 hours, +51.2 gross) gets +28.5 of it from the basis. The agent's prose, not confirmed by any saved command output, mentions a sharp Bybit move in the first minute after settlement; Hyperliquid's price for that minute was not fetched (4-hour candles only), so whether the basis converges in the first minute, and how much would remain after t + 1 min, are unverified assumptions. Bybit's +-5 second funding warning concerns the seconds around t, not the whole first minute. The 1-hour regime is carried by five pumped coins. Venue access from Poland
  is still `unknown`. Ledger: "Bybit vs Hyperliquid (exploratory)".
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
| 11-12 to 12-01 | B1 fetches and report; B2 maker fill model for 4-hour contracts if B1 passes                           |
| 11-30 00:00    | HYP-012 v2 checkpoint; evaluated from 12-01 as that cut, with the registered command on the owner's go |
| 12-01 to 12-15 | B3 laboratory read (maker and taker, 4-hour contracts); B4 protocol frozen                             |
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
