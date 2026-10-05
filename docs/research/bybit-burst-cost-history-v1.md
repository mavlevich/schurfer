# Bybit 1-minute burst: executable cost history v1 (descriptive protocol)

Status: **draft for design review; registered before any order-book file is read.**
Descriptive, on data before 2026-09-29 only. It creates no trading rule and does not
replace the sealed forward measurement (HYP-030 path measurement, running since
2026-10-05).

## Why

The burst decay readout and the HYP-030 design left one unknown that decides whether
the 1-minute burst can make money: what entering and exiting actually cost at those
moments. The middle cost scenario (41 bps per round trip) is an assumption. The sealed
measurement will tell, but only after HYP-012 v2 is terminal.

Bybit publishes its own historical order books (200 levels, a snapshot then deltas,
millisecond exchange times, one file per instrument per day). For the 732 historical
firings that gives the quoted cost at each moment now, on data that is already
readable. If those costs are far above the scenario, HYP-030 is parked without waiting.

## Population

The 732 firings frozen by the decay study
(`evidence/bybit-burst-decay-v1/decay-firings.json`, sha256 `cb3f8154bb43...`),
unchanged: Bybit linear, 2026-08-13..09-28, read-2 rules. The list is verified against
its sha256 before anything is downloaded. **No data on or after 2026-09-29.**

## Data

- **Order books:**
  `https://quote-saver.bycsi.com/orderbook/linear/{SYMBOL}/{DAY}_{SYMBOL}_ob200.data.zip`.
  - JSON lines in the public stream's own format: `type` snapshot or delta,
    exchange time `ts` (ms), update id `u`, sequence `seq`, levels `b` and `a`.
  - Only the instrument-days that hold a firing's entry moments and its exit.
  - Downloaded once, locally, each with its sha256; a 20 GiB cap that counts files
    already on disk. A missing file makes its firings `no_book`.
- **Funding:** Bybit's public funding history (`/v5/market/funding/history`) for the
  settlements inside each hold, recorded with the request and its response hash. Only
  timestamps before 2026-09-29.
- **Trade proxy:** the decay study's g(d) for the same firings, for the comparison.

## Book reconstruction

- Messages are applied in file order. A snapshot replaces the book; a delta sets or
  removes levels.
- A delta whose update id does not follow the previous one, or a delta before any
  snapshot, marks the book broken until the next snapshot. Broken spans are counted,
  and a moment inside one is `book_broken`.
- The book at a moment τ is the state after the last message with `ts` at or before
  τ. Its age is τ minus that `ts`; a book older than 5 s at τ is `book_stale`.
- The book is never read ahead of τ.

## Measures (fixed now)

B is the burst bar's end. Entry moments are d = 0, 2.7, 5, 10 and 46 s after B; the
exit moment is B + 60 min.

1. **Top of book at each entry moment:** the half-spread in bps of mid.
2. **Executable entry:** the volume-weighted ask price for USD 50 of notional; impact
   in bps over mid. Not fillable within 200 levels counts as `depth_short`.
3. **Executable exit:** the volume-weighted bid price for the same base quantity at
   B + 60 min.
4. **Round-trip quoted cost** at entry moment d: entry impact over mid, plus exit impact
   under mid, plus 2 x 5.5 bps (Bybit's published taker fee), plus the settled funding
   over the hold. Long positions pay positive funding.
5. **Net executable proxy:** exit VWAP / entry VWAP - 1 - fees - funding, in bps.
6. **Gap to the trade proxy:** g_trade(d) from the decay study minus the gross
   executable proxy, on the same firings.

**Reported per entry moment:**

- counts of each status;
- mean, median and quartiles of the half-spread, the entry and exit impact, the
  round-trip cost and the net proxy;
- the share of firings whose round-trip cost exceeds 41 bps;
- for d = 2.7 and 5 s, the mean net proxy with 95% cluster bootstrap intervals by
  instrument and by UTC day (10,000 iterations, fixed seed);
- the share of the net from the five largest instruments.

## What it decides

- **The median round-trip cost at d = 5 s is above 41 bps, or both intervals of the
  mean net at d = 5 s lie below zero:** HYP-030 is parked in the discovery ledger. The
  sealed measurement still runs to its end, but no forward test is designed.
- **Otherwise:** nothing is decided here. The window is the one the cell was found in.
  The sealed measurement stays the independent check, and any forward test still
  needs its own registration on an untouched window.

## Not in this study

- Fills, queue position, maker entry, adverse selection beyond the quoted book.
- Sizes above USD 50.
- Binance, and any other trigger.
