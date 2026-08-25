# Derivatives context capture v1

Status: capture-only, collecting after deployment. This contract does not
change a strategy, authorize a trade, or claim alpha.

## Purpose

Schurfer's one-minute momentum bars already preserve price, taker flow, OI,
best bid/ask, exchange identity, and point-in-time timestamps. They did not
preserve the contemporaneous mark price, index price, current/predicted
funding rate, or next funding boundary. Those values cannot be reconstructed
honestly after retention or used later without look-ahead if they were never
captured.

This PR adds those four values to the existing
`timeseries.bybit_momentum_bars_1m` row for both active capture venues:

- Bybit reads them from the already-subscribed linear ticker stream.
- Binance uses the USD-M combined `@markPrice` stream at its default
  three-second cadence. The one-second variant is deliberately not used for
  a one-minute storage grain.

Primary documentation:

- [Bybit derivatives ticker](https://bybit-exchange.github.io/docs/v5/websocket/public/ticker)
- [Binance USD-M mark-price stream](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Mark-Price-Stream)

## Stored contract

Migration `0037` adds nullable columns only. Historical rows remain `NULL`:
missing context is never rewritten as zero funding, zero basis, or a known
negative observation.

Every new row written by the upgraded collector carries
`derivatives_context_version='derivatives_context_v1'` and:

- `mark_price`, `mark_price_event_at`, `mark_price_observed_at`;
- `index_price`, `index_price_event_at`, `index_price_observed_at`;
- `funding_rate`, `funding_rate_event_at`, `funding_rate_observed_at`;
- `next_funding_at`, `next_funding_event_at`,
  `next_funding_observed_at`;
- `derivatives_observed_this_minute`;
- `derivatives_complete`.

`event_at` is exchange message time. `observed_at` is the local receive time
of the message that last changed that value. For `next_funding_at`, the value
is the exchange's announced settlement boundary; its accompanying
`*_event_at`/`*_observed_at` fields describe when that announced value was
seen, not when settlement occurred.

State may carry into the next minute with its original timestamps. Therefore
`derivatives_observed_this_minute=false` does not by itself mean the state is
missing. Consumers must apply their own freshness rule to the value-specific
timestamps.

`derivatives_complete=true` requires all four value/provenance tuples to be
present and the derivatives feed to have remained uninterrupted for the
whole bar. A reconnect, bounded-queue loss, or per-symbol silence mark is
sticky for the affected bar. The next complete bar is possible only after a
valid observation restores the feed.

The legacy `complete` column is unchanged. Existing strategies do not
silently acquire a new quality requirement; a future context-aware contract
must explicitly require both its version and `derivatives_complete=true`.

## Venue semantics

Funding here is an advertised rate snapshot, not an authenticated funding
ledger entry and not proof that a position paid or received funding. Its sign
must be modeled directionally. A future report must align snapshots to
`next_funding_at` and keep missing settlements unresolved rather than treating
them as zero.

Mark/index basis can support point-in-time regime, dislocation, and squeeze
features. It cannot by itself identify forced liquidations. OI falling while
price rises remains only a liquidation-like footprint until an independently
captured liquidation event supports that claim.

## Integrity and observability

- The Go engine rejects non-finite prices/rates, non-positive mark/index
  prices, missing envelope timestamps, and partial value/provenance tuples.
- A PostgreSQL `BEFORE INSERT OR UPDATE` validation trigger enforces
  version/value ownership, finite values, tuple provenance, and the
  full-value requirement for `derivatives_complete=true`. A trigger is used
  because the deployed TimescaleDB release cannot propagate a new CHECK
  constraint across already-compressed chunks without an internal error;
  decompressing the full history solely for DDL would create an unnecessary
  disk-risk window.
- Health schema v3 publishes accepted, invalid, out-of-scope, reconnect,
  timeout, per-symbol gap, and last-observation metrics.
- Binance detects silence per symbol. Activity from other symbols on the
  same combined-stream shard cannot make a stale symbol look healthy.
- A semantically invalid Binance `markPriceUpdate` is delivered through the
  same bounded FIFO as valid updates, counted, and immediately marks the
  affected bar interrupted. It never advances freshness; an invalid update
  without a usable symbol conservatively interrupts the whole subscribed
  derivatives universe.
- The writer's payload hash includes every new field, so a same-primary-key
  retry with different context is reported as a mismatch.

## Storage and capacity

No new hypertable and no additional rows per minute are created. The existing
row grows by a version, 12 float/timestamp values, and two booleans. The health
projection uses a conservative 1280-byte planning estimate based on the old
measured 1143.6-byte row plus structural allowance. This is not a benchmark.

After deployment, record actual hot and compressed deltas at 24 and 72 hours,
including `pg_total_relation_size`, chunk compression, queue drops, process
RSS/CPU, and host disk. The measured canary replaces the planning estimate.

## Explicitly out of scope

- No strategy or execution-path changes.
- No parameter selection or historical backfill.
- No CEX wallet inflow/outflow or on-chain entity labeling.
- No liquidation capture. Bybit all-liquidation and Binance force-order data
  have different append-only, deduplication, censorship, and volume semantics;
  they require a separate versioned event table and PR.
- No raw mark-price tick archive; the retained grain is last known state per
  minute with point-in-time provenance.

## Deployment and validation

This is a cross-service schema change. Apply migration `0037` before starting
upgraded writers, then rebuild/restart `collector`, `momentum-capture`, and
`momentum-capture-binance`. A code-only service deploy is not sufficient.

Post-deploy gates:

1. Alembic head is `0037`; all new columns and the validation trigger exist.
2. All three containers are running with zero restarts and no writer errors.
3. Both venue health hashes advance; invalid/drop/gap counters remain zero
   after startup stabilization.
4. Recent rows for both venues have the expected context version, high
   `derivatives_complete` coverage, finite values, and event/observed
   timestamps in causal order.
5. Existing row throughput remains one row per symbol-minute and measured
   storage/CPU remain within the existing host gates.
