# Liquid top-10 flow pilot: counts on two new blocks (pre-protocol, v1)

Status: **rule fixed 2026-11-09, before any fetch; counts read the same day: fail (below). Registers no hypothesis.** It follows
the [next-candidate note](next-candidate-feasibility-v1.md) and the colleague's review
of it: before a protocol, check on two new calendar blocks whether HYP-036's event on
the liquid top 10 has the rate, the same-instrument controls and the balance the seen
blocks suggested. Counts only: no trade return is computed. The fetch needs the owner's
go.

**What the sample is.** The **liquid top 10**: each month's 10 largest instruments of
HYP-036's pool by 30-day turnover, BTC excluded (it is the benchmark). It includes names
such as 1000PEPE, WIF and FARTCOIN; it is not a set of classic majors, and a study of
BTC, ETH and a few large assets would need its own count.

## The question this pilot prepares

Does an hour of extreme net taker buying predict the next 4 hours **against the same
instrument's ordinary hours** with a similar price move, activity and volatility? A
control from neighbouring days removes most differences between instruments but loses
the protection of the same hour against a common market move, so the comparison (leg b)
is on BTC-relative returns for both sides; the traded net (leg a) stays a separate
metric with actual costs. Neither return is read in this pilot.

## Fixed now

- **Blocks** (decision days, UTC, inclusive): **2024-11-18 to 2024-12-01** and
  **2025-04-14 to 2025-04-27**. Neither overlaps HYP-036's pilot scope, and both leave
  room for the control window inside the features window.
- **Pool:** the monthly top 10 of HYP-036's pool for the decision's month.
- **Event:** HYP-036 v1's, unchanged: qB >= 0.95, B > 0, qV >= 0.5, abs z1 <= 1, first
  in 8 h; H = 4 h.
- **Control candidates:** the **same instrument** at a decision hour more than 8 h and at
  most **7 days** from the event (before or after), every item defined, qB 0.15 to 0.85,
  qV >= 0.5, abs z1 <= 1, the instrument tradable through the hold.
- **Calipers:** abs log sigma1 ratio <= 0.25, abs dz1 <= 0.25, abs dz4 <= 0.5 and
  **abs dqV <= 0.10**; up to 5 nearest by the root of (dz1 / 0.25)^2 + (dz4 / 0.5)^2 +
  (dqV / 0.10)^2, ties by the sigma1 ratio, then by time. Activity is matched, not
  stratified: 52 events cannot fill strata.
- **Scope:** each block's top-10 instruments from 22 days before the block (15 days of
  rank and sigma history for a control up to 7 days earlier) to 7 days after it, so an
  event at a block's edge has the same chance of a control.

## Gate (on the two blocks' counts)

1. **Coverage:** at least **85%** of events with a control, overall and in **each** block.
2. **Balance:** overall abs SMD at most **0.10** for **z1, z4, ln sigma1 and qV** (leg (b)
   weights, v1's formula; different means with no spread fail). ln V and BTC's own z4 at
   t are reported beside them; block figures are reported.
3. **Instruments:** the effective number of symbols by event weights (1 over the sum of
   squared cluster-weight shares) at least **6**, and the largest symbol at most **25%**
   of the weight. ISO weeks and the top week's share are reported (two blocks cannot show
   a year's stability).
4. **Power:** scaled MDE at most **45 bps** for (a) net and (b) BTC-relative excess, with
   v1's power code (hourly blocks, monthly rho of the top 10, inflation 1.5, design effect
   1.2), scaled to the full window at a **conservative** yearly rate: the events' count
   replaced by its one-sided 95% lower Poisson bound.

**Verdicts:** **pass** when all four hold (a protocol may then be written); **fail** when
coverage, balance or instruments fail, or the MDE fails even at the point rate;
**indeterminate** when only the conservative-rate MDE fails (too few events to tell). No
rule is adjusted after the counts.

