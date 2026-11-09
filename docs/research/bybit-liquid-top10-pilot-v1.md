# Liquid top-10 flow pilot: counts on two new blocks (pre-protocol, v1)

Status: **rule fixed 2026-11-09, before any fetch; registers no hypothesis.** It follows
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
