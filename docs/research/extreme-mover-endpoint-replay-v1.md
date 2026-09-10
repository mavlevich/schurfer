# Extreme-mover endpoint replay v1

Status: frozen viewed-window Discovery contract. This report cannot promote a
strategy, start a worker, authorize production deployment, or place an order.

## Question

Across pump episodes already recorded by the measurement and market-quality decision
streams, does an entry at either the first observed decision or the first decision with
recorded market quality show broad after-cost continuation-long or reversal-short
economics at 15, 60 or 240 minutes?

This is the level-C endpoint portion of the broader extreme-mover program. It is split
from the Binance/Bybit minute-path mechanisms because endpoint selection/outcome
integrity is already a coherent runnable result, while minute-path identity, continuity
and next-bar execution require a separate reviewable data boundary. A positive result
here can nominate only an untouched prospective contract. A negative mature result can
stop the corresponding direction before the minute-path PR.

## Frozen window and cohort

- Window: `[2026-08-27T00:00:00Z, 2026-09-10T17:00:00Z)`.
- The end is frozen at least 240 minutes before the first possible report run, so no
  selected outcome straddles the exclusive window edge.
- Strategy versions: `pump_short_measurement_v1` and
  `pump_short_v1_market_quality`.
- Unit: one upstream `pump_event_id`; decisions are sorted by `(ts, row_id)` before any
  outcome is inspected.
- Asset cluster: normalized uppercase base ticker. This is conservative deduplication,
  not cross-venue contract identity proof.
- Outcomes: horizons 15/60/240, resolver `forward_v1`, status `complete`, with both
  `anchor_exchange` and `source_exchange` equal to the selected decision exchange.
- No fallback, proxy venue, later-decision substitution or partial outcome is accepted.
- A selected decision whose horizon ends after the frozen window is unresolved even if
  that outcome becomes available later.

The input fingerprint covers the selected-window decision records and their fetched
resolver rows. The manifest records database snapshot time, generation time, Git
revision, dirty-tree state, selection/cost versions, bounds, floors and bootstrap seed.

## Fixed family

Two anchors are evaluated independently:

1. `first_decision`: earliest supported decision for the event;
2. `first_quality`: earliest supported decision whose point-in-time
   `liquidity.quality.allowed` is exactly `true`.

For each anchor, evaluate long and short at 15/60/240 minutes: 12 trade cells total.
An event with no observed quality-approved decision is cash for the `first_quality`
cell. A decision with an exact outcome but without a sampled USD 100 side-specific
impact is also cash; ticker price is never substituted as an executable entry. A
missing/non-exact outcome is unresolved, not cash and not a loss.

Position notional is descriptive USD 100 because that decision-time impact level is
preserved broadly in the historical liquidity payload. It is not a capital allocation.
Long uses ask impact and short uses bid impact. The shared
`conservative_costs_v1` model charges two taker fees and prorated funding. The primary
exit slippage is 15 bps; 0 and 30 bps are required sensitivities. The outcome endpoint
is still not an observed exit book, so even the primary result is a conservative
modeled-exit screen rather than fully executable evidence.

Stored MFE/MAE are short-oriented. The report swaps them for a long view: short MAE is
long MFE and short MFE is long MAE. It does not infer stop/target ordering.

## Required output

For every cell:

- signals, exact-path trades, cash, unresolved, assets, venues and UTC weeks;
- per-signal and per-filled-trade net mean, trade median, win rate and profit factor;
- total descriptive PnL, maximum sequential drawdown, worst trade and losing streak;
- MFE/MAE, zero/primary/double exit-slippage means;
- trades/day, peak concurrent positions and USD-hours of notional occupancy;
- largest asset/venue/week share;
- deterministic asset-cluster bootstrap interval;
- worst leave-one-asset, leave-one-venue and leave-one-week mean;
- exact 60-minute coverage and entry fillability by venue;
- every coverage/cash reason and every episode result in JSON.

## Routing rule

The minimum Discovery floor is 100 completed trades, 30 asset clusters and two UTC
weeks for a cell.

- `existing_data_candidate`: a pre-declared cell clears all floors, has positive mean
  net return and profit factor above 1 (or no losses), and retains positive mean under
  every applicable leave-one-asset/week/venue sensitivity.
- `stop`: at least one cell clears the floors, but no cell retains positive after-cost
  economics across those sensitivities; or, when there is no candidate, any cell has
  at least 100 completed trades and negative after-cost mean EV. The negative-EV stop
  does not require the asset-cluster or UTC-week floors: insufficient diversity is
  additional context and cannot mask a demonstrated negative result.
- `insufficient_discovery`: no cell clears the floor. No return threshold is changed
  and no best token/venue/week is promoted.

`measurement_blocked` is deliberately not inferred by this endpoint report. Missing
LBank paths are visible, but absent returns cannot establish an economic reason to
build a collector. That routing branch requires an explicit lower-fidelity mechanism
diagnostic or a later report; zero outcomes alone are not evidence of edge.

## Implementation and verification boundary

The implementation is analytics-only: pure selection/economics, a repeatable-read
repository, Markdown/JSON CLI, local/production Make targets and no migration. Tests
cover selection before outcome, partial/alternate-resolver exclusion, cross-venue
exclusion, missing-liquidity cash, window straddles, long/short accounting, candidate
and insufficient verdicts, negative-EV stop precedence, selected-anchor chronological
risk metrics, deterministic serialization, and a real-PostgreSQL repository regression.

The one production run is archived after merge/deploy from clean `main`. Its result is
viewed Discovery. Parameters cannot be edited and re-run on this window to rescue a
direction.