## Scope and fetch

From f0b8b72 (clean tree), with HYP-032's archive-size record (`22e7c119...`), no
request: `scope.json` `5fecacfe174a33df9f689f92ef0892cc7867f3300c882e9e3c92ed18ee66e7f2`,
`scope-size.json` `b27b8448ae455d35ec1723dbfdb6eef96718d6b39ef29dc30a30441fb20469f8`:

| Item                              | Value                 |
| --------------------------------- | --------------------- |
| Instruments                       | 16 (7 in both blocks) |
| Symbol-days                       | 974, all listed       |
| Estimated compressed size         | **28.6 GB**           |
| Shared with HYP-036's pilot scope | 0 symbol-days         |

The estimate uses each symbol's mean sampled size over the year; November 2024 traded
heavily, so the real size may be higher. **Requested on the owner's go:** HYP-032's
fetcher, rules `tape-v3`, a new run bound to this scope, a ledgered byte cap of **35 GB**
(stop and ask if it is reached), about 30 to 40 minutes at the pilot's measured rate.
Then the counts code (its own commit before it runs) and the gate above.

## Result (2026-11-09): fail

On the owner's go. The tape (HYP-032's fetcher, rules `tape-v3`) stopped at the 35 GB cap
with 870 of 974 parts: the real files were 1.47 times the estimate (November 2024). On a
second go the total cap was raised to 45 GB; all 974 parts are `ok`, 40,718,077,705
bytes read in all, every checkable hour matched its kline (14,142, none mismatched).
Manifest `371807a5c75a90e854d1cf8540ddbbacc2f36fe7cb72c5322f7ebcf1ed4c3a94`. Counts from
40932ce (committed before the run, clean tree): `counts.json`
`8f33497b1295ff398e636a932d6855a28083d3d92b0b0f6cd36b111312a55cce`, `pairs.json`
`5284ef66b8c0ec011a7702e136d73459e4b5138e49b7fe83bea0fc3d506e25c1`.

| Gate item                             | Value                                                      | Limit        | Holds |
| ------------------------------------- | ---------------------------------------------------------- | ------------ | ----- |
| Events (determinable 100%)            | 55 (19 and 36 by block), 52 clusters, 95% lower bound 40.7 |              |       |
| Coverage, overall / by block          | **80.0%** / **73.7%** and **83.3%**                        | >= 85% each  | no    |
| abs SMD z1, z4, ln sigma1, qV         | **0.145**, 0.013, 0.038, 0.042                             | <= 0.10      | no    |
| Instruments                           | 13 symbols, effective 10.2, largest 15.4%                  | >= 6, <= 25% | yes   |
| MDE (a), (b) at the point rate        | **48.4, 46.8 bps**                                         | <= 45 bps    | no    |
| MDE (a), (b) at the conservative rate | 54.7, 52.9 bps                                             | <= 45 bps    | no    |

Reported only: ln V SMD -0.02, BTC z4 SMD -0.05 (-0.27 in November); 4 ISO weeks, the
largest 38.5% of the weight. Block balance of z1: 0.141 and 0.148.

**Reading.** The qV caliper did its job (qV SMD 0.80 in the seen blocks, 0.04 here), and
the event rate held (55 against 52). But on new blocks the same-instrument control fails
the coverage gate in both blocks, the z1 imbalance is back above 0.10 (net buying still
comes with up hours, against an instrument's own ordinary hours too), and the MDE is
above 45 bps even at the point rate (the seen blocks suggested 36 and 34). **By the rule
fixed above, the verdict is fail: no protocol is written for the liquid top 10, and no
rule is adjusted.** No trade return was computed.

**Next (as agreed):** family 2 has now failed its comparison three ways on Bybit (v1
same-hour tercile, the whole same-hour pool, the same instrument over a week), each time
on coverage or z1 balance. The agreed fallback is the strategy's: change the kind of
edge, not the next tolerance.
