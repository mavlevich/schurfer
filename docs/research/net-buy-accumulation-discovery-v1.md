# Net-buy accumulation discovery v1 (frozen contract)

Status: **frozen contract, two pre-registered primaries, locked 2026-09-11 and
revised the same day after the second review; pending a further review before any
code.** Green tests are not a substitute for that review. This document cannot promote a strategy, start a
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
- **P-SHAPE (elevated-buy breadth):** `score_s(t) = share of the W minutes whose
  activity is above the token's own baseline AND is net-buying`, i.e.
  `share of m in W with activity(m) > mean_perminute_activity(B) AND net_buy(m) >
0`, where `mean_perminute_activity(B) = (sum activity over B) / (minutes in B)`.
  This is "gradually elevated, distributed buying" -- not mere sign persistence: a
  token drifting `+$1` in most minutes without any activity build does NOT pass,
  because those minutes are not above baseline activity. Fires when `score_s >=
THETA_S = 0.60` (above-baseline net buying in at least 60% of the window's
  minutes).

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
- **Completeness (fully present, no fabricated zeros)**: both scores are a sum or
  a share over the window, so a missing minute cannot be filled with a `0` -- that
  would fabricate an observation we do not have. The window is used only when it is
  **fully present**: every minute in `W` and in `B` must have a bar that is present,
  `trades_complete`, and (for `W`) available (`last_trade_received_at < t`). A
  genuine no-trade minute is NOT a gap -- the collector writes a real
  zero-notional `trades_complete` bar for a universe instrument with no trades, and
  that real `0` is used. Only a genuinely absent/incomplete bar (a capture gap)
  makes the minute `insufficient_bars` (a counted coverage step, never a negative,
  and never a fabricated zero). Because a daily cold-export manifest does not prove
  per-instrument continuity, presence is checked per instrument-minute, not per
  day.
- **Near-zero baseline**: if `mean daily activity over B < BASELINE_ACTIVITY_FLOOR
= 100000 USD`, the normalization is meaningless and the minute is
  `insufficient_baseline` (coverage, not a negative). This also removes the
  divide-by-near-zero explosion in `score_m`.

## Causal fire and reset

A "fire" is a single, causally decidable event, defined per primary:

- **Edge-triggered.** The fire is a below-to-above crossing: the earliest
  eligible minute `t` where that primary's score is at or above its frozen
  threshold AND the score at the previous eligible minute was below it
  (`score(t) >= THETA` and `score(t-1) < THETA`). It is decidable in real time
  (only bars before `t`), and it is NOT a retrospective argmax over a window
  (that would use future minutes to choose the entry).
- **Mandatory reset + minimum gap.** After a fire on an `(instrument, primary)`,
  the next fire requires BOTH: the score has since dropped below the threshold at
  least once (the reset -- so a single sustained build that stays above the
  threshold is one episode, not a re-fire every day), AND at least 24h have
  elapsed since the last fire. Without the reset, a level that stays above
  threshold would re-fire; with it, one slow build is exactly one signal. No
  separate overlap-purge is needed.

## Entry and exit price semantics

Scanner minutes are not real strategy decisions, so no `trade_decision_outcomes`
row exists for them; the forward return is computed from bar prices, causally.

- Bars are labelled by `bucket_start`: bar `m` covers `[m, m+1)` and its
  `close_price` is observed at time `m+1`.
- **Entry price** = `close_price` of bar `t-1` (the bar `[t-1, t)`), whose close
  is observed exactly at the fire time `t` -- the most recent price actually
  observable when the fire is decided. Using bar `t`'s own close (observed at
  `t+1`) would be a one-minute look-ahead and is forbidden.
- **Exit price** = `close_price` of bar `t+239` (the bar `[t+239, t+240)`), whose
  close is observed at `t+240`. Entry is effective at `t`, exit at `t+240`, so the
  hold is **exactly 240 minutes** -- not 241.
- `price_source` is `bybit_momentum_bars_1m.close_price`; both the entry bar
  (`t-1`) and the exit bar (`t+239`) must have `price_complete` true and lie
  inside the frozen range (no straddle past the frozen data).
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
  bybit and binance into one cluster for the diversity floor (as in HYP-024). A
  base ticker can, however, denote different assets across venues, so the report
  runs a **mandatory collision audit**: any base that resolves to more than one
  distinct canonical instrument (differing native identity beyond the venue) is
  listed, and if collisions are material the cluster key falls back to the
  canonical asset id. A silent ticker merge is not allowed.
