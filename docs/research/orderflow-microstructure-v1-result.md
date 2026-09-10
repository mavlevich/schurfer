# HYP-024 order-flow microstructure -- formal discovery result

**Status: `inconclusive`; no candidate and no held-out read earned.** This
document records the frozen discovery run registered in
[orderflow-microstructure-v1.md](orderflow-microstructure-v1.md). The corrected
formal report ran on production on 2026-09-09 after PR #397 fixed the bars
market-type join. It read only the registered discovery window; the held-out
window beginning at `2026-08-25T00:00:00Z` remains unread by this report.

## Reproduction

Read-only, from clean production `main`:

```bash
make prod-hyp-024-orderflow-report \
  ARGS="--cohort-end 2026-08-25T00:00:00Z --format json"
```

## Provenance

| Field                           | Value                                                              |
| ------------------------------- | ------------------------------------------------------------------ |
| report version                  | `orderflow_microstructure_v1`                                      |
| strategy version                | `pump_short_v1_market_quality`                                     |
| resolver version / horizon      | `forward_v1` / 60m                                                 |
| capture version                 | `v1`                                                               |
| cost model version              | `conservative_costs_v1`                                            |
| cost deduction                  | `0.20625` percentage points                                        |
| cohort start / end              | `2026-08-10T00:00:00Z` / `2026-08-25T00:00:00Z`                    |
| held-out start                  | `2026-08-25T00:00:00Z`                                             |
| database snapshot               | `2026-09-09T20:12:24.947915Z`                                      |
| generated at                    | `2026-09-09T20:12:25.948633Z`                                      |
| dataset fingerprint             | `6ad531840bcf1624c96062945cacd3f85369acdca53b6a20a76a3e52b075735d` |
| code revision                   | `b118019ecc884fbb14e219d8136e9e34b1b4e7e6`                         |
| working tree dirty / formal run | `False` / `True`                                                   |

### Post-deploy reproducibility check — 2026-09-10

The same frozen command was run after the ENG-020/021/022 production deployment on
clean revision `913d8f7477a2e4029ff071fe1aa2bba31cb4c1bb`. It generated at
`2026-09-10T11:59:47.032174Z` from database snapshot
`2026-09-10T11:59:46.075842Z` with `formal_run=True` and reproduced the exact dataset
fingerprint `6ad531840bcf1624c96062945cacd3f85369acdca53b6a20a76a3e52b075735d`,
coverage funnel, quintiles and verdict below. This is a reproducibility check of the
same closed discovery cohort, not additional evidence or a held-out read.

## Verdict

| Field                                     | Value                                                                                                 |
| ----------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| verdict                                   | `inconclusive`                                                                                        |
| registered measure                        | 10-minute taker imbalance                                                                             |
| top-minus-bottom median net spread        | `+0.454997` percentage points                                                                         |
| compared quintile episodes (top / bottom) | 17 / 18                                                                                               |
| compared asset clusters (union)           | 28                                                                                                    |
| episode floor (`>=150` each)              | not met                                                                                               |
| cluster floor (`>=30` union)              | not met                                                                                               |
| reasons                                   | `episodes_per_compared_quintile_below_150 (top=17, bottom=18)`; `compared_asset_clusters_28_below_30` |

The sufficiency floor binds before the directional rule, exactly as registered.
The observed primary spread is below the `0.5`-point rejection boundary and the
five quintiles are neither monotonically increasing nor monotonically decreasing,
but 89 measured episodes cannot support a formal rejection. Equally, this result
does not earn candidate status, a held-out read, implementation work, or live
trading.

## Coverage funnel

| Step | Population                             | Remaining | Excluded | Exclusion reason                         |
| ---: | -------------------------------------- | --------: | -------: | ---------------------------------------- |
|    1 | representative pump episodes           |       600 |        0 | --                                       |
|    2 | complete same-venue 60m outcome        |       432 |      168 | `no_complete_same_venue_outcome`         |
|    3 | identity resolved to one native market |        94 |      338 | `unresolved_or_ambiguous_identity`       |
|    4 | complete, available ten-bar pre-window |        89 |        5 | `missing_incomplete_or_unavailable_bars` |
|    5 | measured episodes                      |        89 |        0 | --                                       |

### Coverage by exchange

