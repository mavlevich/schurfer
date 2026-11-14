# B3a discovery result, 2026-11-14

**Both fixed taker policies fail on mature negative net.** This is an economic
failure under the registered model, not insufficient counts, diversity or power.
The budget verdict is `maker_source_review_needed`, not a maker pass and not
the registered whole-track spending pause. No exam or further archive fetch runs.

The [contract](b3-economic-screen-proposal-v1.md) and code were published in main
by #593 (`45fc1a1`) before native fetching or reporting. The owner separately
authorized fetching and then answered "Да, рассчитать оба выхода" for this
formal read. The reader ran from clean `45fc1a14d0d4ccc70175439505b7b36eb4662a0e`.
No code or parameter changed after the first outcome. Both exits were read
together with the original seed 3103 and 20,000 calendar-week draws.

## Fixed policies and sample

Bybit linear USDT perpetuals; the exact B1 d30 list has 1,637 opportunities,
150 symbols and 718 forecast-settlement symbol-day clusters. Signal at forecast
t minus 30 minutes, long at the next minute's native bar open. Exit either at
t minus one minute (`before`) or t plus one minute (`through`). Costs are fixed
at 30 bps round trip, with 50 bps stress. These are bar-price execution proxies,
not observed fills or evidence that native quantity constraints are satisfied.

Actual settled funding uses the pinned discovery index and the registered
mark-bar valuation proxy/sensitivity. It is accounting, never a future-known
entry filter. Average events within complete clusters, then weight clusters
equally. A cluster with any unresolved event contributes no fabricated return.

| Measure                                          | Before settlement | Through settlement |
| ------------------------------------------------ | ----------------: | -----------------: |
| Complete events / original 1,637                 |    1,626 (99.33%) |     1,628 (99.45%) |
| Unresolved: zero-volume execution bar            |                11 |                  9 |
| Complete clusters                                |               708 |                710 |
| Symbols / event weeks                            |          150 / 46 |           150 / 46 |
| Top-five-symbol cluster share                    |            15.25% |             15.35% |
| Mean price return, bps                           |             +3.97 |             -60.69 |
| Mean funding, bps                                |              0.00 |             +70.26 |
| Mean price plus funding before costs, bps        |             +3.97 |              +9.57 |
| **Mean net at 30 bps, bps**                      |        **-26.03** |         **-20.43** |
| Mean net at 50 bps, bps                          |            -46.03 |             -40.43 |
| Median cluster net30, bps                        |            -28.99 |             -17.34 |
| Cluster win share                                |            39.41% |             43.52% |
| Cluster profit factor                            |             0.667 |              0.732 |
| Week-bootstrap net30 percentiles 2.5 / 97.5, bps |    -41.39 / -8.32 |     -36.04 / -4.06 |
| Circular two-week net30 percentiles, bps         |    -39.82 / -8.58 |     -34.41 / -5.24 |
| MDE against zero, diagnostic bps                 |             23.48 |              22.71 |
| BTC-adjusted mean net30, bps                     |            -23.97 |             -18.70 |
| Net30 without top five contributing symbols, bps |            -35.67 |             -31.02 |
| Net30 without top five contributing weeks, bps   |            -39.05 |             -34.04 |
| First / second calendar half net30, bps          |   -27.58 / -24.83 |    -24.94 / -17.00 |
| **Verdict**                                      |          **fail** |           **fail** |

Bounds are per-exit week-bootstrap percentiles. The registered nomination rule
uses the one-sided 97.5% lower bound for each of two selectable exits. Do not
call the two displayed central intervals a simultaneous 95% confidence band.
The circular two-week sensitivity can join the final calendar week to the first.
All 53 calendar ISO weeks touched by discovery enter resampling, including
weeks without events. Event weeks above count weeks with complete clusters.

Count/diversity/concentration/coverage floors pass. Both policies fail the
size, lower-bound, halves, top-five-removal, BTC and funding-sensitivity checks.
`negative_mature_net` takes priority over other failures. Receiving funding
through settlement does not cover both the associated price fall and the
registered trading costs in this sample. The cohorts differ slightly because
of missing execution bars; no causal paired exit improvement is inferred from
subtracting the two displayed means.

## Budget decision for track B

The registered upper diagnostic is the 97.5th percentile of price plus funding
before execution costs, using the favorable mark valuation sensitivity:

- `before`: **21.68 bps**;
- `through`: **26.08 bps**.

