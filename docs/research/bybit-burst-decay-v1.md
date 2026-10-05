# Bybit 1-minute burst: seconds-level decay v1 (descriptive protocol)

Status: **design reviews 1 and 2 folded in (2026-10-05); registered before any trade file is
read.** A
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
- **Provenance:** the list is built only from reduced bars that match their manifests
  and equal the bars pinned by the read-2 `inputs.json` (sha256 recorded). It records
  the code revision (a clean checkout) and the contract hash.
- **Count:** any count other than the readout's 732 is refused.
- **Freezing:** the list (instrument, bar start, previous close, prior turnover median,
  t+1 and t+61 opens) is frozen to a file with its sha256 before any trade file is
  downloaded. Every later phase checks the contract hash and the list's sha256.
- **No data on or after 2026-09-29.** Every firing's exit is before it.

## Data

- **Source:** Bybit's public trade archive,
  `https://public.bybit.com/trading/{SYMBOL}/{SYMBOL}{YYYY-MM-DD}.csv.gz`.
  - Columns: `timestamp` (seconds, 0.1 ms resolution), `symbol`, `side`, `size`,
    `price`, `tickDirection`, `trdMatchID` (a UUID, not a sequence), notionals, `RPI`.
- **Files:** only the instrument-days that contain a firing, its exit, or the exit's
  wait.
  - Downloaded once to the local machine, never to the production host.
  - Each file's sha256 is recorded.
  - The cap is 20 GiB, counting files already on disk.
  - A missing file makes its firings `no_tape`.
  - Each file is hashed and parsed once per instrument. Only the firings' windows (the
    burst minute to the exit wait, about 62 minutes each) are kept in memory, and the
    result records the read's peak resident memory.
- **Reader revision:** the read runs only from a clean checkout of an explicit commit.
  The result records it as `reader_code_revision`, apart from the revision that froze
  the firing list.
- **Order:** trades keep the file's row order. With no sequence id, ties at the same
  timestamp are ordered by row, never by price.

## Measures (fixed now)

B is the burst bar's end.

- **Exit X:** the first trade at or after B + 60 min. This is the t+61 open of the
  paired readout. If there is no trade within 60 s, the firing is `exit_unavailable`;
  it is counted and never resolved from an older price.
- **Entry proxy E(d):** the first trade at or after B + d, with a wait of at most 2 s.
  Without one, the moment is missing and counted.
- **Prices are proxies.** Both are trade prices, not fills; nothing here is executable
  economics.

1. **Consistency checks (reported first):**
   - E(0) against the bar file's t+1 open, and X against the t+61 open: mismatches
     are counted;
   - the waits of E(0) and X.

   No firing is dropped on this basis.

2. **Decay curve, primary.** For d in 0, 0.25, 0.5, 1, 2, 2.7, 5, 10, 20, 30, 46 and
   60 s, g(d) = X / E(d) - 1 in bps. Reported per d: missing count, mean, median,
   quartiles and the share of waits over 5 s.
3. **Last known price, secondary.** L(d) is the last trade at or before B + d, with
   its age, and g_L(d) = X / L(d) - 1. Just after B, L(d) can still be a trade inside
   the burst bar. It describes the last known price; it does not set a latency
   requirement.
4. **Paired loss.** For d of 2.7 s and 46 s, the mean of g(0) - g(d) over firings where
   both exist. 95% cluster bootstrap intervals by instrument and by UTC day (10,000
   iterations, fixed seed).
5. **Intrabar detection (secondary, conditional).** The earliest trade inside the
   burst minute at which a streaming reader would already see the bar's conditions
   met:
   - the price at least +5% above the previous bar's close;
   - the minute's turnover so far at least 5x the same prior median.

   Reported:
   - how many seconds before B that moment comes (the share of firings with one);
   - g from that trade;
   - the paired difference against g(0).

   This is **conditional on the bar ending as a burst.** It does not estimate a
   streaming rule over all its triggers: intrabar crossings whose bar then ended below
   the threshold are not in this population.

6. **Concentration.**
   - The share of the paired loss at 2.7 s from the five largest instruments.
   - The ISO-week means of the same quantity.

## What it decides

- **If the paired loss at 2.7 s and 46 s is close to the full minute's 79 bps** (the
  t+1 versus t+2 open loss on the same firings; the entry and exit here match those two
  opens, and the checks report any mismatch): the first-minute move is gone before any
  bar reader can act. Only an intrabar trade reader could matter, and measure 5 (with
  its conditionality) then decides whether a forward test of such a reader is worth
  designing.
- **If a material part survives several seconds:** a forward test of a registered rule
  (HYP-030) becomes worth designing, with its entry delay set to a measured latency,
  real-quote costs and a sealed accrual read after HYP-012 v2 is terminal.
- **In neither case** does this study change the architecture by itself; it supplies the
  latency requirement.

## Out of scope

- Binance and the route "Binance signal, Bybit execution".
- Quotes, slippage and size: what can be earned from any surviving move needs them.
- Any other trigger family.
