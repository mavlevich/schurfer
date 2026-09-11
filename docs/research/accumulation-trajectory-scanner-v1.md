# Accumulation-trajectory scanner v1 (frozen discovery contract -- DRAFT)

Status: **frozen contract; primary score and parameters locked 2026-09-11.**
Two gates remain before any code or outcome read: (1) a colleague contract
review (green tests are not a substitute), and (2) the cold-export history
freeze below. This document cannot promote a strategy, start a worker, authorize
a deploy, or place an order. Historical evaluation on this contract is Discovery
only.

Family: order-flow / pre-price accumulation. This is a NEW analytical shape
(the trajectory of activity over a day), distinct from `early_momentum_v4`'s
point-in-time features and from the retired near-trigger one-minute
`taker_imbalance` order-flow family. It does not reuse their name, cutoff,
report, or positive slice.

## Thesis in one line

Gradual, distributed accumulation of aggressive buy activity over roughly a day,
elevated relative to the token's own baseline, precedes a price rise. We test
whether a single pre-committed trajectory score, scanned over the whole
universe, separates forward returns -- including every time it fires and nothing
happens.

## Unit of observation (the anti-look-ahead rule)

The unit is **every eligible `(exchange, instrument, decision_minute)`**, NOT a
known pump and its prior day. Selecting tokens already known to have pumped and
then reading their previous 24h is forbidden: it only ever looks at winners and
cannot see the base rate, the daily false-positive count, or capital occupancy.

For each eligible decision minute `t`:

```
features from bars strictly before t  ->  score(t)  ->  fires or not  ->  forward outcome after t
```

Every fire is recorded, including the ones that go nowhere. Non-fires define the
denominator. No outcome, price, or MFE/MAE is ever an input to the score.

## Frozen window, baseline and availability

- **Trajectory window `W`**: the 1440 one-minute bars in `[t - 24h, t)`.
- **Baseline `B`**: the token's own trailing activity over `[t - 24h - 7d, t - 24h)`
  (seven prior days), so "elevated" means elevated relative to this token's own
  normal, not a cross-token constant. `B` never uses any bar at or after
  `t - 24h`.
- **Availability (anti-look-ahead, reused from HYP-024)**: a bar counts only when
  its trade data was received before `t`
  (`last_trade_received_at IS NOT NULL AND < t`). A closed minute is not proof its
  data was available at decision time.
- **Completeness**: a bar counts only when `trades_complete`. A decision minute
  is `insufficient_bars` (a named coverage step, not a negative) when the frozen
  minimum of complete, available bars in `W` or `B` is not met.

## Fixed direction and horizon

- **Direction: LONG only.** The thesis is accumulation before a rise, so the
  direction is fixed in advance. We do not choose long or short after seeing
  which looked better.
- **Primary horizon: 240 minutes.** `60m` is diagnostic only and never replaces
  the primary.
- **Forward return is computed from the bars, not from
  `trade_decision_outcomes`.** A full-universe minute scan fires at minutes that
  are NOT real strategy decisions, so no resolver outcome row exists for them.
  The forward return is the `close_price` change from the decision bar `t` to the
  bar at `t + 240m`, both required complete and available, on the same frozen
  bars. The `t + 240m` bar must be inside the frozen window (no straddle past the
  frozen data). A fired episode whose forward bar is missing or outside the frozen
  range is `unresolved` (a counted step, never a negative).

## The one primary score -- LOCKED 2026-09-11

The primary is a SINGLE pre-committed statistic with a fixed definition,
direction and horizon. A menu of features (buy-flow, turnover, burst frequency,
cumulative abnormal volume, clustering, several baselines, windows, thresholds)
is not one test, and a multiple-testing correction does not rescue a formula
chosen after seeing results.

Per-minute inputs, all point-in-time from `bybit_momentum_bars_1m`:
`net_buy = buy_total_notional_usd - sell_total_notional_usd`;
`activity = buy_total_notional_usd + sell_total_notional_usd`.

**Locked primary score (A -- baseline-normalized cumulative net buy):**

```
score(t) = ( sum of net_buy over the 1440 available complete bars in W )
           / ( mean daily activity over the seven baseline days B )
```

Magnitude of accumulation in the token's own "normal-days" units. It is the
lowest-degree-of-freedom choice: no weights, no tuned threshold, one direction.
It deliberately captures magnitude, not shape.

**Shape is a pre-declared DIAGNOSTIC, never the primary.** The "many small,
distributed" versus "one spike" intuition is tested descriptively, not baked into
a tunable score. Reported per score quantile, computed but never used to rank or
gate:

- `breadth` = share of minutes in W with `net_buy > 0`;
- `steadiness` = `R^2` of an OLS fit to the cumulative net-buy curve over W;
- `top_minute_share` = the largest single-minute `net_buy` divided by the window
  sum.

If the top score quantile is dominated by high-breadth, high-steadiness episodes,
that is evidence a shape score deserves its own frozen v2 -- with that evidence,
not by eye. Turning any of these diagnostics into the ranking key is a new
contract version, never a mid-study change.

Rejected alternatives (recorded so they are not silently reintroduced): a
breadth-gated numerator, an OLS-slope-times-`R^2` shape score, and a full
model-discovery (train/validate/holdout) framing. Each adds degrees of freedom
the locked magnitude primary avoids; shape earns primacy only through the
diagnostic evidence above, in a versioned successor.

**Ranking is threshold-free.** The scanned universe is ranked by the frozen
score and forward economics are reported by score quantile, so no entry
threshold is tuned on outcomes.

## One signal per episode (no overlap inflation)

- **Cooldown**: after a fire on an instrument, no new fire on that instrument for
  a frozen cooldown (proposed 24h) so one slow build is one signal, not 1440.