Both are above the fixed 15 bps spending hurdle. The rule therefore returns
**`maker_source_review_needed`**, not `track_b_paused_budget`. This neither
establishes positive maker EV nor approves a large archive download. Maker
entry prices, timing and selection differ; changing the fee in these taker
returns does not compute a maker result.

Both current taker policies stop. The next permissible design question is
whether B2's unresolved ordinary-flow eligibility, historical quantity rules
and exit-source coverage can support the already frozen maker model. No
parameter, exit or sample is tuned here, and no new maker fetch is launched.
The exam stays closed.

## Mandatory one-slot portfolio diagnostic

Initial model cash USD 50, reserve USD 5, entry notional min(45, cash minus 5),
minimum modeled notional USD 5, no size growth above USD 45. The threshold is
not a verified native order minimum. Hash ordering, slot skips and stored
position sizes follow the pre-read corrected contract.

| Known-prefix measure                                | Before settlement | Through settlement |
| --------------------------------------------------- | ----------------: | -----------------: |
| Closed positions                                    |               640 |                640 |
| Opportunities in known prefix                       |             1,226 |              1,226 |
| Known-prefix model PnL, USD                         |            -36.69 |             -35.75 |
| Known-prefix ending cash, USD                       |             13.31 |              14.25 |
| Maximum realized drawdown from model cash peak, USD |             49.41 |              49.50 |
| Longest closed-trade losing streak                  |                11 |                 11 |
| Known accepted holding hours                        |            298.67 |             320.00 |
| Slot-busy skips                                     |               586 |                586 |
| First selected unresolved position                  |                 1 |                  1 |
| Later opportunities with portfolio value unknown    |               410 |                410 |
| Full-cohort ending cash                             |           unknown |            unknown |

The known prefix starts **2024-10-22 15:31 UTC** and ends immediately before
**2025-06-07 11:31 UTC**: 227.83 elapsed days, touching 229 UTC dates. Cash
above is derived from the already reported daily realized PnL on this prefix.
It is not a full-year ending balance. The unresolved selected entry and all
subsequent portfolio values remain unknown, not cash, zero return or a skipped
trade. Drawdown is from the modeled cash peak, not simply loss from initial
USD 50; intrabar margin/liquidation risk remains unmeasured. These diagnostics
do not gate event-level nomination and are not actual account performance.

## Provenance and verification

[Evidence inventory](evidence/b3a-discovery-2026-11-14/manifest.json) and
[reproduction notes](evidence/b3a-discovery-2026-11-14/README.md).

| Artifact                | SHA-256                                                            |
| ----------------------- | ------------------------------------------------------------------ |
| Original result JSON    | `4d4f6f1adc18bda779c48aa78bf7ea997f34f47b79bf602e5f32e3cd87444b33` |
| Sealed native responses | `d952ecf2b22d3ede3b393347dda51f12a518b3601b5537bb9842a383be29740d` |
| Run binding             | `c4563c4877fb2f4b592f6b14b92809db02612eb4e72eb16136e2372b9d7082fc` |
| Frozen request scope    | `c199994e706ae956ed454682a757c867fab187581a233c7d3d5b3897bdb56213` |
| Funding index           | `f48fe09f7023a476e524cf70c6ec1b629eee30a9a07cff41c9d149dfb59e0cf5` |

Fetch: 4,200 complete requests, 4,206 attempts including six recovered API
retries, 9,997,957 native response-body bytes, 15 MiB conservative budget
reservations. First-to-last receipt duration was 1,460.943 seconds. All native
receipts and the sealed snapshot are retained outside disposable worktrees at
project-root `runtime/research/b3a-v1/`.
The hash-verified funding index is retained there under `inputs/` as well,
using a hard link of the original immutable file rather than another fetch.

The report revalidated every receipt, identity, native body hash and retry
consistency. The original JSON is retained byte-for-byte in deterministic gzip.
Independent arithmetic on its existing event rows reproduced all four cluster
means for each exit within 1e-10 bps and the price-plus-funding-minus-cost identity
within 1e-9 bps. Prefix cash was reconciled to reported daily realized PnL.
No extra data request, exit, filter, bootstrap seed or alternative missing-data
portfolio was introduced by this verification.

The read is exposed discovery. The earlier 4h selection and at least 124
exploratory configurations plus eight B2 execution configurations remain in
the selection history. The HYP-031 test year, backward holdout and HYP-012 v2
blind window remain untouched by this computation. There is no promotion,
production deployment or order.
