# Where the edge is lost: detection-delay decomposition v1

Status: **DRAFT, design review 1 folded in.** A bounded research result, separate from
any collector. No code, no read of any data on or after 2026-09-29. Nothing here
becomes a trading rule.

## Why

Every hypothesis tested so far failed after costs (HYP-012b, HYP-012c, HYP-029,
abnormal-flow v1, early momentum v4, the pump-short line). Two explanations fit that
record:

1. we arrive too late, after most of each move;
2. there is no edge at any time.

We have never measured which. The code and production show how late the current
pipelines are:

| Stage                                        | Measured                                                                      | Source                                               |
| -------------------------------------------- | ----------------------------------------------------------------------------- | ---------------------------------------------------- |
| Pump scanner rule                            | fires at a 24h change >= +20% (median 20.5%, 15,325 events 2026-08-09..09-26) | ROADMAP, `PUMP_MEASUREMENT_MIN_PCT=20`               |
| Scanner cycle length                         | median 101 s, p90 110 s (configured 60 s; the sleep starts after all work)    | production logs, 71 cycles, 2026-10-04               |
| Bybit bar available after the minute closes  | 2.7 s (p90 7.7)                                                               | `momentum_flow_watch_evaluations_1m`, 2026-09-20..27 |
| Bybit watch decision after the minute closes | 36.6 s (p90 40.5): a fixed 30 s settle plus a 10 s poll                       | same, and `due_buckets`                              |
| Paper entry after the minute closes          | 45-47 s (p90 56-62)                                                           | `momentum_flow_paper_probes`, 2026-09-15..28         |

HYP-029 already tested an earlier trigger on MEXC 5-minute bars and failed formally.

## Two parts, two different claims

### A. Descriptive decomposition of known moves (no verdict)

For the scanner's own population (instruments whose 24h change reached +20%), describe
the timeline of each move. It only describes moves that happened, so it carries **no**
claim about the economics of early signals; early triggers that were followed by
nothing are not in it.

The timestamps are kept apart, because they start from different moments:

1. **Move start** (hindsight, reference only): the last 1-minute bar before the 24h low
   that precedes the +20% crossing.
2. **Condition crossing:** the first minute bar whose close satisfies a threshold
   (+3%, +5%, +10% over 5 minutes; +20% over 24 hours).
3. **Data available:** crossing bar close plus the venue's measured bar-ready delay
   (Bybit 2.7 s; other venues reported as unknown).
4. **Detected:** for a streamed reader, at availability; for today's scanner, at its
   next cycle. The cycle phase is uniform over the measured cycle-length distribution,
   reported as a distribution, not one number.
5. **Decision and entry:** today's measured decision and entry delays where they apply.
   With minute bars the earliest executable price is the next minute open, so delays
   below a minute are quantized and reported as such.

The output is, per threshold, the share of the move (start to 24h peak) already gone at
each of those moments, with medians and quartiles.

### B. Early-trigger economics on the full denominator (one primary contrast)

Every firing of one pre-specified trigger on every instrument of the venue's captured
universe, whether or not a pump followed. This is the only part that speaks to whether early detection
could pay.

- **Primary contrast (fixed now):**
  - venue: Bybit linear USDT perpetuals (the only venue with measured costs and the
    owner's confirmed execution venue);
  - trigger: 5-minute return >= +5% on the closed minute bar;
  - side: long; entry at the next minute open; exit after 60 minutes;
  - firings: one per instrument per 60 minutes;
  - metric: mean net return under the middle cost scenario.
- **Secondary results (descriptive only, never a verdict):**
  - the other thresholds (+3%, +10%), short side, horizons 15 minutes and 4 hours;
  - Binance;
  - the 1-minute trigger family (1-minute return >= 2/3/5% with turnover >= 5x its
    60-minute median).
- **Cost scenarios** (per side, fixed now): the measured Bybit taker fee plus slippage
  of 5, 15 and 40 bps; the middle one is primary.
- **Statistics:**
  - cluster bootstrap by instrument and UTC day;
  - the minimum detectable effect at 80% power for the observed count, reported
    before the estimate.
- **Verdict on the primary contrast only:**
  - _positive established_: 95% lower bound above zero;
  - _negative established_: 95% upper bound below zero;
  - otherwise _not established_, including every under-powered case. "Not
    established" does not mean speed is useless.

### Gate tick tapes (separate analysis)

Gate's July 2026 trade archives (the PR 3 source, within its 2026-08-01 boundary) give
seconds resolution for Gate-listed instruments only. Part A's timeline is repeated there
with delays of 5, 15, 45 and 101 seconds from the crossing. This describes Gate's own
moves; it does not reconstruct MEXC execution in September.

## Data and boundaries

- **Bybit 1-minute bars:** 2026-08-10 (capture start) to 2026-08-30, from the cold-bar
  archive; **Binance** from its own capture start, where it falls before 2026-08-31. **Gate trade archives:** July 2026.
- **No data on or after 2026-09-29.**
- **ISO weeks 36-39 (2026-08-31..09-28)** were the HYP-012b holdout. They have since been
  spent by the HYP-012c and HYP-029 formal reads, but the PR 3 protocol still treated
  them as unread. They are **not used unless the research ledger explicitly releases
  them**. That excludes almost all of the MEXC 1-minute archive (2026-08-28 onward), so
  MEXC appears only in Part A's three pre-holdout days, as a note.
- **Scanner events** of the same window, as the anchor of what the scanner actually
  saw.

## What it decides

- **Part B positive:** early detection on a venue with measured costs can pay. That
  supports a streamed capture and weighs in the 2026-10-31 direction decision.
- **Part B negative:** stop investing in speed for this trigger family.
- **Not established:** no conclusion about speed. The decision rests on Part A's
  description and on the capture design's own merits.

Nothing here is a trading rule. A rule is registered separately and tested on an
untouched forward period.