- **Capacity: `capacity_unknown`.** Stored minute BBO is prices, not depth or
  queue size, and binance bookTicker carries no depth. This contract reports
  spread / turnover / a coarse liquidity segment as proxies and marks executable
  size `capacity_unknown`. Reported economics are **gross-on-proxies, not
  net-proven**, and are never presented as evidence of tradability. Real capacity
  is resolved only by an L2/book-depth shadow around live fires (a later step).

## Costs and the adjusted return (NOT net)

Every economic figure in this contract is the **fee-and-funding-adjusted return,
slippage unknown** -- written `adj_return` -- never a "net return". It applies the
shared `conservative_costs_v1` (two-sided taker fees plus funding scaled to the
240m hold) to the raw `close_price` path return. Slippage is deliberately NOT
subtracted, because depth was never measured (`capacity_unknown`); it is named,
not zero-filled. A positive `adj_return` is therefore NOT proof of net
profitability -- unmodelled slippage can erase it -- so even a positive result is
gross-on-proxies and earns only a prospective registration plus an L2 shadow,
never a "net proven" claim.

## Outcome, hit, and false positive

Because the ranking metric (`adj_return`) is continuous, "false positive" needs a
binary definition. A resolved fire is a **hit** when its 240m `adj_return > 0` and
a **miss** otherwise. The **false-positive rate** is the miss share among resolved
fires, reported per primary and per quantile alongside the base rate (hits / all
resolved fires). A hit is not a profitable trade: slippage is unknown.

## Verdict rules (Discovery, frozen)

The historical window is only partly unseen (`burst`/`turnover` were viewed
descriptively in `early-momentum-unused-flow-features-v1`), so any positive
result is a **Discovery candidate**, never a confirmed edge. Every branch is a
computable predicate with frozen constants; there are no un-frozen verbal
thresholds. Evaluated per primary, then the two headline claims are Holm-adjusted
jointly.

The object evaluated is the **frozen strategy itself**: enter long on every fire
(the `score >= THETA` edge crossing), hold 240m, one position per fire. The
headline economic claim per primary is the **mean `adj_return` over ALL resolved
fires** of that strategy, not a post-hoc quantile slice. The score quantiles
(does `adj_return` rise with the score above the threshold) are a SUPPORTING
diagnostic only -- trading a chosen quantile would need a second, un-frozen
threshold, so quantile spread never gates a verdict.

- `stop` (mature negative): at least `STOP_MIN_RESOLVED_FIRES = 100` resolved
  fires AND the frozen strategy's mean `adj_return <= 0`. Requires ONLY the
  trade-count floor, never the cluster/week diversity floor: thin diversity must
  not let a mature negative hide behind `insufficient_discovery`.
- `too_rare_or_illiquid`: not a mature negative, but fires are too rare (see the
  coverage-normalized rate in the floor section) to sustain a prospective cohort,
  OR fewer than `30%` of fires fall in the tradable-liquidity segment
  (`mean daily activity over B >= LIQUID_SEGMENT_FLOOR = 5,000,000 USD`). A
  first-class stop, equal to negative EV.
- `insufficient_discovery`: fewer than `STOP_MIN_RESOLVED_FIRES` resolved fires,
  or the candidate diversity floor (below) is unmet without a mature negative. No
  constant is changed to reach a verdict.
- `discovery_candidate`: the candidate diversity floor met AND the frozen
  strategy's mean `adj_return > 0` AND its joint Holm-adjusted block-bootstrap
  lower bound `> 0` AND it survives leave-one-out of the largest asset cluster,
  venue and UTC week AND the tradable-liquidity share above is met. Supporting (not
  gating): the five quantile medians monotone increasing and a top-minus-bottom
  spread `>= CANDIDATE_SPREAD_PP = 1.0` pp with Rule 6 adjacent boundaries
  distinct. Earns only a fresh prospective registration plus a narrow L2 shadow,
  never implementation or live -- and, because slippage is unknown, never a
  "net proven" claim.

## Sufficiency floors (checkable without outcomes)

Two distinct floors, per primary, frozen:

- **Stop floor (trade count only)**: `STOP_MIN_RESOLVED_FIRES = 100` resolved
  fires. This is all a mature-negative `stop` needs; diversity does not gate a
  `stop`.
- **Candidate diversity floor**: `>= 150` fired episodes in **each compared
  quantile** (top and bottom), `>= 30` distinct asset clusters across the compared
  quantiles, and `>= WEEKLY_MIN_FIRES = 20` fired episodes in **each fully-covered
  UTC week** (partial boundary weeks are excluded from this per-week rule, so an
  incomplete first/last week cannot fail it). The fired-episode, cluster and
  per-week counts are computable from fires alone, before any outcome is read.