- **Overlap purge**: overlapping 24h windows on the same instrument collapse to a
  single episode; the earliest qualifying minute is the episode's decision.
- Episodes are deduplicated per instrument before any outcome is inspected.

## Universe, identity, capture, capacity

- **Universe**: the continuously captured bybit + binance linear-USDT-perpetual
  momentum-bar universe (the only venues with pre-decision flow bars). Other
  venues are coverage loss, not negatives.
- **Identity**: exact `(exchange, native_market_id, market_type, capture_version)`;
  the bar `market_type` is the capture value `"linear"`
  (`BYBIT_MOMENTUM_MARKET_TYPE`), never the identity table's canonical
  `"linear_usdt_perpetual"` (the HYP-024 zero-join bug).
- **Capacity: `capacity_unknown`.** Stored minute BBO is prices, not depth or
  queue size, and binance bookTicker carries no depth. This contract therefore
  reports spread / turnover / a coarse liquidity segment as proxies and marks
  real executable size `capacity_unknown`. It never claims "net proven" through a
  proxy volume. Real capacity is resolved only by an L2/book-depth shadow around
  live fires (next step, not this contract).

## Costs

The shared `conservative_costs_v1`: two-sided taker fees, funding scaled to the
240m hold, and -- unlike a forward path -- no per-decision slippage is invented
where depth was never measured. Costs are applied to every fired episode's
forward return before any economics.

## Verdict rules (Discovery)

The historical window is only partly unseen (`burst`/`turnover` were already
viewed descriptively in `early-momentum-unused-flow-features-v1`), so a positive
result here is a **Discovery candidate**, never a confirmed edge.

- `stop`: at the sufficiency floor, negative after-cost mean/median across the
  top score quantile, or the directional relationship is absent/inverted. A
  negative-EV stop does not require the diversity floor.
- `too_rare_or_illiquid`: the mechanism is sound-looking but fires too rarely, or
  only on instruments too illiquid to trade at meaningful size (proxy segment),
  so it cannot reach the evidence floor in reasonable calendar time or cannot
  hold capital. A first-class stop, equal to negative EV.
- `discovery_candidate`: the top-versus-bottom quantile forward-return separation
  clears a pre-frozen margin with the right sign, survives leave-one-out of the
  largest asset / venue / week, has a plausible signals-per-week rate, and the
  fired instruments include a tradable-liquidity segment. Earns only a prospective
  registration plus a narrow L2 shadow, never implementation or live.
- `insufficient_discovery`: floors not met. No threshold is changed to reach a
  verdict.

Sufficiency floor (frozen): the same discipline as the family (proposed `>= 150`
fired episodes in each compared quantile, `>= 30` distinct asset clusters, `>= 4`
UTC weeks), reported with the base rate and the daily false-positive count.

## Frozen cutoff, and why historical is Discovery-only

- The discovery window and its exclusive forward cutoff are frozen here before any
  outcome is read; no fired episode whose 240m outcome ends after the cutoff is
  resolved.
- Because burst/turnover were already viewed, a positive read on this window is a
  hypothesis, not a test. Confirmation needs a separate untouched prospective
  cohort registered after this contract and its score are immutable.

## Data preservation (prerequisite, must happen first)

Minute bars purge from PostgreSQL after 35 days and a plain backup does not save
them (`cold_bar_export.py`). This contract needs `W` plus seven baseline days, so
the usable history erodes daily. **Before the scanner reads anything**, the
cold-export continuity for the used days must be verified and those days frozen
as immutable artifacts with a manifest and SHA hashes. The report reads only
those frozen days and records their hashes in its own manifest.

## Reproducibility

The report records: database snapshot time, generation time, git revision,
dirty-tree state, the frozen score definition and its version, window/baseline
bounds, cooldown, the exact frozen-day manifest hashes, cost version, floors, and
a SHA-256 fingerprint of the fired-episode dataset in deterministic order.

## Locked decisions (2026-09-11)

1. **Primary score**: A, baseline-normalized cumulative net buy (above). Shape is
   a diagnostic, not the primary.
2. **Cooldown**: 24h per instrument; overlapping 24h windows collapse to the
   earliest qualifying minute as the episode decision.
3. **Ranking**: quantile-only, no tuned trigger threshold.
4. **Sufficiency floor**: `>= 150` fired episodes per compared quantile, `>= 30`
   distinct asset clusters, `>= 4` UTC weeks, reported with base rate and daily
   false-positive count.
5. **Discovery window and forward cutoff** (set 2026-09-11 from the frozen
   cold-export range, final for this discovery read): the continuous frozen
   cold-bar days are `2026-08-10` through `2026-09-10`, 32 days, no gaps (the
   `2026-09-09` and `2026-09-10` days were caught up on 2026-09-11, and the
   cold-export systemd timer was installed and enabled the same day, so the range
   no longer erodes). Baseline `B` starts at `2026-08-10`; the decision window is
   `[2026-08-18T00:00Z, 2026-09-10T20:00Z)`, chosen so that every fired minute's
   full `24h + 7d` feature window AND its `t + 240m` forward bar fall inside the
   frozen range. This window is FINAL for the discovery read: later days accrue to
   a separate prospective cohort, never to re-extending this window after a read.
   The scanner reads only these frozen days and records their manifest SHA hashes.

## Remaining gates before code and before the read

- **Before code**: a colleague review of this frozen contract. Green tests are
  not a substitute for that review.
- **Before the read**: the cold-export history freeze (continuity verified, days
  frozen with manifest and SHA), which also fixes decision 5's dates.

The point-in-time scanner and its tests may be written against this locked
contract for review, but no historical outcome is read until both gates clear.
