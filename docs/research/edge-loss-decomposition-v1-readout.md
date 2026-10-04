# Where the edge is lost v1: readout

Registered study: [edge-loss-decomposition-v1](edge-loss-decomposition-v1.md) with
amendment 1. Read once on 2026-10-04 at reader revision `7b66074`.

**Evidence** ([evidence/edge-loss-decomposition-v1](evidence/edge-loss-decomposition-v1/)):

| File               | sha256            |
| ------------------ | ----------------- |
| `inputs.json`      | `18b90609c908...` |
| `result.json`      | `2b2c1ac2ab9c...` |
| `gate-tapes.json`  | `653a24877ec2...` |
| `gate-result.json` | `7b32ffd1b8ab...` |

**Inputs:**

- Bybit bars of 2026-08-13..09-28 (47 days, each a verified Borg fetch reduced on the
  host).
- Binance from 2026-08-18, the first day with at least 99% price coverage.
- The MEXC 1-minute archive from 2026-08-28.
- 92 Gate July tapes.
- The scanner's 20,653 pump sources (07-23..09-28, four venues).

## Verdict on the primary contrast: negative established

Bybit, 5-minute return of at least +5%, long at the next minute open, 60-minute hold,
middle cost (20.5 bps per side). The values are written in the order they were
computed:

| Resolved firings | Unresolved | Clusters (instrument / day) | SE       | MDE (80%) | Mean net      | 95% CI by instrument | 95% CI by day |
| ---------------- | ---------- | --------------------------- | -------- | --------- | ------------- | -------------------- | ------------- |
| 2,602            | 2          | 419 / 47                    | 19.3 bps | 54.2 bps  | **-41.1 bps** | -79.6 .. -4.7        | -80.0 .. -4.5 |

Gross is about zero (mean -0.1 bps, median -93 bps, quartiles -446 and +265): a 5-minute +5% move on Bybit
predicts nothing for the next hour, so the full round-trip cost is lost. Both upper
bounds are below zero. **Stop investing in speed for this trigger family.** The same
holds descriptively for +3% and +10%, both sides, 15/60/240 minutes, on Bybit,
Binance and MEXC: no 5-minute cell clears the middle cost except the 240-minute shorts
of +10% moves (descriptive, +41 bps net on Bybit).

## Part A: how late the scanner's pumps are seen (descriptive)

Share of the move (24h low to the 24h peak after the crossing) already gone, median
and quartiles:

| Moment                                  | Bybit            | Binance          | MEXC             | Gate (seconds)   |
| --------------------------------------- | ---------------- | ---------------- | ---------------- | ---------------- |
| 5m +3% crossing                         | 0.30 [0.17-0.48] | 0.30 [0.17-0.48] | 0.13 [0.08-0.25] | 0.19 [0.09-0.27] |
| 5m +5% crossing                         | 0.42 [0.26-0.65] | 0.38 [0.22-0.63] | 0.23 [0.12-0.40] | 0.24 [0.15-0.33] |
| 24h +20% crossing (bar close / tape)    | 0.69 [0.51-0.83] | 0.67 [0.49-0.82] | 0.59 [0.40-0.78] | 0.62 [0.43-0.71] |
| same, entry 101 s later (Gate) / model  | 0.69             | 0.67             | 0.59             | 0.62             |
| scanner's actual `first_seen_at` entry  | 0.70             | 0.68             | 0.64             | 0.73             |
| scanner lag after the crossing (median) | 63 s             | 56 s             | 29 min           | 37 min           |

- **The trigger is the loss, not the plumbing.** By the time a 24h change reaches
  +20%, about two thirds of the move is over (the crossing is about 22 hours after the
  move start). Seconds of delay change the share by under one point; on Gate's tapes
  0, 5, 15, 45 and 101 seconds after the crossing are indistinguishable (0.62 each).
- **The scanner adds little on Bybit and Binance** (about one cycle, 60 s), but sees
  MEXC and Gate pumps about half an hour late. A plausible cause is the venues' own 24h
  reference in their tickers; it is not investigated here.
- **Earlier triggers catch moves earlier but select badly.** A 5-minute +3% crossing
  comes when only 13-30% of a later pump is gone, but Part B shows such crossings carry
  no gross edge across the full denominator: most of them are not followed by a pump.

## Exploratory finding (not a verdict): 1-minute bursts on Bybit and Binance

One descriptive family stands out: a **1-minute return of at least +5% with turnover
of at least 5x the median of the prior 60 minutes, long at the next minute open**. It
was found among 108 descriptive cells, so it is a hypothesis, not a result. A further
look on the same data (also exploratory;
[script](evidence/edge-loss-decomposition-v1/exploratory_burst_robustness.py.txt),
[output](evidence/edge-loss-decomposition-v1/exploratory-burst-robustness.json)):

| Venue, 1m >= +5%, 60 min hold                        | Bybit          | Binance         |
| ---------------------------------------------------- | -------------- | --------------- |
| Firings                                              | 740            | 770             |
| Gross, entry at the next minute open (mean / median) | +128 / +55 bps | +183 / +88 bps  |
| Entry one minute later                               | +46 / 0 bps    | +120 / +36 bps  |
| Entry two minutes later                              | +31 / -39 bps  | +91 / -16 bps   |
| Net at 20.5 bps per side, 95% CI by instrument       | +3 .. +175 bps | +62 .. +221 bps |
| Mean without the 5 best instruments                  | +63 bps        | +122 bps        |
| Winsorized 1/99 mean                                 | +111 bps       | +169 bps        |

- **Most of the edge sits in the first minute after the burst.** Waiting one more
  minute cuts the Bybit mean by about two thirds and the median to zero. Today's paths
  enter 45-110 s after the bar closes, so they would miss most of it. This is the one
  place in the study where speed matters.
- **Caveats:**
  - one window, chosen after viewing;
  - weeks vary (ISO week 38 negative on Bybit);
  - the 5 best instruments carry about half the Bybit sum;
  - slippage right after a +5% minute is very likely above 15 bps and is not measured;
  - minute bars cannot show how fast the edge decays within the first minute.
- **MEXC shows the opposite** (median -180 bps at 60 minutes), so the canary venue
  matters (see below).

## What follows

1. **Registered forward test of the 1-minute burst rule on Bybit (proposed HYP-030),**
   with the rule frozen from this readout:
   - a sealed accrual from its merge, read after HYP-012 v2 is terminal;
   - a real-quote cost model;
   - the entry delay fixed as the measured latency of the path that will trade it.

   Nothing here is a trading rule until that test passes.

2. **Seconds-resolution decay on Bybit's own public trade archive (Aug-Sep).** This
   measures how many seconds the edge survives after a burst, which sets the latency
   target. It describes; it does not confirm.
3. **The realtime design's venue choice is reopened.** The signal lives on Bybit and
   Binance, not on MEXC. The owner's ability to trade Bybit perpetuals decides whether
   the fast path is built for Bybit (execution venue) with Binance as a source, or
   whether MEXC stays the canary.
