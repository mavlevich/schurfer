# HYP-029: MEXC early trigger entered on Bybit, September 1m re-check (v1)

Status: REGISTERED 2026-09-28, before any September return was read.

- **What had been read.** The MEXC 1m bars for 2026-08-29..09-28 were archived (PR #462) and
  checked only for completeness and hashes. No price path, return or trigger had been computed
  on them.
- **Timing.** The HYP-012c holdout read runs at 2026-09-29 00:30Z. This registration merges
  before it, so its result cannot shape this rule.

Code: `mexc_early_trigger_hyp029.py` (CLI `mexc-early-trigger-hyp029`).

## Why

The August 5m exploration on burnt data (`mexc_early_trigger_screen`, PR #462) found a
dose-response on the Bybit leg:

| MEXC 5m trigger | Bybit 1h gross |
| --------------- | -------------- |
| >=3%            | +0.20%         |
| >=5%            | +1.65%         |
| >=8%            | +3.45%         |

For the >=8% cell, the net was +3.25% at 0.2% cost [+0.03, +6.47], the median +1.59%, n=65.
That cell was the best of three thresholds and four horizons viewed, so its interval
overstates confidence. This study tests that one rule on new data with a more realistic entry,
and nothing else.

## The rule (fixed before the read)

- **Trigger.** A 5m bar built from five MEXC 1m bars, aligned to 5-minute boundaries, all five
  present.
  - Return (close / open - 1) at least 8%.
  - Turnover at least 5x the median 5m turnover of the prior 24h, and at least $20,000.
  - A prior 24h change (bar open versus 24h earlier) below 10%.
  - At most one trigger per symbol per 24h.
  - Window: bars from 2026-09-01T00:00Z. Every exit bar closes before 2026-09-29T00:00Z.
- **Route, known at the signal.** Exactly one Bybit USDT linear perpetual for the base
  trading at the entry. The catalogue is fetched with every status, so delisted contracts are
  included. The entry price must be within 2x of the MEXC trigger close.
  - Triggers without a route are counted, not evaluated.
  - A later delisting is not a selection criterion. A contract delisted during the hold stays
    in the routed denominator as the unresolved status `delisted_during_hold`.
- **Entry and exit.**
  - Entry at the open of the Bybit 1m bar that starts 60 s after the MEXC bar closes. This is
    more conservative than the exploration's exact-close entry.
  - Exit at the close of the Bybit 1m bar 59 minutes later: a 60-minute hold.
  - A 120 s entry is reported as a sensitivity only.
- **Costs.** The primary cost is 0.4% per trade (not yet measured); 0.2% is reported.

## Test and verdict

- **The test.** Pooled mean net at the primary cost, with an asset-clustered bootstrap
  (10,000 iterations, seed 20260928).
- **Floor.** At least 30 legs, 20 assets, and no ISO week above 45%. Unresolved routed legs
  must be at most 10%. A missing entry or exit bar after a successful response, or a
  delisting during the hold, counts as unresolved.
- **Verdict**, in this order:
  1. `insufficient_data` when there is no mean;
  2. `fail` at 30 or more legs with a mean at or below zero, even without an interval;
  3. `insufficient_data` without an interval, below the floor, or over the unresolved ceiling;
  4. `candidate` when both the mean and the interval's lower bound are above zero;
  5. otherwise `fail`.
- **Reported, never used to choose.** Gross mean and median, the win rate, the 120 s
  sensitivity, the top five symbols by contribution, and the mean without the top symbol.

## Protocol

1. **`prepare`**, from 2026-09-29T00:00Z. It verifies the archive (every file `complete` or
   `empty`, every sha256 matching the manifest), builds the triggers from MEXC bars only, and
   freezes the Bybit catalogue and each routed leg's Bybit 1m bars. No return is computed.
   - Bybit requests retry on HTTP errors, 429/5xx and API error codes. When the retries are
     exhausted, prepare aborts before the inputs are published, so a transient failure can
     never be frozen as a gap.
   - The inputs are written once with their sha256.
2. **`read`**, from 2026-09-29T01:00Z. It takes the claim, which pins the inputs, the contract
   digest and the reader's revision, and computes the verdict from the stored inputs only.
   A crashed read resumes only on the same pins; a completed read is never repeated.

Both phases run only from a clean checkout. `--code-revision` is required and must equal
`HEAD`, so the claim never pins an unknown revision.

## What a verdict allows

- **`candidate`.** Only the next registered step: a bounded MEXC websocket canary and an
  untouched forward cohort, with execution costs measured from real books and the real signal
  latency. It never allows an order.
- **`fail`.** Closes minute-scale MEXC early detection with this rule. The threshold is not
  re-tuned on September.
- **`insufficient_data`.** Leaves only the forward cohort as the test.

## Known limits

- **Overlap in time with HYP-012c.** September overlaps the HYP-012c holdout in time and
  partly in assets. The rules differ (early MEXC trigger versus a price gap at a +20% scanner
  signal), and this registration precedes that read.
- **Survivorship.** The archive holds MEXC symbols listed on 2026-09-27; symbols delisted
  earlier in September are absent.
- **Candle entry.** A Bybit bar open is not a fill from the order book. Measured costs and
  the forward cohort address this.
