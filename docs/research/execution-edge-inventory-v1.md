# Execution edge: limit entry with a fixed wait against a taker entry (inventory v1)

Status: **planning note, 2026-11-09; decides nothing, registers nothing, reads no new
data.** The colleague's direction for the strategy's fallback after the
[BTC trend note](btc-trend-feasibility-v1.md): does a limit entry with a fixed wait lower
the full cost of execution against an immediate taker entry, on the same decisions fixed
in advance? This note is the first step the colleague asked for: which recorded decisions,
order book and tape exist, whether both policies can be replayed on each decision, and
the MDE in bps per decision. Everything here comes from the repository's documents; no
database, archive or return was read.

## The comparison can be measured without the signal's return

Two policies on each decision (a buy shown; a sell mirrors it):

- **Taker:** buy at the ask at the decision time t (plus the declared latency), taker fee.
- **Limit with a fixed wait W:** post at the best bid at t; whatever fills by t + W (by
  the fill model below) pays the maker fee at the bid; whatever is still open at t + W
  is bought by taker at the ask then.

Both hold the same size to the same exit, so the difference of their results is only the
difference of their **entry costs**: fees, the price paid, and the move between t and
t + W on the part that was not filled. Partial fills are inside this, and no decision
drops out, so it **needs no return of the signal** and does not read one. A variant that
**skips** the unfilled part (the colleague's "misses stay in the denominator") also needs
the signal's return on the missed part; it is a second variant, readable only for a
signal whose return is itself known.

Adverse selection is the point of the test: on a momentum decision the price tends to
leave the bid, so the limit fills least often exactly when the taker entry would have
been best. That is why it must be measured on a strategy's own decisions, not on random
times.

## Decisions on record

| Source                                                        | Decisions (from the docs)                                                                 | Window status                                                                                 | Use                                                             |
| ------------------------------------------------------------- | ----------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| **HYP-015 WATCH** (momentum flow, Bybit), 2026-08-14 to 09-28 | about 172 a day by the registration's 48 h checkpoint, so about **7,700** (to be counted) | sealed until its 2026-11-04 read; whether that read has run is not recorded in the repository | the only source with volume                                     |
| HYP-015 WATCH from 2026-09-29                                 | the same flow                                                                             | inside HYP-012 v2's blind window until v2 is terminal                                         | confirmation later, not now                                     |
| HYP-012 v2 forward cohort                                     | small: its 10-31 checkpoint needed only 12 to continue                                    | blind window                                                                                  | too few                                                         |
| Pump-short maker-entry cohort, 2026-08-24 to 09-28            | episode counts read (coverage)                                                            | coverage only                                                                                 | its own maker report exists, but the pump-short line is stopped |
| LIVE_PROBE                                                    | none (not deployed)                                                                       |                                                                                               | none                                                            |
| Rules defined on public history (HYP-032 to HYP-036 style)    | any number                                                                                | holdout and discovery rules apply                                                             | possible, but none of those signals survived                    |

## Book and tape for the replay

- **Bybit public order book archive** (`ob500` and `ob200` daily files, snapshots and
  deltas, delisted symbols included) and the **trade archive** (every trade with its
  time, side and size) exist for any day from 2023, including August and September 2026.
  Together they give the book at t and every trade until t + W, which the fill model
  needs.
- **Fill model:** strategy v2's default, declared before any read: the order joins the
  back of the visible quantity at its price and fills only after that quantity has
  traded through (volume-limited primary, the looser model a declared sensitivity). Our
  own place in the queue is never observed, so the model's assumption is the largest
  single uncertainty.
- **Own captures:** the momentum bars hold the last bid and ask per minute (too coarse for
  a queue); the Binance book ticker is another venue. Neither replaces the archive.
- **Size:** order book files are large (HYP-030's cost history read 606 files, 30.4 GB);
  the scope is the symbol-days that carry decisions, sized by listing and HEAD before any
  fetch.

## Detectable effect

The spread of the per-decision cost difference is not known before a read. Scenario:
15, 30 or 60 bps (a W of about a minute on liquid alts, where most of the spread is the
unfilled part's move). With the HYP-015 confirmation note's rule (MDE = 2.80 SD root of
(deff / n)):

| n                 | SD 15 bps, deff 1 / 3 / 10 | SD 30 bps       | SD 60 bps        |
| ----------------- | -------------------------- | --------------- | ---------------- |
| 7,700 (all)       | 0.5 / 0.8 / 1.5            | 1.0 / 1.7 / 3.0 | 1.9 / 3.3 / 6.0  |
| 1,900 (a quarter) | 1.0 / 1.7 / 3.0            | 1.9 / 3.3 / 6.0 | 3.8 / 6.6 / 12.1 |

The saving at stake is the fee gap (taker about 5.5 against maker about 2 bps on the
perpetual) plus part of the half-spread, a few to about 10 bps a decision before adverse
selection. **Power is not the binding problem here**, unlike every family before it.

## What binds

1. **Which signal.** A cheaper entry is worth something only for a signal that pays. On
   record, only HYP-015 has the volume, and its value depends on its 11-04 read. If that
   read fails, this test measures the execution of a signal that will not trade.
2. **Which window.** August and September 2026 is the most reused own-capture period
   (outcomes level for HYP-011 to HYP-030); for HYP-015's decisions it is discovery at
   best. Confirmation needs decisions after HYP-012 v2 is terminal.
3. **The fill model.** With the queue unobserved, a result is only as good as the
   declared model; a live cost probe (strategy v2, step 1 of the live path) is the only
   real check.

## Position and next steps

My position: the execution test is feasible and powered, but it should be tied to
HYP-015 and wait on its 11-04 read. Next, in order, each small:

1. **The owner states HYP-015's 11-04 read status** (run or not, and its verdict if
   run). If HYP-015 failed, this direction pauses with it.
2. **Count the WATCH decisions** of 2026-08-14 to 09-28 per day and symbol (counts only,
   on production, coverage level).
3. **Size the archive scope** (order book and trade files for those symbol-days, by
   listing and HEAD, no download).
4. Then a protocol: W, the fill model, the primary metric (mean entry-cost difference
   per decision, cluster-robust by symbol), the variant with skips, and the fetch cap.

Alternative: a signal-free calibration on random liquid-symbol times first (fill rates,
queue model, cost spread), which needs no decision source but does not measure adverse
selection, the effect that decides the question.
