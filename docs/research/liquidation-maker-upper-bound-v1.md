# Liquidation-cascade maker reversion — post-hoc upper bound v1

## Purpose

Tests whether a passive (maker) reversion trade against the accumulated
liquidation-event history (Binance + Bybit, `timeseries.liquidation_events`,
already live in production) has any economic edge at all, before any
L2/shadow-capture infrastructure is built to test it for real. ROADMAP.md
"Near-term interleaving from 2026-08-31", item 5: "test maker-style
reversion on the accumulated liquidation-event history itself, not the 2
live-paper trades this idea started from... A negative result closes the
direction before any L2/shadow-capture infrastructure gets built for it."

This is discovery-only. It authorizes nothing by itself — see "Verdict
rule" below.

## Cascade definition

One episode is a period where a rolling 5-minute trailing sum of
`estimated_liquidation_notional` for one (exchange, native_market_id,
position_side) crosses **$250,000** (`PRIMARY_CASCADE_NOTIONAL_USD`).
Overlapping/nearby trigger minutes are not independent observations: they
merge into one episode, and a new episode is only allowed to start once
**60 minutes** (`CASCADE_COOLDOWN_MINUTES`) have passed with no new
qualifying trigger minute for that same group.

`$100,000` and `$500,000` are a pre-registered sensitivity family
(`SENSITIVITY_CASCADE_NOTIONAL_USD_FAMILY`) — reported as episode counts
only, never as an alternative formal read. The threshold is never chosen
after seeing which one "looks best."

## Directions are never merged

`position_side='long'` liquidations (forced sells, price pushed down) and
`position_side='short'` liquidations (forced buys, price pushed up) are
two separate populations: separate episode sets, separate evidence
floors, separate verdicts. Never pooled into one sample.

## Entry: a post-hoc optimistic upper bound, not an executable order

`entry_price` is the most extreme price actually touched during the
episode (lowest low for a `long`-liquidation episode, highest high for a
`short`-liquidation episode) — the exact price a resting limit order at
that level would have needed to be filled at.

This is **not** modeled as a real, pre-placed order: the final extremum is
only knowable after the episode has already finished, so describing this
as an executable order would be look-ahead bias. The honest question is:
"if a passive order had somehow always been resting exactly at the best
price this episode ever touched, and it had been filled there, what would
the economics have been?" — an upper bound on what any real resting-order
strategy could ever achieve, not a claim that this specific strategy is
executable. Touching a price is also not proof of a maker fill on its
own: queue position, available depth at that price, and this strategy's
own place in the book are all unknown and unmodeled.

Two separate timers, never conflated: `order_expiry` (how long an
unfilled resting limit order could realistically wait) is **not modeled
here at all** — this study assumes the fill already happened at the exact
extremum. `MAX_POSITION_HOLD_MINUTES` (60, frozen) is the only timer this
module uses: how long the resulting position is held after that assumed
fill, before a taker-style close-out. A future causal/shadow test needs
its own separately frozen `order_expiry`.

## Exit and costs

Exit is an OHLCV-close proxy (`EXIT_PRICE_SOURCE_VERSION =
"ohlcv_close_proxy_v1"`), ceil-aligned to the first fully-closed 1-minute
bar at or after `entry_at + 60 minutes`, with a max 2-minute gap
tolerance (`MAX_EXIT_BAR_GAP_MINUTES`). Costs via the shared
`schurfer_performance.calculate_performance`: maker fee/rebate on entry
(`MAKER_ENTRY_FEE_BPS = 0.0`, no rebate assumed), taker fee
(`DEFAULT_COSTS.taker_fee_bps_per_side`) and a pre-registered conservative
slippage assumption (`EXIT_SLIPPAGE_BPS_ASSUMED = 15.0`,
`REQUIRE_EXIT_SLIPPAGE_SENSITIVITY = True`) on the exit, funding prorated
by actual hold duration. Exact-venue OHLCV only — the exchange the
liquidation was captured on, never a different exchange's prices
substituted in; `native_market_id` is resolved to a CCXT unified symbol
via that exchange's own loaded `markets_by_id` index, never reconstructed
from a bare ticker.

## Evidence floor

This codebase's usual 100 resolved episodes / 30 distinct asset clusters
/ 4 distinct UTC weeks, with the usual 35%/45% per-asset/per-week
concentration caps, applied **per direction** — long and short each need
to clear the floor independently.

## Primary metrics

Resolved/unresolved episode counts (by reason), median and mean net
return, profit factor, win rate, MFE, MAE — broken down by side, asset,
and week.

## Verdict rule

Cluster-bootstrap 95% CI (this codebase's shared `clustered_inference`
module, frozen method/seed/iterations/confidence level) on the primary
sensitivity (net return, clustered by `native_market_id`) — gated on the
CI's **upper** bound, deliberately the opposite of this codebase's usual
lower-bound convention: this study models an optimistic _upper bound_ on
what a real resting order could achieve, so the honest reject condition
is "even the optimistic upper bound's own confidence interval never
crosses into positive territory."

- **`insufficient_data`** if the evidence floor or concentration caps are
  not met.
- **`reject`** if the floor is met and the CI's upper bound is not
  positive — conclusive: no real order could plausibly beat an upper
  bound that already loses money.
- **`positive_warrants_shadow_test`** if the floor is met and the CI's
  upper bound is positive — **not** authorization for paper or live
  trading. It only justifies the next, causal step: a BBO/L2
  shadow-capture test that can actually observe queue position and fill
  probability (ROADMAP item 6: "only if 5 is positive: bounded shadow
  capture — still no real orders").

## Required output

Same shape as this codebase's other discovery reports: funnel (trigger
minutes, episodes at the primary threshold, matured, resolved,
unresolved by reason), the sensitivity family's own episode counts, and
the primary result table (resolved/clusters/weeks/median net/mean
net/profit factor/win rate/median MFE/median MAE/95% cluster CI/verdict)
— per direction.

## Frozen parameters, verbatim from the user (2026-09-03)

Cascade threshold: $250k over 5 minutes, one symbol/side (primary);
$100k/$500k sensitivity family, never cherry-picked. Overlapping
rolling-window triggers merge; a new episode only after a 60-minute
cooldown. Directions analyzed separately (long liquidations → potential
buy/long reversion; short liquidations → potential sell/short reversion),
never merged. Entry exactly at the extremum is valid only as a post-hoc
optimistic potential-fill bound at the final cascade extremum — not a
real pre-placed order, since the final extremum is unknown at trade time.
Touching price does not prove a maker fill (queue, available volume, and
our own position in the book are unknown) — so the result does not
authorize paper/live trading: a negative net even at this oracle-style
entry rejects the candidate immediately; a positive result is only
grounds to build the next causal BBO/L2 shadow test. 60 minutes is the
maximum position hold after a potential fill (`max_position_hold`), kept
distinct from `order_expiry` (how long an unfilled order could wait),
which is not modeled here and would need its own frozen parameter for a
future causal test. Economics: maker fee/rebate on entry; taker fee and
slippage on exit (since exit is not modeled via a separate causal maker
rule); funding if a settlement instant is crossed; exact-venue prices,
never substituting another exchange's prices. Primary metrics:
resolved/unresolved episodes, median and mean net return, profit factor,
win rate, MFE, MAE, drawdown, broken down by side, asset, and week.
Evidence floor: minimum 100 independent resolved episodes, with a
concentration check across multiple tokens.
