# Bybit 1-minute burst: executable cost history v1 (descriptive protocol)

Status: **design review 1 folded in (2026-10-05); registered before any order-book file
is read.**
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
  - Downloaded once, locally, each with its sha256. A 40 GiB cap (amended from 20 GiB before any read: the first fetch stopped at the cap
    with 430 of 606 files, 21.4 GB, projecting about 30 GB): everything already
    in the directory (partial files included) counts before the first download, and
    each download is checked against the cap before and while it is written. A missing
    file makes its firings `no_book`.
- **Funding:** Bybit's public funding history (`/v5/market/funding/history`) per
  instrument over its firings' span, with each response's hash, and the mark price at
  every settlement inside a hold (the open of the 1-minute mark-price candle starting
  at the settlement, `/v5/market/mark-price-kline`). Only timestamps before 2026-09-29.
  - A failed mark-price request leaves that settlement without a mark, so any hold that
    crosses it is `funding_missing`. An interrupted fetch resumes: the recorded books
    are kept only if they are this contract's and every file still matches its sha256,
    and only the funding is fetched again.
  - Missing data is never a zero. An instrument whose fetch failed, or that shows no
    settlement over a span longer than 24 hours, is `funding_missing`; so is a
    settlement inside a hold without a mark price.
- **Trade proxy:** the decay study's published mean g(d), for the comparison of levels.

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
exit moment is B + 60 min. Costs are money, normalized by the entry notional (about
USD 50), as Bybit charges them.

1. **Top of book at each entry moment:** the half-spread in bps of mid.
2. **Executable entry:** walk the asks for USD 50 of notional, giving the quantity q and
   the entry notional N_e. The impact is in bps over mid. Not fillable within 200
   levels counts as `depth_short`.
3. **Executable exit:** walk the bids for the same q at B + 60 min, giving the exit
   notional N_x.
4. **Fees:** 5.5 bps (Bybit's published taker rate) on N_e and on N_x.
5. **Funding paid by a long:** the sum over settlements inside the hold of q x mark
   price x rate.
6. **Round-trip cost:** entry and exit impact in money, plus fees and funding, over N_e.
7. **Net executable proxy:** (N_x - N_e - fees - funding) / N_e, in bps.
8. **Against the trade proxy:** the mean gross executable proxy (N_x / N_e - 1) beside
   the decay study's mean g_trade(d), from its published result (sha256
   `30356acb870a...`). That result holds aggregates only, so the two means can cover
   slightly different resolved sets. This is a comparison of levels, not a paired
   difference.

**Reported per entry moment:**

- counts of each status;
- mean, median and quartiles of the half-spread, the entry and exit impact, the
  round-trip cost and the net proxy;
- the share of firings whose round-trip cost exceeds 41 bps;
- for d = 2.7 and 5 s, the mean net proxy with 95% cluster bootstrap intervals by
  instrument and by UTC day (10,000 iterations, fixed seed);
- the share of the net from the five largest instruments.

## What it decides

A decision needs enough evidence at the 5 s entry, fixed now:

- at least 300 resolved firings and at least half of the 732;
- at least 50 instruments;
- at least 20 UTC days.

Below any of these the result is **insufficient data** and decides nothing.

- **Enough evidence, and either the median round-trip cost at d = 5 s is above 41 bps
  or both intervals of the mean net at d = 5 s lie below zero:** HYP-030 is parked in
  the discovery ledger. The sealed measurement still runs to its end, but no forward
  test is designed.
- **Otherwise:** nothing is decided here. The window is the one the cell was found in.
  The sealed measurement stays the independent check, and any forward test still
  needs its own registration on an untouched window.

## Not in this study

- Fills, queue position, maker entry, adverse selection beyond the quoted book.
- Sizes above USD 50.
- Binance, and any other trigger.
