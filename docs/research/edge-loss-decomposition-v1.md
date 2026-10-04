# Where the edge is lost: detection-delay decomposition v1

Status: **DRAFT for design review.** No code, no read of any data on or after
2026-09-29. A feasibility ceiling, never a strategy: no rule is chosen from it.

## Why

Every hypothesis tested so far failed after costs (HYP-012b, HYP-012c, HYP-029,
abnormal-flow v1, early momentum v4, the pump-short line). Two diagnoses fit that record
and point opposite ways:

1. **We arrive late.** Moves exist and cover costs, but most of each move is gone
   before we see it and can enter. Then earlier data, faster detection and the venue
   where the move starts are worth building.
2. **There is no edge even at a perfect time.** Then speed and new venues will not
   help, and effort belongs to another mechanism (for example spot/perp carry).

We have never measured which. What the code and production show today:

| Stage                                        | Measured                                                                         | Source                                               |
| -------------------------------------------- | -------------------------------------------------------------------------------- | ---------------------------------------------------- |
| Pump scanner trigger                         | 24h change >= +20% (median detection 20.5%, all 15,325 events 2026-08-09..09-26) | ROADMAP, `PUMP_MEASUREMENT_MIN_PCT=20`               |
| Scanner cycle                                | median 101 s, p90 110 s (configured 60 s; the sleep starts after all work)       | production logs, 71 cycles on 2026-10-04             |
| Exchange spread inside a cycle               | last venue (Gate) 17 s after the first; MEXC 7.5 s                               | same logs                                            |
| Ticker freshness accepted                    | up to 15 minutes                                                                 | `scanner.MAX_TICKER_AGE_MS`                          |
| Bybit momentum: bar ready after minute close | 2.7 s (p90 7.7)                                                                  | `momentum_flow_watch_evaluations_1m`, 2026-09-20..27 |
| Bybit momentum: decision after minute close  | 36.6 s (p90 40.5): a fixed 30 s settle in `due_buckets` plus a 10 s poll         | same, and code                                       |
| Paper claim after decision                   | 8-11 s (p90 18-29)                                                               | `momentum_flow_paper_probes`, 2026-09-15..28         |
| Paper entry after minute close               | 45-47 s (p90 56-62)                                                              | same                                                 |

HYP-029 already tried earlier detection on MEXC 5-minute bars and failed formally
(gross +1.26%, median about 0, net CI spanning 0), so "earlier at minute resolution"
has some evidence against it. Seconds-level timing on the venue where the move starts
is untested.

## Question

For moves of the scanner's population, how much after-cost return remains as a
function of **when** we would have entered, from the true start of the move to the
scanner's +20% detection plus our measured pipeline delays?

## Data (all before 2026-09-29)

- **MEXC 1-minute contract bars**, 2026-08-28..09-28, from the pinned
  `mexc_kline_archive` manifest. The HYP-012b holdout weeks 36-39 inside it were
  reserved until the HYP-012c read, which happened on 2026-09-29; they are readable
  now under the registered rule.
- **Bybit and Binance 1-minute bars** of the same window (hot table and cold-bar
  archive) for cross-venue timing.
- **Scanner events** (`app.pump_events` first-seen times and detection percentages) of
  the same window, as the "what we actually saw" anchor.
- **Gate trade archives** for July 2026 (the PR 3 source, tick-level, within its
  2026-08-01 boundary): the only historical tape with seconds resolution, used to
  measure delays below one minute on Gate-listed moves.
- **Costs**: the measured pre-blind book-cost baseline for Bybit and Binance; MEXC fees
  and spreads are not measured yet and enter as registered scenarios (taker fee and
  three spread levels), reported separately.

## Fixed before any result is looked at

- **Population:** every venue-instrument whose 24h change reaches +20% in the window
  (the scanner's own population), deduplicated per asset and day.
- **Move start (reference only, hindsight):** the last 1-minute bar before the 24h low
  that precedes the +20% crossing. It marks how much move existed; it is never a
  trigger.
- **Point-in-time triggers** (computable at their own time, no look-ahead):
  - T0: the scanner rule (24h >= +20%) at its measured cadence;
  - T1: 5-minute return >= 3%, 5% or 10%;
  - T2: 1-minute return >= 2%, 3% or 5% with 1-minute turnover >= 5x its 60-minute
    median.
- **Entry delays after a trigger:** 5 s (streamed detection; Gate ticks only), 45 s
  (today's momentum pipeline), 101 s (today's scanner cycle), next 1-minute open.
- **Directions:** long and short, each reported.
- **Horizons:** 5, 15, 60 and 240 minutes and 24 hours.
- **Statistics:** mean and median net return, cluster bootstrap by asset and UTC day,
  count of events; split-half by calendar week as a stability check.
- **Decision rule (registered before the read):** earlier detection is worth building
  when, for some trigger, the net mean at a delay we can actually reach (>= 5 s
  streamed) has a cluster-bootstrap 95% lower bound above zero in both halves, under
  the mid cost scenario. Otherwise the line "detect earlier" stops, and the report says
  so.

## Output

A table and a curve: remaining after-cost return against trigger and delay, per venue
and direction, with the share of the move already gone at each point. Plus the scanner
and momentum pipeline delays above as reference points on the same curve.

## Integrity

- A ceiling measured in hindsight is not a strategy. No threshold from this report may
  be used as a trading rule without a separate registration and an untouched forward
  test (the MEXC shadow capture below provides the forward data, readable only after
  HYP-012 v2 reaches a terminal state).
- No data on or after 2026-09-29 is read. Gate tapes stay within the PR 3 boundary.
- All parameters above are fixed in this document before the analysis runs; a change
  needs a new version.

## What it decides

- **Positive:** the realtime capture below is justified as an investment, and the
  2026-10-31 direction decision weighs MEXC (where moves start, and where the owner can
  trade futures) against Gate.
- **Negative:** we stop investing in speed for this event type and move the free slot
  to spot/perp carry feasibility.
