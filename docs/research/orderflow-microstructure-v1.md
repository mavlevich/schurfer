# HYP-024 — Does the order flow we already collect say anything about the next hour

**Status: registered 2026-09-08, before any relationship to outcome was
computed.**

> **Amended 2026-09-08, before any outcome was joined.** The family rules in
> [entry-signal-family-rules-v1.md](entry-signal-family-rules-v1.md) bind this
> contract and override anything below that contradicts them: the unit of
> observation is the episode and not the decision, the floors count episodes and
> asset clusters, a negative verdict needs as much data as a positive one, no
> outcome may straddle a window boundary, and a feature counts only if it was
> available when the decision was made.
>
> **Amended again 2026-09-09, still before any outcome was joined.** Rule 6 was
> added after HYP-023's `pump_age` candidate was withdrawn: a quintile boundary
> falling inside a tied value splits equal measurements by sort order, so a
> candidate now requires every adjacent pair of compared buckets to differ in the
> feature, and every report states the distinct-value count and the largest tied
> group. This binds here too, and it matters most for any feature recorded at
> coarse resolution.

## Family declaration

One of three hypotheses registered together on 2026-09-08 (HYP-023, HYP-024,
HYP-025), all searching the same recorded data for entry signal. Three
independent searches produce a "finding" at a 5% threshold about 14% of the time
by chance. A positive result here is weaker than the same result from a single
registered search, and it promotes nothing without the held-out window.

## What is already established

`timeseries.bybit_momentum_bars_1m` records per-minute order flow that no
decision uses:

`buy_total_notional_usd`, `buy_trade_count`, `buy_hist_counts`,
`buy_hist_notional`, `buy_max_10s_notional_usd`, `buy_block_trade_count`,
`buy_rpi_trade_count`, `last_bid_price`, `last_ask_price`, and the mirrored sell
columns.

13 GB of it, collected since 2026-08-10 and growing, feeding nothing. The score
is built from `pump_age`, `funding_rate`, `price_extent`, `oi_trend` and
`retrace_from_peak`; none of them is order flow.

## Question

Does an order-flow imbalance measured in the minutes before a decision separate
episodes by their forward 60-minute excursion?

The horizon is the point. The median favourable move in the first hour is 3.99%,
which is a microstructure timescale, and order flow is the class of signal that
ordinarily lives there. We are collecting it and not looking at it.

## Population

Decisions for `pump_short_v1_market_quality` that have bars covering the ten
minutes before the decision, joined to outcomes at the 60-minute horizon,
complete only.

Coverage is reported per exchange. Bars exist for bybit and binance; decisions on
other venues have no order flow at all and are excluded as coverage, never
counted as a negative result.

## The measure, fixed here

One statistic, not a search: **taker imbalance over the ten minutes before the
decision**, defined as

`(sell_total_notional_usd - buy_total_notional_usd) / (sell + buy)`

summed over those bars. Positive means sellers dominated, which for a short entry
is the intuitive direction.

Ten minutes and one statistic, chosen before looking. Block trades, RPI counts,
the size histograms and the bid-ask spread are all recorded and all deliberately
left alone: each is another search, and this hypothesis is already one of three.

## Windows

- **Discovery:** `2026-08-10` (the oldest surviving bar) to `2026-08-25`.
- **Held out, not read in this pass:** `2026-08-25` onward.

Shorter than the other two hypotheses' discovery window because the bars do not
go back further. That is a limitation of the data, and it is why the evidence
floor below is stated in outcomes rather than in days.

## Primary metric, declared before reading

**The spread in median net short return between the top and bottom quintile of
taker imbalance**, at the 60-minute horizon, net of the shared cost model.

## Secondary, context only

Median MFE and MAE per quintile, monotonicity across all five, and the same
statistic computed over five and twenty minutes rather than ten. The alternative
windows are reported to show whether any relationship is a knife edge; they may
not replace the registered ten-minute measure.

## Decision rule, declared before reading

- **Insufficient** before anything else: below **150 episodes** per compared
  quintile or **30 asset clusters** across them, the verdict is `inconclusive`
  in both directions. The bars start on 2026-08-10, so this floor is the one
  most likely to bind here, and it must bind rather than produce a rejection.
- **Candidate** if the top-to-bottom spread exceeds **1.5 percentage points**
  with a monotone relationship across all five quintiles. It earns a read of the
  held-out window and nothing else.
- **Rejected** if the spread is under 0.5 points, and only above the
  sufficiency floor. The order flow we collect does not carry signal at this
  horizon in this form, and the 13 GB is then justified by other uses or by
  nothing.
- **Inconclusive** otherwise.

## What this pass may not do

It may not try the other order-flow columns after this one fails, may not tune
the lookback window, and may not build a composite. Each of those is a new
hypothesis with a new id and an untouched window. The alternative lookbacks are
reported as context precisely so that reaching for them later is visibly a
second search rather than a refinement.
