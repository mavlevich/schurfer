# Net-buy accumulation discovery v1 (frozen contract)

Status: **frozen contract, two pre-registered primaries, locked 2026-09-11;
pending a second colleague review before any code.** Green tests are not a
substitute for that review. This document cannot promote a strategy, start a
worker, authorize a deploy, or place an order. Historical evaluation on this
contract is Discovery only: a positive result nominates a hypothesis for a fresh
prospective cohort, it never confirms an edge.

Family: order-flow / pre-price accumulation. Distinct from `early_momentum_v4`
(point-in-time features) and the retired near-trigger one-minute
`taker_imbalance` family; it reuses none of their name, cutoff, report or slice.

## Two questions, two pre-registered primaries

We test both, honestly, as two frozen hypotheses with a joint multiple-testing
correction (Holm across the family of two), never "run both and keep the prettier
one". Both are LONG-only (the thesis is accumulation before a rise; direction is
fixed in advance) with a 240-minute primary horizon (60m is diagnostic only).

Per-minute point-in-time inputs from `timeseries.bybit_momentum_bars_1m`:
`net_buy(m) = buy_total_notional_usd - sell_total_notional_usd`;
`activity(m) = buy_total_notional_usd + sell_total_notional_usd`.

- **P-MAG (magnitude):** `score_m(t) = ( sum of net_buy over the trajectory
window W ) / ( mean daily activity over the baseline B )`. Accumulation size in
  the token's own "normal-days" units. Fires when `score_m >= THETA_M = 1.0`
  (a 24h net buy of at least one baseline-day of activity).
- **P-SHAPE (breadth):** `score_s(t) = share of minutes in W with net_buy(m) >
0`. The "many small, distributed" shape in its purest form, independent of
  size. Fires when `score_s >= THETA_S = 0.60` (net buying in at least 60% of the
  window's minutes).

`THETA_M` and `THETA_S` are frozen a priori on interpretable grounds, never tuned
on outcomes. The two fires are independent event streams; an instrument may fire
on one, both, or neither. Each primary is analysed on its own fired-episode set
and floor; the joint Holm correction applies to the two headline claims.

## Unit of observation (anti-look-ahead)

The unit is **every eligible `(exchange, instrument, minute)`** on the scanned
universe, NOT a known pump and its prior day. Selecting instruments already known
to have pumped and reading their previous day is forbidden (it only ever sees
winners). For each eligible minute `t`, features come only from bars strictly
before `t`; a fire is decided at `t`; the forward outcome is read after `t`. No
outcome, price after `t`, or MFE/MAE is ever an input to a score or to fire
selection.

## Window, baseline, availability, completeness

- **Trajectory window `W`**: the 1440 one-minute bars in `[t - 24h, t)`.
- **Baseline `B`**: `[t - 24h - 7d, t - 24h)` (seven prior days). `B` uses no bar
  at or after `t - 24h`, so "elevated" is relative to the token's own prior
  normal.
- **Availability (anti-look-ahead)**: a bar counts only when its trade data was
  received before `t` (`last_trade_received_at IS NOT NULL AND < t`).
- **Completeness / minimum counts**: a bar counts only when `trades_complete`. A
  minute is `insufficient_bars` (a counted coverage step, never a negative) unless
  `W` has at least **1152** complete, available bars (80% of 1440) AND `B` has at
  least **8064** (80% of 7 x 1440). A daily cold-export manifest existing does not
  prove per-instrument continuity, so these counts are checked per instrument, not
  per day.
- **Near-zero baseline**: if `mean daily activity over B < BASELINE_ACTIVITY_FLOOR
= 100000 USD`, the normalization is meaningless and the minute is
  `insufficient_baseline` (coverage, not a negative). This also removes the
  divide-by-near-zero explosion in `score_m`.

## Causal fire and cooldown

A "fire" is a single, causally decidable event, defined per primary:

- The fire is the **earliest** eligible minute `t` (chronologically) at which that
  primary's score crosses its frozen threshold (`score_m >= THETA_M`, or
  `score_s >= THETA_S`). It is decidable in real time: it uses only bars before
  `t`. It is NOT a retrospective argmax over a window (that would use future
  minutes to choose the entry).
- After a fire on an instrument for a given primary, a **half-open cooldown
  `[fire, fire + 24h)`** suppresses further fires of that primary on that
  instrument, so one slow build is one episode, not 1440. Cooldown is per
  `(instrument, primary)`. No separate overlap-purge is needed.

## Entry and exit price semantics

