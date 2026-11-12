# Method retrospective: which gate stopped each line (planning note v1)

Status: **planning note, 2026-11-10; decides nothing, registers nothing and changes no
rule.** Asked by the owner after the [resume readiness note](resume-readiness-v1.md):
with this much data and infrastructure, did the method itself keep the program from
moving? It reads nothing new. Every number comes from the [discovery ledger](discovery-ledger.md)
and the stop records it links.

## 1. Two kinds of stop

The record holds two different kinds of negative result, and they mean different things:

- **Read and failed.** The returns were read under a registered protocol and the money
  question got a negative answer.
- **Stopped before any return.** A data, balance or power gate failed, so nothing is
  known about an edge either way.

| Line                                                                                                 | Kind                  | What stopped it                                                               |
| ---------------------------------------------------------------------------------------------------- | --------------------- | ----------------------------------------------------------------------------- |
| HYP-012b, HYP-012c (source lead)                                                                     | read and failed       | every venue's mean net negative; 012c -0.455%                                 |
| HYP-029 (MEXC early trigger)                                                                         | read and failed       | gross +1.26%, median about 0, net CI spans 0                                  |
| HYP-028 (delayed fade short)                                                                         | read and failed       | +0.29% with a CI across 0, every stop turns it negative                       |
| HYP-015 (hold 12 h)                                                                                  | read and failed       | net -64 bps                                                                   |
| HYP-031 minute (post-funding short)                                                                  | read and failed       | net -13.0 bps at 30 bps; gross break-even +17.0                               |
| Carry screen v1.1                                                                                    | read and failed       | best +0.3% a year                                                             |
| HYP-011, HYP-013, HYP-014, HYP-016, HYP-017 (summer)                                                 | stopped before return | capture integrity, too few triggers or unresolved paths                       |
| [HYP-032](bybit-compressed-spring-v1.md)                                                             | stopped before return | **leg (b)**: control losses concentrated on weeks                             |
| [HYP-033](bybit-squeeze-continuation-v1.md)                                                          | stopped before return | **leg (b)**: 61.2% of events with a control (floor 80%), (b) MDE 61.5         |
| [HYP-034](bybit-premium-dislocation-v1.md)                                                           | stopped before return | **leg (b)**: after the balance amendment, 70.9% with a control, (b) MDE 50.13 |
| [HYP-035](bybit-relative-strength-v1.md)                                                             | stopped before return | balance 0.132 and **both legs** above 50 bps                                  |
| [HYP-036](bybit-absorbed-buying-v1.md)                                                               | stopped before return | **leg (b)**: 56.4% with a control (gate 85%), z1 balance 0.175                |
| [Family 5, top-10 pilot](next-candidate-feasibility-v1.md), [BTC trend](btc-trend-feasibility-v1.md) | stopped before return | power on paper, or coverage and balance                                       |

The "read and failed" lines are real answers, and this note does not reopen them. They
were the program's most direct ideas (follow a pump's source, fade it, hold momentum,
short after extreme funding), and each lost after costs.

## 2. The finding: the comparison leg stopped four lines whose money leg had power

From HYP-032 on, every design asked two questions:

- **(a) net:** does the trade, as it would be traded, earn more than its costs?
- **(b) excess:** does it earn more than a matched control, a similar instrument at a
  similar moment without the signal? This is the test of "more than the price path".

The floor required both to be testable, and the reading required both to pass. The
money question is (a). Its power, recorded before any return:

| Line    | MDE (a), bps | Limit | (a) testable | What failed                 |
| ------- | -----------: | ----: | ------------ | --------------------------- |
| HYP-032 |         44.7 |    50 | yes          | (b) control losses on weeks |
| HYP-033 |         43.7 |    50 | yes          | (b) control share, (b) MDE  |
| HYP-034 |         38.9 |    50 | yes          | (b) control share, (b) MDE  |
| HYP-036 |         19.9 |    45 | yes          | (b) control share, balance  |
| HYP-035 |         88.7 |    50 | no           | both legs and balance       |

So **four of the five strategy v2 families were stopped by the comparison, not by the
money question.** In each, (a) could have been read at the registered size. HYP-034
even passed every item of its first count (`pass_to_reading`); its own balance amendment,
made before any return, then cost it the control share.

Why (b) was there, and why that was not wrong:

- A long that earns in a rising market may only be carrying the market. A control tells
  a signal from a price path, which matters for whether an edge lasts.
