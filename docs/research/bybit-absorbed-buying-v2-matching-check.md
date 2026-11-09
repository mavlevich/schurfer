# HYP-036: can a control be matched without changing the signal? (one diagnostic run)

Status: **rule fixed 2026-11-09, before the run; registers no hypothesis.** One
diagnostic run on the sealed pilot tape of [HYP-036 v1](bybit-absorbed-buying-v1.md)
(manifest `3885a196...`, scope `315a343b...`), agreed by the owner and the colleague
after v1 stopped at the pilot gate. No fetch and no trade return. The pilot blocks'
features are already seen, so the rule below is chosen on seen features: if it passes,
a v2 protocol must say so and check the rule on other blocks before any return is read.

## The question

v1 found a control for 56.4% of its events (gate 85%) with z1 SMD 0.175 (gate 0.10).
Dropping the tercile alone raised coverage to 77.6%, and a tighter z1 caliper alone
would lower it. Can one changed matching rule reach the gate with the signal unchanged?

## What stays as in v1

The event (qB >= 0.95, B > 0, qV >= 0.5, abs z1 <= 1, first in 8 h), the pool (monthly
top 150), the decision hours, H = 4 h, the same t, another instrument, the sigma1 ratio
caliper (abs log ratio <= 0.25), the z4 caliper (0.5), up to 5 nearest by (z1, z4) with
the same ties, the control's qV >= 0.5 and abs z1 <= 1, the leg (b) weights, the SMD
formula and the power code (MDE by hourly blocks, inflation 1.5, design effect 1.2,
scaled to the full window).

## The one changed rule (fixed now)

| Item            | v1                          | This run                                                                             |
| --------------- | --------------------------- | ------------------------------------------------------------------------------------ |
| Candidate pool  | event's tercile of the pool | **the whole top 150 at the same hour**                                               |
| Control qB band | 0.25 to 0.75                | **0.15 to 0.85** (symmetric about the median, a gap of 0.10 below the signal's 0.95) |
| z1 caliper      | 0.5                         | **0.25**                                                                             |

No other variant is run. The v1 rule is recomputed by the same code as a check: it must
give v1's 784 events, 442 with a control and z1 SMD 0.175.

## What is reported

- Coverage (events with a control) overall and in each block.
- Balance (abs SMD on leg (b) weights) of z1, z4, ln sigma1 and **ln turnover** (the
  instrument's 30-day turnover before its month, the pool's ranking measure), overall
  and in each block; qV and ln V reported only.
- Control reuse: distinct controls, mean pair slots per distinct control, and the
  largest single control's share of leg (b)'s control weight.
- MDE of (a) and (b) on the new pairs, scaled to the full window as in v1.

## Pass (all must hold)

1. Coverage at least **85%** overall and in **each** block.
2. Overall abs SMD at most **0.10** for z1, z4, ln sigma1 **and ln turnover**. Block
   balance is reported only (about 400 events each).
3. Scaled MDE at most **45 bps** for (a) and (b).

**If it passes:** a v2 protocol may be written, stating that this rule was chosen on
the seen pilot features and checking it on other blocks before reading any return.
**If it fails:** this HYP-036 design on Bybit stops and the next candidate is taken;
the family (flow against price response) is not closed by one failed matching rule.