Scanner minutes are not real strategy decisions, so no `trade_decision_outcomes`
row exists for them; the forward return is computed from bar prices, causally.

- **Entry price** = `close_price` of the **last complete bar strictly before the
  fire minute `t`** -- the most recent price actually observable when the fire is
  decided. Using `close_price(t)` (the fire minute's own close, 60s later) would
  be a one-minute look-ahead and is forbidden.
- **Exit price** = `close_price` of the bar at `t + 240m`.
- `price_source` is `bybit_momentum_bars_1m.close_price`; both the entry and exit
  bars must have `price_complete` true and be inside the frozen range (exact bar
  boundaries, no straddle past the frozen data).
- **Internal-gap policy**: if the entry bar, the exit bar, or any bar needed to
  place them is missing/incomplete, the episode is `unresolved` (a counted step,
  never a negative). Forward return is `(exit - entry) / entry` for the long,
  net of costs (below).

## Ranking, quantiles, ties

- **Pooled** across bybit and binance (one universe); per-exchange coverage is
  reported but ranking is not split by venue.
- Quantiles are computed **over the fired episodes** of each primary (not per
  raw minute): the fired set is split into five score quantiles and forward
  economics are compared top versus bottom. No entry threshold beyond the frozen
  fire threshold is tuned on outcomes.
- **Tie handling (Rule 6, the HYP-023 lesson)**: report the distinct-value count
  and the largest tied group of the ranking score; require every adjacent
  compared-quantile boundary to fall between distinct values; a tie straddling a
  boundary downgrades a candidate to insufficient.

## Identity, cluster, universe version, capacity

- **Universe**: the continuously captured bybit + binance linear-USDT-perpetual
  momentum-bar universe (the only venues with pre-decision flow bars). Other
  venues are coverage loss, not negatives.
- **Identity**: exact `(exchange, native_market_id, market_type, capture_version)`
  resolved point-in-time at `t` through `momentum_universe_snapshots` /
  `_instruments`, fail-closed on unresolved/ambiguous. The bar `market_type` is
  the capture literal `"linear"` (`BYBIT_MOMENTUM_MARKET_TYPE`), never the
  identity table's canonical `"linear_usdt_perpetual"` (the HYP-024 zero-join
  bug). The bar join excludes `universe_version`, so a `universe_version` change
  inside the 8-day feature window does not break it; identity is still resolved at
  `t`.
- **Cluster key** = normalized uppercase base ticker, merging the same asset on
  bybit and binance into one cluster for the diversity floor (as in HYP-024).
- **Capacity: `capacity_unknown`.** Stored minute BBO is prices, not depth or
  queue size, and binance bookTicker carries no depth. This contract reports
  spread / turnover / a coarse liquidity segment as proxies and marks executable
  size `capacity_unknown`. Reported economics are **gross-on-proxies, not
  net-proven**, and are never presented as evidence of tradability. Real capacity
  is resolved only by an L2/book-depth shadow around live fires (a later step).

## Costs

The shared `conservative_costs_v1`: two-sided taker fees plus funding scaled to
the 240m hold. No per-decision slippage is invented where depth was never
measured; the absence is named (`capacity_unknown`), not zero-filled. Costs are
applied to every fired episode's return before any economics.

## Outcome, hit, and false positive

Because the ranking metric (median net return) is continuous, "false positive"
needs a binary definition. A resolved fire is a **hit** when its 240m net return
`> 0` (a rise after costs) and a **miss** otherwise. The **false-positive rate**
is the miss share among resolved fires, reported per primary and per quantile
alongside the base rate (hits / all resolved fires).

## Verdict rules (Discovery, frozen)

The historical window is only partly unseen (`burst`/`turnover` were viewed
descriptively in `early-momentum-unused-flow-features-v1`), so any positive
result is a **Discovery candidate**, never a confirmed edge. Every branch is a
computable predicate with frozen constants; there are no un-frozen verbal
thresholds. Evaluated per primary, then the two headline claims are Holm-adjusted
jointly.

- `insufficient_discovery`: the floor is not met (below). No constant is changed
  to reach a verdict.
- `stop` (mature negative): floor met and the top score quantile's median net
  240m return `<= 0`. Does not require the diversity floor.
