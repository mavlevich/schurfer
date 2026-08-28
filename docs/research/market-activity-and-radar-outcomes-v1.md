# Market activity and radar outcomes v1

Status: frozen discovery protocol. Neither report is a strategy or a trading
authorization.

## Why this exists

The project had accumulated three different things under the word "pump":

1. `app.pump_events`, whose detector already selects roughly +20% moves;
2. prospective WATCH decisions, which exist whether or not a later pump is
   labelled; and
3. Telegram-style CEX activity observations such as "10% of 24h volume bought
   in five minutes".

Using pump events as both the sample and the outcome made a +20% label circular
and removed the denominator of ordinary opportunities. These reports instead
start from point-in-time signals and resolve the market path afterward.

The broader `2026-08-14` through `2026-08-28` context was inspected during
discovery. It is permanently discovery-only. The first reproducible runs should
use the mature half-open window `2026-08-18T00:00:00Z` through
`2026-08-27T00:00:00Z`; outcomes need the following 24 hours. No row at or
before the viewed `2026-08-28` cutoff may later be presented as untouched
confirmation evidence.

## Shared outcome contract

- Exact identity: `(exchange, market_type, symbol, capture_version)`.
- Entry: a native one-minute bar open strictly after the signal was knowable.
  For a closed burst bucket this is `bucket_start + 1 minute`. For WATCH it is
  the first full UTC minute after recorded `decision_at`.
- Primary outcome: favorable excursion of at least 25% during the following
  1,440 exact one-minute bars.
- Missingness: all 1,440 price-complete minutes are required. A gap is
  unresolved, never a loss, win, zero return or nearest-bar substitute.
- Control: same exact instrument and UTC time shifted by up to seven days,
  farther than 24 hours from any signal of the same family. One control cannot
  be reused by two signals.
- Inference: paired binary hit-rate difference, whole-symbol cluster bootstrap,
  95% interval. CEX buy/sell are one family with Holm correction. WATCH long is
  a separate one-candidate family.
- Readiness: at least 60 matched pairs, 20 assets and two distinct UTC weeks.
- Promotion: each report may nominate at most one forward candidate. A
  nomination only freezes the next hypothesis; it does not start paper/live
  trading.

OHLCV high/low answers whether the market moved, not whether Schurfer could
fill. Costs, spread, impact, funding and exit execution are intentionally not
claimed here. Any candidate must next enter an untouched forward quote-capture
shadow with explicit capacity and after-cost economics.

## HYP-016: CEX activity

Signal family, fixed before the first run of the new report:

- `buy`: five complete one-minute buckets contain buy taker notional equal to
  at least 10% of the instrument's own strictly complete trailing 24h total
  taker notional; candidate direction is long.
- `sell`: symmetric sell condition; candidate direction is short.
- trailing 24h volume floor: USD 50,000;
- independent episode refractory period: 60 minutes.

Only these two registered directions belong to the primary family. Alternative
30-second/2-minute/10-minute/15-minute windows, threshold tuning, venue mixing,
BTC filters and OI combinations are later discovery families, not post-hoc
ways to rescue this result.

This is the research foundation for a future CEXTrack-like product. We should
not build Telegram alerts first: alerts would create an attractive feed without
knowing its false-positive rate. If one direction survives forward evidence,
the live capture message can report exact venue/market, direction, five-minute
notional, share of trailing 24h volume, price move, liquidity, identity and data
quality while preserving every emitted observation for evaluation.

## HYP-017: WATCH radar outcome

Signal family:

- every persisted `decision_status='watch'`, `quality_ready=true`,
  `raw_qualified=true` decision from the immutable
  `momentum_flow_watch_v1` contract;
- one registered direction: long;
- signal time is recorded `decision_at`, not the detector's minute bucket and
  not a later pump-event timestamp.

This directly tests the preliminary observation that WATCH may detect
multi-hour heating. It does not join to `pump_events` and therefore does not
inherit their +20% selection rule. Median time-to-hit, favorable excursion and
adverse excursion are diagnostics; the only primary metric is the paired
25%-within-24h hit-rate difference.

## Commands after merge

```bash
# Offline replica or frozen extract only; do not point this full-universe scan
# at the production primary.
DATABASE_URL='<offline-postgres-url>' make cex-activity-discovery-report ARGS='--since 2026-08-18T00:00:00Z --until 2026-08-27T00:00:00Z --format json'

make prod-radar-outcome-discovery-report ARGS='--since 2026-08-18T00:00:00Z --until 2026-08-27T00:00:00Z --format json'
```

The WATCH production target refuses to start without the normal report memory
headroom. Each SQL transaction is read-only and has a five-minute statement
timeout. The CEX scan deliberately has no production target after its first
frozen run proved too I/O-heavy for the primary database; HYP-016 remains parked
until the denominator is materialized or an offline replica is available.
