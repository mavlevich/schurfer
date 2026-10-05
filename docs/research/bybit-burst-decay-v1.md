# Bybit 1-minute burst: seconds-level decay v1 (descriptive protocol)

Status: **draft for design review; registered before any trade file is read.** A
descriptive study. It confirms nothing and creates no trading rule.

## Why

The edge-loss readout found, exploratorily, that for 1-minute bursts on Bybit (1-minute
return of at least +5%, turnover of at least 5x the prior-60-minute median) most of the
gross return to a fixed exit sits in the first minute after the burst bar. On the same
firings, entering at the t+2 open instead of the t+1 open loses 79 bps (95% CI 49..108
by instrument).

Minute bars cannot say whether any of that is reachable. The t+1 open is the first
trade after the bar ends; a bar reader sees the bar 2.7 s later. This study measures,
from Bybit's own trades, how the move decays over the first seconds. That bounds the
latency a fast path would need, or shows that none is enough.

What it cannot say, and does not claim: executable economics. That needs quotes, costs
and size; none are here.

## Population (fixed now)

- **Firings:** exactly the paired exploratory set of the edge-loss readout.
  - Bybit linear, 2026-08-13..09-28, read-2 bar rules;
  - 1-minute return of at least +5% on price-complete bars;
  - trade-complete trigger bar, turnover of at least 5x the median of the trade-complete
    bars of the prior 60 minutes (at least 30 of them);
  - one firing per instrument per 60 minutes;
  - t+1, t+2, t+3, t+6 and t+61 opens all present.
- **Expected count:** 732 firings. The list (instrument, bar start) is frozen to a file
  with its sha256 before any trade file is downloaded.
- **No data on or after 2026-09-29.** Every firing's exit is before it.

## Data

- **Source:** Bybit's public trade archive,
  `https://public.bybit.com/trading/{SYMBOL}/{SYMBOL}{YYYY-MM-DD}.csv.gz`.
  - Columns: `timestamp` (seconds, 0.1 ms resolution), `symbol`, `side`, `size`,
    `price`, `tickDirection`, `trdMatchID` (a UUID, not a sequence), notionals, `RPI`.
- **Files:** only the instrument-days that contain a firing or its exit (at most two
  days per firing).
  - Downloaded once to the local machine, never to the production host.
  - Each file's sha256 is recorded; the cap is 20 GiB.
  - A missing file makes its firings `no_tape`.
- **Order:** trades keep the file's row order. With no sequence id, ties at the same
  timestamp are ordered by row, never by price.
- **Consistency check, reported first:** for each firing, the first trade at or after
  the t+1 boundary against the bar file's t+1 open. Disagreements are counted.
  - A firing whose bar open has no matching trade within 1 s of the boundary is
    flagged.
  - No firing is dropped on this basis.

## Measures (fixed now)

The bar's end is B. The exit price X is the last trade at or before B + 60 min, the
same fixed exit as the paired readout (the t+61 open's neighbourhood).

1. **Decay curve.** For delays d of 0, 0.25, 0.5, 1, 2, 2.7, 5, 10, 20, 30, 46 and
   60 s:
   - P(d) is the last trade at or before B + d, with its age;
   - g(d) = X / P(d) - 1, in bps.

   Reported per d: count, mean, median and quartiles, and the share of firings whose
   P(d) is older than 5 s (stale).

2. **Paired loss against the first trade after B.** For each d, the mean of
   g(first trade after B) - g(d) over the firings where both exist, with 95% cluster
   bootstrap intervals by instrument and by UTC day (10,000 iterations, fixed seed).
3. **Intrabar detection (secondary).** The earliest trade inside the burst minute at
   which a streaming reader would already see the bar's conditions met:
   - the price at least +5% above the previous bar's close;
   - the minute's turnover so far at least 5x the same prior median.

   Reported:
   - how many seconds before B that moment comes (the share of firings with such a
     moment);
   - g from that moment's price;
   - the paired difference against g at B.

   This describes an opportunity that bars cannot show. It is not the bar rule.

4. **Concentration.**
   - The share of the paired loss at d = 2.7 s from the five largest instruments.
   - The ISO-week means of the same quantity.

## What it decides

- **If the paired loss at 2.7 s and 46 s is close to the full 79 bps:** the first-minute
  move is gone before any bar reader can act. Only an intrabar trade reader could
  matter, and measure 3 then decides whether it is worth a forward test.
- **If a material part survives several seconds:** a forward test of a registered rule
  (HYP-030) becomes worth designing, with its entry delay set to a measured latency,
  real-quote costs and a sealed accrual read after HYP-012 v2 is terminal.
- **In neither case** does this study change the architecture by itself; it supplies the
  latency requirement.

## Out of scope

- Binance and the route "Binance signal, Bybit execution".
- Quotes, slippage and size.
- Any other trigger family.