- `too_rare_or_illiquid`: floor could be met on volume but the fired episodes
  reach `< 30` per week (cannot sustain a prospective cohort in reasonable
  calendar time), OR fewer than `30%` of the top-quantile fires fall in the
  tradable-liquidity segment (`mean daily activity over B >= LIQUID_SEGMENT_FLOOR
= 5,000,000 USD`). A first-class stop, equal to negative EV.
- `discovery_candidate`: floor met AND top-quantile median net return `> 0` AND
  the top-minus-bottom median net spread `>= CANDIDATE_SPREAD_PP = 1.0`
  percentage points with the long sign AND the five quantile medians are monotone
  increasing AND the joint Holm-adjusted block-bootstrap lower bound `> 0` AND the
  result survives leave-one-out of the largest asset cluster, venue and UTC week
  AND the tradable-liquidity share above is met AND Rule 6 adjacent boundaries are
  distinct. Earns only a fresh prospective registration plus a narrow L2 shadow,
  never implementation or live.

## Sufficiency floor (checkable without outcomes)

Per primary, frozen: `>= 150` fired episodes in **each compared quantile** (top
and bottom), `>= 30` distinct asset clusters across the compared quantiles, and
minimum representation in **each distinct UTC week present in the window**. The
fired-episode and cluster counts are computable from fires alone, before any
outcome is read. This window spans roughly 23 calendar days, so it is NOT
presented as four full UTC weeks; the week requirement is minimum representation
per present week, not a claim of four complete weeks.

## Frozen window and forward cutoff (final for this discovery read)

- Continuous frozen cold-export range: `2026-08-10` through `2026-09-10` (32 UTC
  days, no missing day; the `2026-09-09`/`2026-09-10` days were caught up and the
  cold-export systemd timer installed and enabled on 2026-09-11, so the range no
  longer erodes).
- Baseline `B` starts at `2026-08-10`; the decision window is
  `[2026-08-18T00:00Z, 2026-09-10T20:00Z)`, so every fired minute's full `24h +
7d` features and its `t + 240m` exit bar fall inside the frozen range.
- This window is FINAL for the discovery read; later days accrue to a separate
  prospective cohort, never to re-extending this window after a read. The report
  records the exact per-day cold-bar manifest SHA hashes it read, and verifies
  per-instrument bar counts (a daily manifest is not proof of per-instrument
  continuity).

## Data preservation (done)

Minute bars purge from PostgreSQL after 35 days and a plain backup does not save
them (`cold_bar_export.py`). As of 2026-09-11 the frozen days above are exported
with manifests and SHA hashes, and the daily systemd export timer is installed
and enabled, so the discovery range is preserved and future days keep
accumulating.

## Reproducibility

The report records: database snapshot time, generation time, git revision,
dirty-tree state, both frozen score definitions and thresholds with a contract
version, window/baseline bounds, cooldown, the exact cold-bar manifest hashes,
cost version, floors, the Holm family, the bootstrap seed, and a SHA-256
fingerprint of the fired-episode dataset (both primaries) in deterministic order.

## Locked decisions (2026-09-11)

1. Two pre-registered primaries, LONG, 240m: P-MAG (`score_m`, `THETA_M = 1.0`)
   and P-SHAPE breadth (`score_s`, `THETA_S = 0.60`); joint Holm across the two.
   Shape is a primary here (not a diagnostic) because the thesis is shape; any
   further shape variant is a new versioned contract on new data.
2. Causal fire = earliest threshold crossing; cooldown `[fire, fire+24h)` per
   `(instrument, primary)`.
3. Entry = close of the last complete bar strictly before `t`; exit = close at
   `t+240m`; both `price_complete`; gaps -> `unresolved`.
4. Pooled ranking; quantiles over fired episodes; Rule 6 ties.
5. Cluster = base (merges bybit/binance); bar join excludes `universe_version`.
6. `BASELINE_ACTIVITY_FLOOR = 100000`; min bars `W >= 1152`, `B >= 8064`.
7. Hit = 240m net return `> 0`; false-positive rate = miss share.
8. Frozen verdict constants: `CANDIDATE_SPREAD_PP = 1.0`, `too_rare < 30
fires/week`, `LIQUID_SEGMENT_FLOOR = 5,000,000 USD`, tradable share `>= 30%`.
9. Floor `>= 150` fired episodes per compared quantile, `>= 30` clusters, minimum
   representation per present UTC week (window is ~23 days, not four full weeks).
10. Window `[2026-08-18, 2026-09-10T20:00Z)`, baseline from `2026-08-10`, final
    for discovery; capacity `capacity_unknown`, economics gross-on-proxies.

All frozen constants above were chosen a priori on interpretable grounds, before
any outcome was read. A reviewer may adjust any of them, but only before the
scanner is written; none is tuned on results.

## Remaining gate before code

A second colleague review of this frozen contract. After sign-off, the
point-in-time scanner and its tests are written against these locked decisions;
no historical outcome is read before then.
