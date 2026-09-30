# Pre-blind book-cost baseline v1: one-time readout

Status: descriptive read completed once on 2026-09-30. This is the result of
the protocol registered in [preblind-book-cost-baseline-v1.md](preblind-book-cost-baseline-v1.md)
by #477. The reader was activated by #478. It did not select a strategy
return, PnL, funding charge, price level, OHLCV bar or v2 outcome.

## Provenance and scope

| Item                     | Value                                                                                             |
| ------------------------ | ------------------------------------------------------------------------------------------------- |
| Fixed observation window | `[2026-08-01T00:00:00Z, 2026-09-29T00:00:00Z)`                                                    |
| Database snapshot time   | `2026-09-30T19:19:56.083168+00:00`                                                                |
| Merged reader revision   | `9774c790a0acb094d705b5e7c5497e242daaa40e`                                                        |
| Reader version           | `preblind_book_cost_baseline_v1`                                                                  |
| Working tree             | clean                                                                                             |
| Frozen inputs SHA-256    | `cb2860da3b5e03dcd72b302ebf6c3c6e3b9c7a00c8ae30b2d0c704922386e4da`                                |
| Result SHA-256           | `d5505fadc046347b02e851b5ea169251ae2f12df0f409451dd801d7bb7828b39`                                |
| Durable files            | `runtime/research/preblind-book-cost-baseline/{inputs,result}.json` with matching `.sha256` files |

The two file hashes were independently recomputed on the production host and
matched their saved digest files. Both files are under the configured nightly
`runtime/research` offsite-backup path. This readout does not claim that the
next offsite archive has already completed. The persistent analytics and
execution containers were not restarted for this one-shot read.

## Complete funnel

| Population          |                             Rows in read | Excluded or incomplete                                                              |       Paired book-cost rows |
| ------------------- | ---------------------------------------: | ----------------------------------------------------------------------------------- | --------------------------: |
| Bybit paper v1      |                                    6,633 | 447 entry not opened                                                                |                       6,186 |
| Bybit paper hold12h |                                    6,320 | 4,067 entry not opened                                                              |                       2,253 |
| Source-lead v1      | 4,882 left-join rows from 3,203 captures | 1,524 rows without a pre-cutoff target; 2,634 targets `excluded`; 11 `fetch_failed` | 713: 384 Binance, 329 Bybit |

Another 136 paper rows were excluded before the read because their
`updated_at` crossed the pre-blind cutoff. No source captures or target rows
were excluded by those post-cutoff checks. The paired counts above are the
sums of the registered groups below; no missing quote is assigned zero cost.

No Binance paper or $150 lev3 paper row appears in the fixed pre-blind window.
Thus this read has **only $50 quote sizes** and cannot estimate how impact
grows at $150, $500 or $5,000. Source-lead quote age is `unknown` for all
paired rows, so their freshness cannot be established from this artifact.

## Required midpoint move by registered group

All values are basis points. Fees of 0, 5.5 and 10 bps are **scenarios per
side**, not verified account fees. The required move includes the recorded
ask and bid VWAP impacts; half-spread is already inside those impacts.
`fresh` means both paper books were within the registered `[-1000, 2000] ms`
age range. `outside` means at least one was outside it. `unknown` is a
missing source-lead book age. Source-lead uses an immediate bid and ask from
one capture-time book, not an observed exit 30 minutes later. Full p99 and
maximum values for every group remain in the hashed result artifact.

| Population       | Venue           | Spread bps | Age     |     n | Mean, fee 0 | Mean, fee 5.5 | Median, fee 5.5 | p90, fee 5.5 | Mean, fee 10 |
| ---------------- | --------------- | ---------- | ------- | ----: | ----------: | ------------: | --------------: | -----------: | -----------: |
| Paper v1         | Bybit           | <5         | fresh   | 2,015 |        5.12 |         16.13 |           15.43 |        20.47 |        25.15 |
| Paper v1         | Bybit           | <5         | outside |   108 |        7.66 |         18.68 |           17.92 |        24.18 |        27.70 |
| Paper v1         | Bybit           | 5-<20      | fresh   | 3,374 |       11.58 |         22.60 |           21.78 |        28.89 |        31.62 |
| Paper v1         | Bybit           | 5-<20      | outside |   403 |       12.58 |         23.60 |           22.92 |        29.50 |        32.63 |
| Paper v1         | Bybit           | 20-<50     | fresh   |   255 |       24.26 |         35.29 |           33.84 |        44.61 |        44.33 |
| Paper v1         | Bybit           | 20-<50     | outside |    30 |       23.84 |         34.87 |           32.15 |        43.43 |        43.91 |
| Paper v1         | Bybit           | >=50       | fresh   |     1 |       49.35 |         60.41 |           60.41 |        60.41 |        69.46 |
| Paper hold12h    | Bybit           | <5         | fresh   |   629 |        5.73 |         16.74 |           15.96 |        21.72 |        25.76 |
| Paper hold12h    | Bybit           | <5         | outside |    37 |        7.62 |         18.63 |           19.44 |        23.40 |        27.66 |
| Paper hold12h    | Bybit           | 5-<20      | fresh   | 1,319 |       12.10 |         23.12 |           21.95 |        29.71 |        32.14 |
| Paper hold12h    | Bybit           | 5-<20      | outside |   152 |       12.64 |         23.66 |           23.12 |        28.21 |        32.68 |
| Paper hold12h    | Bybit           | 20-<50     | fresh   |   108 |       24.72 |         35.76 |           34.41 |        46.16 |        44.79 |
| Paper hold12h    | Bybit           | 20-<50     | outside |     8 |       24.48 |         35.51 |           34.89 |        41.90 |        44.55 |
| Source same-book | Gate to Binance | <5         | unknown |   225 |        3.65 |         14.66 |           14.69 |        16.69 |        23.68 |
| Source same-book | Gate to Binance | 5-<20      | unknown |   154 |        8.64 |         19.65 |           18.80 |        24.01 |        28.68 |
| Source same-book | Gate to Binance | 20-<50     | unknown |     5 |       26.77 |         37.80 |           36.81 |        42.78 |        46.84 |
| Source same-book | Gate to Bybit   | <5         | unknown |   127 |        3.95 |         14.96 |           14.88 |        17.75 |        23.97 |
| Source same-book | Gate to Bybit   | 5-<20      | unknown |   187 |       12.18 |         23.20 |           22.40 |        29.79 |        32.22 |
| Source same-book | Gate to Bybit   | 20-<50     | unknown |    15 |       26.02 |         37.06 |           34.81 |        41.98 |        46.09 |

## Interpretation allowed by this read

For fresh Bybit paper quotes at $50, the mean scenario threshold at 5.5 bps
per side rises from about 16-17 bps with spread below 5 bps to about 23 bps
with spread from 5 to below 20 bps, and about 35-36 bps with spread from 20
to below 50 bps. The 10 bps fee scenario is approximately 9 bps higher. The
single >=50 bps observation is not a stable estimate. The source-lead
same-book rows are a different measurement and must not be pooled with paper
entry and exit rows.

These thresholds describe observed **book quotes**, not fills or profits.
They omit latency from signal to order, adverse selection, maker non-fill,
funding and capacity above $50. They cannot revise the registered cost models
or verdicts of HYP-012b, HYP-012c, HYP-029, v2 or HYP-015. A future cohort
can use these distributions to state a break-even target and calculate power
before collecting new data, with its own prospective execution measurements.
