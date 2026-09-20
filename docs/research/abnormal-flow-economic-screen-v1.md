# Abnormal-flow economic screen v1 -- DRAFT

> **Not registered; no outcome read authorized.** This is the proposed third and
> final near-term discovery family in the [edge decision program](../../ROADMAP.md#near-term-edge-decision-program--2026-09-15).
> HYP-012 and HYP-015 keep their own prospective cohorts. This document does not
> change their contracts, enable trading, or revive the closed pump-short,
> accumulation, or monster-harvest strategies.

## Decision this study may support

Test whether an **early, same-venue OI-and-taker-flow imbalance with limited
contemporaneous price movement** predicts a tradeable long move over 12 hours,
above a matched price/liquidity baseline. A historical pass is discovery-only:
it can nominate at most one separately registered forward cohort, not PAPER or
live trading. A historical failure closes only this declared bar-level
Bybit/Binance mechanism. It says nothing about uncollected venues or L2 state.

The primary candidate is _proposed_, not frozen: 60-minute feature lookback,
long-only, 720-minute outcome horizon. The numerical OI, buy-pressure, and
price-containment thresholds, entry cost, and portfolio policy must be
registered before replaying returns. Do not select them from endpoint results.

## Population and information boundary

- Scan every captured `(exchange, native instrument, market_type, minute)` in
  the declared historical window. Neither pump events, Telegram alerts,
  current catalog membership, nor subsequently known winners select rows.
  Bybit and Binance remain separate exact-native paths; do not join on ticker
  or pool their raw OI units.
- Primary input is `timeseries.bybit_momentum_bars_1m`, `market_type=linear`,
  `capture_version=v1`. Pin the input window, universe/capture versions,
  per-day Parquet manifests and file hashes before the first outcome read.
  A present bar is not necessarily usable: require the relevant
  `price_complete`, `trades_complete`, and `open_interest_complete` flags.
- The decision clock is **after** finalization of the last feature bar.
  The replay must check `created_at <= decision_at`, trade receive time and
  OI observation time `<= decision_at`; it must never use the deciding
  minute's final bar before it was available. Choose and freeze the scan lag
  from outcome-blind finalization-lag data. If a required timestamp or bar is
  absent, classify the row as unavailable; never replace it with a later
  observation.
- Use a continuous 60-minute history on the exact route and source version.
  OI change is a _within-instrument percentage_ of native OI amount, not a
  cross-venue comparison of OI levels. Binance's USD OI-value field is not
  available in the sampled data, so a shared signal must not silently treat
  it as zero or substitute an unversioned mark-price conversion. Buy pressure
  uses actual accepted taker-buy and taker-sell notional; zero-flow windows
  are explicitly unavailable for the ratio.
- Eligibility floor (frozen with the thresholds, `min_oi_notional_usd` +
  participation). Native OI is converted to USD by ONE registered per-venue rule
  at a point-in-time price at or before the decision (native OI amount x the
  decision-time mark/close price); Binance's USD OI is that conversion, never a
  zero or an unversioned substitute. Participation divides the fixed position by
  PRE-DECISION turnover over a frozen window (`participation_turnover_window_minutes`),
  never the future entry minute. A venue/day below the floor is ineligible, so the
  signal is not a small-cap noise detector and is at least nominally tradeable.
- Buy/sell USD semantics differ by venue and must be verified against the writer
  and real rows before use: Bybit sums individual trades, Binance sums aggTrades;
  both flow through price x size but are not identical. Describe and test this; if
  the notional is not comparably available on a venue, narrow scope rather than
  compare unlike quantities.
- Canonical asset identity is resolved as of the decision time for clustering
  and duplicate control. An unresolved identity is counted in the funnel and
  cannot be silently treated as a distinct ticker asset.

## Proposed signal and control

The **one primary** screen requires (a) positive within-instrument OI growth
over the prior hour (percent of native OI amount), (b) aggressive taker-buy
notional dominating sell notional (> 0.5 of buy+sell USD) over that same hour,
and (c) restrained price movement before the decision, measured as the MAXIMUM
deviation of the prior hour's 1m bar highs and lows from the window's opening
price (`price_containment_max_bar_dev`) -- bar extremes, not closes, so an
intraminute wick that closes back near the open (e.g. SAYLORMOON) is NOT counted
as restrained. This tests a pre-breakout accumulation mechanism, not the
already-tested near-trigger one-minute imbalance and not HYP-012's cross-venue
lead. The OI ABLATION runs the same primary cell over the SAME eligible set with
only the OI-growth threshold removed, so it isolates the OI effect and never
changes the token composition; no incremental OI benefit closes the mechanism.

Before reading returns, freeze either fixed thresholds or one deterministic
_outcome-blind_ threshold-calibration rule, its calibration window, and a
single selected cell. No post-result sweep over lookbacks, directions,
horizons, price-containment cutoffs, or venues may nominate a winner. Shape,
buy-burst, liquidation, and short-side variants are diagnostics or future
separate hypotheses, not hidden primary cells.

Form episodes before evaluation: one first qualifying decision per
`(exchange, canonical asset)` followed by a frozen cooldown at least as long
as the 720-minute horizon. A cross-venue same-asset overlap is reported for
portfolio concentration; it is not silently multiplied into independent
asset evidence. Controls are selected point-in-time from the same venue,
calendar regime, liquidity band, and pre-decision price-movement band, using
a frozen deterministic matching rule. Report both standalone strategy net
economics and excess over that matched control; a profitable market regime
alone is not alpha.

## Outcome and executable-price limitation

The historical minute bars contain OHLC and a last bid/ask, but **not the
timestamp of that last quote**. A quote stored in the minute bar is not proof
it was available or fillable at a chosen intraminute entry. Therefore the
historical replay must label its next-bar entry and exit as a **priced proxy**,
charge a pre-registered conservative spread/slippage/fee/funding model, and
show a break-even-cost sensitivity. It must not claim measured executable
fills or capacity from these bars alone. Same-venue gaps or missing quotes
are unresolved, never filled from another venue or later hindsight.

If the bar-level economics and matched excess both survive, the next gate is
a bounded point-in-time quote/depth/latency shadow on the candidate fires.
Only that gate can support a claim about executable size. A failing bar-level
screen does not justify building the shadow or adding exchanges.

## Required replay artifact and ordered decision

The implementation PR may contain the contract and tested replay code, but
**no result or economic conclusion**. Before running it, pin the exact input
window, data/code fingerprints, one primary threshold rule, entry/exit and
cost model, missingness ceilings, evidence floor, selection/cooldown,
portfolio bank and slot policy, baseline matching, and the one-shot verdict
rule. At minimum the output includes:

1. full scanned/eligible/fire/episode/resolved/unresolved funnel by venue and
   rejection reason, including zero-flow, stale OI, unavailable price,
   incomplete lookback, and missing outcome;
2. standalone net return and matched excess with cluster- and week-aware
   uncertainty, asset/week concentration, and leave-one-out checks;
3. signals per week, notional/participation proxy, concurrency, capital
   occupancy, drawdown and losing streak for the frozen fixed-bank portfolio;
4. dollar PnL for the actual historical window at the $300 research bank,
   conservative cost sensitivity, and an explicitly labelled capacity
   _unknown_ rather than an invented scaling claim.

A mature negative standalone net result is a stop before diversity commentary.
Positive but underpowered evidence is insufficient, not promotion. A candidate
requires positive standalone after-cost economics **and** matched excess,
acceptable portfolio risk, and a material dollar path; historical discovery
alone never authorizes live trading.

## Outcome-blind capability check (2026-09-19; not an edge result)

One day of production bars, `[2026-09-18, 2026-09-19) UTC`, was aggregated
without reading forward returns. At production revision `72ff35e`, both
venues had price, trade, and OI-amount observations and positive bid/ask
fields. The row counts were Bybit 741,600 and Binance 750,405. Bybit had
735,676 `price_complete` and `open_interest_complete` bars and 737,280
`trades_complete` bars; Binance had 750,405 for all three. OI-value was
nonnull on all Bybit rows and **zero Binance rows**; native OI amount was
nonnull on every row of both venues. This is a one-day operational preflight,
not a fingerprinted research dataset or a guarantee of historical continuity.

The query was a bounded same-day aggregate on the exact bar table, with no
outcome join. Before implementation, run a repeatable outcome-blind coverage
audit over the proposed research window and archive its query, revision,
row counts, coverage reasons, and output hash. If the input cannot support
the declared signal on both venues, narrow scope **before** any outcome read;
do not silently loosen quality gates after seeing returns.

The offline `abnormal-flow-input-audit` command implements the first part of
that gate over frozen daily Parquet files. It requires every UTC day and checks
each manifest's file hash, row count, bounds and whole-row source-fidelity
fingerprint before aggregating only input availability. Its JSON output lists
each input file/hash and per-venue/day quality counts. It does not calculate a
signal, join outcomes or authorize a formal run. Run it in an isolated research
environment, for example:

```text
abnormal-flow-input-audit --cold-bars-dir <restored-cold-bars-dir> \
  --start-day <YYYY-MM-DD> --end-day <YYYY-MM-DD-exclusive>
```

## Open decisions before registration

1. Owner approval of the one primary mechanism, 60-minute lookback, long
   direction, and 720-minute horizon.
2. One outcome-blind threshold-selection rule and its window; scan lag and
   strict OI freshness by venue; historical window and source manifest.
3. Fixed entry/exit proxy, fee/funding/slippage assumptions, the matched
   control, episode cooldown, fixed-bank portfolio, evidence/missingness
   floors, and the one-shot verdict.
4. A repeatable input coverage artifact, plus tests proving the real query
   path respects availability, native route, gaps, deduplication, and no
   outcome read during calibration.

Until these are resolved this document remains **DRAFT** and the replay must
refuse a formal or promotion-labelled run.
