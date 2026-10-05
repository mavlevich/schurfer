# Bybit 1-minute burst: seconds-level decay v1, readout

Protocol: [bybit-burst-decay-v1](bybit-burst-decay-v1.md) (design reviews 1 and 2).
Read once on 2026-10-05; firings frozen and read at `dc63f4d` from a clean checkout.
Descriptive only: every price is a trade-price proxy, not a fill, and nothing here is
executable economics.

**Evidence** ([bybit-burst-decay-v1](evidence/bybit-burst-decay-v1/)):

| File                 | sha256            | What                                                        |
| -------------------- | ----------------- | ----------------------------------------------------------- |
| `decay-firings.json` | `cb3f8154bb43...` | the 732 firings; bars verified against read 2's pins        |
| `decay-tapes.json`   | `40571a69ecc1...` | 606 Bybit trade files (282 instruments, 13 GB), all present |
| `decay-result.json`  | `30356acb870a...` | the result; peak memory 2.9 GB                              |

## Checks first

- **Statuses.** 726 of 732 firings are `ok`; 4 `exit_unavailable` and 2
  `entry_unavailable` (no trade within the wait).
- **The bar opens are not first trades.**
  - The first trade at or after B differs from the bar file's t+1 open in 563 firings,
    and the exit trade from the t+61 open in 509.
  - Cause: Bybit capture bars take OHLC from the ticker's `lastPrice` in arrival order
    ([momentum-trade-price-source-v1](momentum-trade-price-source-v1.md)), not from
    trades.
  - A diagnostic after the read
    ([script](evidence/bybit-burst-decay-v1/diagnostic_open_vs_first_trade.py.txt),
    [output](evidence/bybit-burst-decay-v1/diagnostic-open-vs-first-trade.json)) puts
    the gap at: entry median 0 bps (absolute median 6.4, 5-95% -27..+28); exit median
    0 bps (absolute median 2.7).
  - On these firings, the mean returns of the two proxies are close: +125.4 bps from
    the bar opens and +125.0 bps from the trades. This does not test whether ticker
    prices affected which bars were selected as bursts, or the edge-loss readout's
    delay comparison built on the bar opens.
- **Waits.** The median wait is 0.06 s for the entry and 0.29 s for the exit. No moment
  waited over 5 s.

## Decay of the trade-price proxy after the burst bar

g(d) = exit / first trade at or after B + d, minus 1. The exit is the first trade at or
after B + 60 min.

| Delay after B | Firings | Mean g   | Share of g(0) | Median g |
| ------------- | ------- | -------- | ------------- | -------- |
| 0 s           | 726     | +125 bps | 1.00          | +50 bps  |
| 1 s           | 721     | +120 bps | 0.96          | +46 bps  |
| 2.7 s         | 707     | +106 bps | 0.85          | +33 bps  |
| 5 s           | 710     | +95 bps  | 0.76          | +27 bps  |
| 10 s          | 714     | +75 bps  | 0.60          | +8 bps   |
| 20 s          | 709     | +62 bps  | 0.50          | 0 bps    |
| 46 s          | 687     | +36 bps  | 0.29          | -21 bps  |

**Paired loss against the first trade after B** (same firings, same exit):

| Delay | Firings | Mean loss | 95% CI by instrument | 95% CI by UTC day |
| ----- | ------- | --------- | -------------------- | ----------------- |
| 2.7 s | 707     | 16.5 bps  | 7.6 .. 25.7          | -6.1 .. 37.6      |
| 46 s  | 687     | 85.0 bps  | 58.1 .. 114.0        | 25.4 .. 137.1     |

The last known price before each moment (secondary) tells the same story: a mean of
+110 bps at 2.7 s and +44 bps at 46 s.

**Dispersion is extreme.** The quartiles of g(0) are -448 and +472 bps; a few large
moves carry the mean. At 2.7 s:

- 42% of the paired loss comes from five instruments;
- the ISO-week means of the loss range from -22 to +62 bps.

## Intrabar detection (conditional; not a rule estimate)

All 726 bars had a moment inside the minute when both conditions were already met, by
construction: these bars ended as bursts. The median moment is 26.5 s before B. The
proxy from there to the exit is +210 bps (median), +255 bps more than from B on
average.

This is conditional on knowing that the bar ends as a burst, which a streaming reader
does not know. It does not estimate a streaming rule over all its triggers. Those whose
bar ended below the threshold are not in this population.

## What it means (descriptive)

- **The first-minute move decays over seconds to tens of seconds, not instantly.**
  - About 85% of the mean proxy is still there 2.7 s after the bar's end (when a bar
    reader has the bar), 76% at 5 s, half at 20 s, under a third at 46 s.
  - Today's watch path enters about 46 s after the bar closes: it is in the part where
    most is gone, and its median is negative.
- **Latency target (protocol's second case), not yet confirmed.** A material part of
  the proxy survives several seconds, so acting within about 3 to 5 s of the bar's end
  is a target worth testing.
  - Bybit's capture has the bar 2.7 s after close at the median, but 7.7 s at p90,
    before any signal is computed or a quote is fetched. The 30 s settle and the 10 s
    poll are today's main delay, not the data.
  - Whether a new path meets the target, and what the economics are at its actual
    latencies including their tail, is for a bounded measurement to show.
  - Nothing here calls for co-location or sub-second engineering.
- **This is not money yet.**
  - The middle cost scenario is 41 bps per round trip. Against the 2.7 s mean of
    106 bps it leaves about 65 bps, but the 2.7 s median (33 bps) does not clear it.
    The case rests on the tail.
  - Slippage just after a +5% minute is not measured and is very likely above 15 bps.
  - The population is the window in which the cell was found.

## Next (agreed order after review)

1. **HYP-030 design:**
   - one rule: the 1-minute burst on Bybit, unchanged;
   - the moment its features are available;
   - the universe and how gaps are handled;
   - portfolio limits;
   - the decision criterion.
2. **Power for bursts specifically:**
   - their dispersion;
   - the dependence between firings (about 16 a day are not 16 independent
     observations).
3. **A bounded measurement of a new path:**
   - the distribution of its latencies;
   - executable quotes at entry and exit for the probe size, fees and funding.

   The v2 and HYP-015 paths stay as they are.

4. **Registration and a sealed forward test.** It is read only once HYP-012 v2 is
   terminal; 2026-10-31 alone does not make it so.

No architecture or library change follows from this readout. The measured slack is in
today's fixed waits and polling.