- **Rarity (coverage-normalized, for `too_rare`)**: rarity is judged on a
  coverage-invariant rate -- fires per **1000 eligible instrument-days** (an
  eligible instrument-day is an instrument-UTC-day with any eligible minute) --
  not on raw fires-per-calendar-week, so partial boundary weeks or uneven
  per-instrument coverage cannot spuriously make a signal look rare. `too_rare`
  fires below `RARE_RATE_MIN` (the equivalent of `< 30` fires per fully-covered
  week at the window's own eligible-instrument-day count), reported with the raw
  count.

This window spans roughly 23 calendar days, so it is NOT presented as four full
UTC weeks; the per-week rule is a numeric minimum in each represented week, not a
claim of four complete weeks.

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

## Required result metrics

Beyond the verdict, the report must always output, per primary and pooled: the
frozen strategy's mean and median `adj_return`, profit factor, hit / false-positive
rate, block-bootstrap CI and Holm-adjusted lower bound, max drawdown and worst
losing streak over the fired sequence, concurrency and capital occupancy (how many
positions and how much notional the frozen strategy would hold at once), the
liquidity-segment breakdown, the cluster collision audit, and the concentration of
`adj_return` by asset cluster, venue and UTC week. A single favorable aggregate is
never reported without these.

## Reproducibility

The report records: database snapshot time, generation time, git revision,
dirty-tree state, both frozen score definitions and thresholds with a contract
version, window/baseline bounds, cooldown, the exact cold-bar manifest hashes,
cost version, floors, the Holm family, the bootstrap seed, and a SHA-256
fingerprint of the fired-episode dataset (both primaries) in deterministic order.

## Locked decisions (2026-09-11)

1. Two pre-registered primaries, LONG, 240m: P-MAG (`score_m`, `THETA_M = 1.0`)
   and P-SHAPE elevated-buy breadth (`score_s` = share of W minutes with
   `activity > baseline per-minute activity AND net_buy > 0`, `THETA_S = 0.60`);
   joint Holm across the two. Shape is a primary here because the thesis is shape;
   any further shape variant is a new versioned contract on new data.
2. Causal fire = edge-triggered below-to-above crossing; next fire needs a reset
   (score fell below threshold) AND `>= 24h` since the last fire, per
   `(instrument, primary)`.
3. Entry = close of bar `t-1` (observed at `t`); exit = close of bar `t+239`
   (observed at `t+240`), hold exactly 240m; both `price_complete`; gaps ->
   `unresolved`.
4. Verdict tests the FROZEN strategy (enter on every fire, hold 240m): headline =
   mean `adj_return` over all fires. Quantile monotonicity/spread is a supporting
   diagnostic, never a gate. Pooled ranking; Rule 6 ties on the diagnostic.
5. Cluster = base (merges bybit/binance) WITH a mandatory collision audit (fall
   back to canonical asset id if collisions are material); bar join excludes
   `universe_version`.
6. Fully-present `W` and `B` (no fabricated zeros: a missing bar is
   `insufficient_bars`, a real no-trade zero-notional bar is used);
   `BASELINE_ACTIVITY_FLOOR = 100000`.
7. Return metric is `adj_return` (fees+funding only, slippage unknown), never
   "net"; hit = 240m `adj_return > 0`; false-positive rate = miss share.
8. Frozen verdict constants: `STOP_MIN_RESOLVED_FIRES = 100`,
   `LIQUID_SEGMENT_FLOOR = 5,000,000 USD`, tradable share `>= 30%`,
   `WEEKLY_MIN_FIRES = 20` (fully-covered weeks), `RARE_RATE_MIN` on fires per
   1000 eligible instrument-days; `CANDIDATE_SPREAD_PP = 1.0` is diagnostic only.
9. Two floors: `stop` needs only `>= 100` resolved fires (no diversity);
   `discovery_candidate` needs `>= 150` per compared quantile, `>= 30` clusters,
   `>= 20` fires per fully-covered UTC week (window ~23 days, not four full weeks).
10. Window `[2026-08-18, 2026-09-10T20:00Z)`, baseline from `2026-08-10`, final
    for discovery; capacity `capacity_unknown`, economics gross-on-proxies, plus
    the mandatory result-metrics block.

All frozen constants above were chosen a priori on interpretable grounds, before
any outcome was read. A reviewer may adjust any of them, but only before the
scanner is written; none is tuned on results.

## Remaining gate before code

A further colleague review of these revisions. After sign-off, the point-in-time
scanner and its tests are written against these locked decisions; no historical
outcome is read before then.