- The program had just been burned by results that did not replicate (HYP-012's
  discovery, the MEXC cell). A strict second leg was a reasonable answer to that.

Why it cost more than it protected:

- The controls failed for structural reasons (scarce matches, imbalance on the trigger
  variable), so the gate measured the matching, not the signal.
- Most of what (b) protects against is also covered by the rest of the reading: both
  halves positive, the result without the top 5 symbols and the top 5 weeks, the
  week-block bound, and later the frozen holdout batch on 2023-02 to 2024-09.
  [Strategy v2](research-strategy-v2.md) already puts the decisive protection there.
- Market drift can be removed without matching: report (a) adjusted by BTC over the same
  minutes. Every design's reading inputs already include BTCUSDT minute klines for the
  same intervals.

## 3. Other method costs, smaller

1. **Infrastructure before a cheap screen (summer).** HYP-011 to HYP-017 died on capture
   integrity and missing triggers, after live capture was built for them. Since October
   the order is history first, capture after, and every screen fails fast and cheap.
   That lesson is learned.
2. **One cost for every instrument.** 30 bps round trip was HYP-031's assumption,
   reused. It decides none of the stops above: no line with a negative read was within
   a few bps of zero except HYP-031 minute, whose gross +17.0 is under any taker cost.
   A maker entry was never tested live; the [execution inventory](execution-edge-inventory-v1.md)
   is paused because no signal pays.
3. **One class of idea.** Almost everything was a short-horizon price pattern on
   altcoin perpetuals. Copying measured traders and maker trading were not tried.

## 4. What a net-primary design would change

A **v2 of a stopped family** that makes (a) the primary and (b) a diagnostic, decided
before any return:

- **Primary:** the cluster mean net at the registered cost, funding included, at the
  registered horizon; the floor of counts, symbols and weeks unchanged; at least +30 bps.
- **Guards kept:** the week-block bootstrap bound above 0, without the top 5 symbols and
  the top 5 weeks, both halves positive.
- **Market drift:** the BTC-adjusted net reported beside it, and required to stay
  positive (no matching needed).
- **(b) and the control share:** reported, deciding nothing.
- **Confirmation unchanged:** a pass is a stage 1 candidate, frozen into the holdout
  batch; never orders.

Candidates, by cost to read (inputs from their own protocols):

| Family  | Inputs held                             | Reading needs                                                     | (a) MDE |
| ------- | --------------------------------------- | ----------------------------------------------------------------- | ------: |
| HYP-034 | features snapshot + premium index fetch | minute klines, a few MB; a 48 h live publication check (after v2) |    38.9 |
| HYP-033 | features snapshot                       | minute klines, a few MB                                           |    43.7 |
| HYP-032 | features snapshot + sealed tape         | minute klines, a few MB                                           |    44.7 |
| HYP-036 | 2 pilot blocks of tape                  | the full tape, about 526 GB                                       |    19.9 |

**Open questions for the decision**, not answered here:

1. **The window.** These families' discovery window (2024-10-16 to 2025-09-30) was read
   at the features level (counts, controls, power), never for a return. The strategy
   asks a new test of the same idea for "an untouched window". Whether features-level
   exposure touches it is the key question. My position: it does not for the money leg,
   because nothing seen predicts (a)'s sign, provided the change is fixed before any
   read and each family keeps its own registration.
2. **Selection.** Picking the families whose (a) had power is outcome-blind (power uses
   dispersion, not means), but every revived family and variant enters the ledger and
   the batch counts all of them.
3. **The 11-09 stop.** Reviving families is a change to the program review's decision,
   under its third route ("controls a stopped design lacked", read here as a design that
   no longer depends on controls). It is the owner's decision with the colleague.
4. **Live checks.** HYP-034's publication check reads live data and waits for v2's
   terminal state; a read on history does not.

## 5. Position

Mine; the colleague reviews before merge.

- The read-and-failed lines stay closed; the method did its job there.
- The comparison gate was the costliest rule of strategy v2. It stopped four of five
  families before the money question was asked.
- Proposed next step, if the owner and the colleague agree: a **net-primary amendment**
  for HYP-033 and HYP-034 first, as one written protocol change per family, fixed before
  any return. They are the cheapest to read (a few MB each, historical only). HYP-032
  follows if they are clean. HYP-036 waits, because of its 526 GB fetch.
- A pass is a stage 1 candidate for the holdout batch, not money. A fail closes the
  family for good, which is still progress compared to "nothing is known".