| Exchange | Episodes | Complete outcome | Identity resolved | Measured | No complete outcome | Unresolved identity | Ambiguous identity | Missing/unavailable bars |
| -------- | -------: | ---------------: | ----------------: | -------: | ------------------: | ------------------: | -----------------: | -----------------------: |
| binance  |      199 |              199 |                81 |       77 |                   0 |                 118 |                  0 |                        4 |
| bingx    |       58 |               49 |                 0 |        0 |                   9 |                  49 |                  0 |                        0 |
| bitget   |        8 |                8 |                 0 |        0 |                   0 |                   8 |                  0 |                        0 |
| bybit    |       15 |               15 |                13 |       12 |                   0 |                   2 |                  0 |                        1 |
| gate     |       14 |               14 |                 0 |        0 |                   0 |                  14 |                  0 |                        0 |
| lbank    |      153 |                0 |                 0 |        0 |                 153 |                   0 |                  0 |                        0 |
| mexc     |      146 |              140 |                 0 |        0 |                   6 |                 140 |                  0 |                        0 |
| okx      |        1 |                1 |                 0 |        0 |                   0 |                   1 |                  0 |                        0 |
| xt       |        6 |                6 |                 0 |        0 |                   0 |                   6 |                  0 |                        0 |

Venues without captured bars remain coverage loss, never negative outcomes.
The resolved Bybit/Binance population lost only five episodes at the bar gate
after the join fix.

## Registered 10-minute measure

| Quintile | Episodes | Clusters | Feature range        | Median net | Median gross | Median MFE | Median MAE |
| -------: | -------: | -------: | -------------------- | ---------: | -----------: | ---------: | ---------: |
|        1 |       18 |       16 | `-2.9329 .. -0.9751` |    `0.46%` |      `0.66%` |    `2.68%` |    `4.19%` |
|        2 |       18 |       15 | `-0.9249 .. -0.5453` |    `2.37%` |      `2.57%` |    `5.09%` |    `2.34%` |
|        3 |       18 |       15 | `-0.5382 .. -0.3291` |   `-0.98%` |     `-0.77%` |    `2.93%` |    `4.56%` |
|        4 |       18 |       15 | `-0.3235 .. 0.0485`  |    `0.34%` |      `0.55%` |    `4.33%` |    `2.96%` |
|        5 |       17 |       14 | `0.0657 .. 2.0933`   |    `0.91%` |      `1.12%` |    `3.35%` |    `3.72%` |

- Top-minus-bottom median net spread: `+0.454997` percentage points.
- Monotone increasing / decreasing: `False` / `False`.
- Rule 6: 89 distinct values, largest tied group 1, every adjacent boundary
  distinct, no tied boundary pairs.

## Secondary context

These lookbacks were registered as context only. They do not replace the
10-minute measure or create a new candidate.

| Lookback | Usable episodes | Top-minus-bottom median net spread | Monotone increasing / decreasing | Distinct values | Largest tied group |
| -------: | --------------: | ---------------------------------: | -------------------------------- | --------------: | -----------------: |
|       5m |              89 |                 `-2.260885` points | `False` / `False`                |              89 |                  1 |
|      20m |              88 |                 `+0.443393` points | `False` / `False`                |              88 |                  1 |

The sign reversal at 5 minutes and lack of monotonicity at every lookback are
descriptive only. Selecting another lookback after seeing these values would be
a new search requiring a new hypothesis and untouched window.

## Invalidated pre-fix run

The first production invocation at clean revision
`bab9aa85bd04b1a1773d31b424fcf3192397f2a1` reported 94 identity-resolved
episodes and zero measured episodes with fingerprint
`c48a2309753e94cfc8a31d6ae194858c5887404ae892f38194992806ad0ee2cd`.
That output is invalid and must not be cited as a research result: the SQL joined
`timeseries.bybit_momentum_bars_1m.market_type = 'linear'` to
`momentum_universe_instruments.canonical_market_type =
'linear_usdt_perpetual'`, so every resolved episode necessarily lost its bars.

PR #397 separated the capture-writer and canonical-identity vocabularies and
added a real-PostgreSQL regression fixture containing both actual values. A
read-only production diagnostic before deployment predicted 77 measurable
Binance and 12 measurable Bybit episodes; the corrected formal run reproduced
those counts exactly. `formal_run=True` records revision/tree/window hygiene; it
does not make a semantically defective query valid.

## Boundaries retained

1. `short_return_pct` is a raw price-path return. The report deducts the fixed
   `0.20625`-point fee/funding model; slippage is unavailable and not deducted.
   The constant cost shifts quintile levels but cancels from their spread.
2. Asset diversity is counted by exchange-independent `base`, so the same asset
   on Bybit and Binance is one cluster.
3. Both measured venues use captured `market_type='linear'` and
   `capture_version='v1'`; canonical identity remains
   `linear_usdt_perpetual`.
4. Identity is resolved through the most recent momentum-universe snapshot at
   or before each decision. Ambiguous and unresolved identities fail closed.
5. The discovery window is closed. No HYP-024 held-out read is permitted because
   the registered discovery rule did not produce a candidate.
