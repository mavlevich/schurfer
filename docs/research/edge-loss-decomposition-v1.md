# Where the edge is lost: detection-delay decomposition v1

Status: **registered 2026-10-04, before any analysis; design review 1 folded in;
amendment 1 (2026-10-05) supersedes the window, fee and statistics wording below;
amendment 2 (2026-10-05, after review 2) supersedes read 1.** A
bounded research result, separate from
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

- **Bybit 1-minute bars:** 2026-08-10 (capture start) to 2026-09-28, from the cold-bar
  archive and the hot table; **Binance** from its own capture start to 2026-09-28. **Gate trade archives:** July 2026.
- **No data on or after 2026-09-29.**
- **ISO weeks 36-39 (2026-08-31..09-28)** were the HYP-012b holdout and have been
  spent by the HYP-012c and HYP-029 formal reads. **The owner released them for this
  study on 2026-10-04.** So the Bybit window runs 2026-08-10..09-28, and the MEXC
  1-minute archive (2026-08-28..09-28) enters Part A and, as a secondary descriptive
  result only, Part B (MEXC costs are not measured, so it gets the cost scenarios but
  never the verdict).
- **Scanner events** of the same window, as the anchor of what the scanner actually
  saw.

## Amendment 1 (2026-10-05, before any data was read)

Locating the inputs showed that four points of the registration do not hold as written.
They are fixed here, before any bar, return or event of the window has been read.
Only coverage facts (which days exist, which files exist, row counts by exchange) were
looked at.

1. **Bybit window: 2026-08-13..2026-09-28.**
   - The cold bars of 2026-08-10, 11 and 12 are `unverifiable_legacy` and not
     admissible as formal evidence
     ([audit 2026-09-19](../engineering/audits/2026-09-19/README.md)). They are
     excluded.
   - PostgreSQL now holds bars from 2026-09-02 only. To give every day the same
     provenance, all days of the window are read from verified Borg fetches
     (`cold-bar-fetch`: the receipt's sha256 and row count).
   - Each day is then reduced on the host to the columns this study needs, with its own
     manifest. The full file is deleted after the reduction, so the host disk never
     holds more than one fetch batch.
2. **There is no measured Bybit taker fee.** The repository has no fill and no
   account-verified fee.
   - Every cost scenario uses Bybit's published base-tier taker rate for linear
     perpetuals, **5.5 bps per side**. It is labelled published, not measured.
   - Slippage stays 5, 15 and 40 bps per side, so the cost scenarios are 10.5, 20.5 and
     45.5 bps per side. The primary is 20.5 (41 bps per round trip).
   - One sensitivity result, descriptive only: a 10 bps fee with 15 bps slippage.
3. **Binance price coverage.** Binance close prices were empty until the trade-price
   source landed. Binance (secondary only) starts at the first UTC day on which at least
   99% of its bars carry `price_complete`. That is a coverage rule, fixed before
   reading prices.
4. **Statistics, made exact.**
   - **Clustering.** "Cluster bootstrap by instrument and UTC day" becomes two one-way
     cluster bootstraps: one clustered by instrument, one by UTC day. Each runs 10,000
     iterations with a seed derived from the contract hash.
   - **Verdict.** _Positive established_ needs both 95% lower bounds above zero.
     _Negative established_ needs both upper bounds below zero.
   - **Minimum detectable effect.** MDE at 80% power, two-sided 5%, is
     (1.959964 + 0.841621) x SE. SE is the larger of the two bootstrap standard
     deviations of the mean.
   - **Order of reporting.** The MDE and the firing count are computed and written
     before the mean.
5. **Gate tapes: population.** Only the three PR 3 probe bases are on disk.
   - **Population:** the bases of the scanner's Gate pump sources first seen
     2026-07-23..07-31. The scanner's records start on 07-23, and the PR 3 boundary is
     08-01.
   - **Download:** their July trade files, fetched once from the public archive to the
     local machine (not the production host). Each file's sha256 is recorded, with a cap
     of 200 bases and 5 GiB.
   - **What it shows:** a crossing is used only if the move is inside the tape.
6. **The scanner's own detection time is measured, not simulated.**
   - Part A step 4 also reports the actual lag from the bar-based crossing to the
     scanner's `first_seen_at` for the same exchange and base.
   - The uniform-phase cycle model stays as the explanation of that lag.

The MEXC 1-minute archive keeps its survivorship caveat: it holds only the symbols listed
on 2026-09-27.

## Amendment 2 (2026-10-05, after read 1, from design review 2)

Read 1 (reader `7b66074`, evidence kept under `read-1-superseded/`) ran before review 2
arrived. The review found correctness defects in the reader, so read 1 is superseded.
Read 2 runs on the same verified inputs with only these fixes. The primary contrast,
thresholds, horizons, costs and windows are unchanged.

1. **Bar quality.**
   - A bar's price is used only if all four prices are positive and the bar is
     price-complete (`price_complete`; for Bybit before that column existed, the bar's
     `complete`).
   - Turnover is used only from trade-complete bars.
   - A 5-minute return needs all six minutes t-5..t; read 1 checked only the two ends.
   - The 1-minute family needs t-1 and t, a trade-complete trigger bar, and a median
     over trade-complete bars only.
   - Entry and exit bars must be price-complete, or the firing is unresolved, with the
     reason recorded.
   - Exclusions are counted.
2. **Minimum evidence for a verdict.** At least 100 resolved firings, 20 instruments
   and 10 UTC days. Below that the primary is _not established_, whatever its interval.
3. **Entry timing.**
   - The registered entry (open of t+1) is labelled what it is: a bar-optimistic bound.
     The bar is available 2.7 s after the close, and a minute bar cannot show that the
     t+1 open was still obtainable then.
   - Every cell, the primary included, is also reported at the open of t+2: the first
     open after the data is available.
   - The verdict stays on the registered entry. Executable economics are claimed from
     neither.
4. **Resumption.** An interrupted read reuses its pinned inputs only if they are
   identical to the ones verified again. A reduced bar file counts as done only with a
   matching manifest.
5. **Gate tapes (read 1 of the Gate part is superseded too).**
   - **Prices are point-in-time.** The price at a moment is the last trade at or before
     it, with its age recorded. Read 1 took the last trade of the whole second, so it
     could use a trade up to a second later. A moment price older than 60 s is missing,
     and so is a 24h-ago reference older than 1 hour; both are counted. Lows and peaks
     come from the trades in the exact windows.
   - **Identity** comes from the scanner's stored `market_id` (no id, an identity
     conflict, a type other than `swap`, or an unexpected form are counted apart),
     never from the ticker's spelling.
6. **Scanner identities in Part A.** For Bybit, Binance and MEXC the scanner's
   `symbol` equals its stored `market_id` wherever one exists. 28 of the window's
   sources have none (3 Bybit, 8 Binance, 17 MEXC); they affect Part A's description
   only. Part B does not use scanner identities.

The reduction step on the production host now also checks the 10 GiB reserve before and
after reducing, and bounds DuckDB's memory and spill. Read 1's production run had
already completed before this fix: its log shows 17-19 GiB free throughout.

## What it decides

- **Part B positive:** early detection on a venue with measured costs can pay. That
  supports a streamed capture and weighs in the 2026-10-31 direction decision.
- **Part B negative:** stop investing in speed for this trigger family.
- **Not established:** no conclusion about speed. The decision rests on Part A's
  description and on the capture design's own merits.

Nothing here is a trading rule. A rule is registered separately and tested on an
untouched forward period.
