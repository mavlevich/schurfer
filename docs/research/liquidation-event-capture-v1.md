# Public liquidation event capture v1

Status: implemented, disabled by default, requires a bounded production probe.

This dataset records what public venue feeds actually expose. It is research
infrastructure, not a new strategy and not permission to change paper or live
trading.

## Source contracts

- Bybit `allLiquidation.{symbol}` documents all liquidations, a 500 ms push
  cadence, liquidated **position side**, executed size, and bankruptcy price:
  <https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation>.
  Rows are labelled `complete_stream`, but only minutes with a complete durable
  heartbeat may support a no-event or complete-coverage claim.
- Binance `!forceOrder@arr` documents at most the latest liquidation order for
  each symbol in every 1000 ms interval. It is a censored snapshot, not a tape:
  <https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market>.
  Every row and heartbeat is permanently labelled
  `latest_per_symbol_1000ms`.

The bounded 2026-08-25 smoke also observed production USD-M frames without the
post-CM `st` scope tag described by the current catalog. Such a frame is
accepted only when its symbol belongs to this process's frozen, strict USD-M
catalog. It is labelled `binance_usdm_no_scope_tag_v1`, while explicit `st=1`
frames are labelled `binance_merged_um_v1`; the missing-tag acceptance counter
is durable in health and coverage heartbeats. Explicit `st=2` COIN-M frames
remain out of scope. Consumers must not silently pool source-contract variants.

For Bybit, `Buy` means a long position was liquidated and `Sell` means a short
position was liquidated. For Binance, the force-order side is the closing
order: `SELL` closes a liquidated long and `BUY` closes a liquidated short.
Tests pin both mappings.

## Storage and identity

`timeseries.liquidation_events` is append-only and keeps:

- exchange, native market id, market type, capture version, source-contract
  variant, and coverage kind;
- normalized liquidated position side;
- exchange event time, exchange publication time, local receive time, and DB
  persistence time;
- venue-native quantity/price fields plus explicit `quantity_unit`;
- an estimated quote notional whose basis remains auditable from native fields;
- the individual raw JSON item and its SHA-256;
- a deterministic source-event key that deduplicates an identical delivery
  across reconnects.

Neither venue exposes a native liquidation-event ID in these public payloads.
The key therefore does not claim to recognize a venue event that is re-batched
or rewritten with different envelope metadata. Bybit includes the publication
timestamp and item index in the source identity to avoid silently collapsing
two distinct same-millisecond liquidations with identical visible fields.

`ON CONFLICT DO NOTHING` is accepted only when the stored payload hash matches.
A same-key/different-payload collision is exposed as a health failure, never a
harmless retry.

No current canonical cluster is stamped into the raw event. Instrument identity
changes over time; research joins the native market id through a point-in-time
identity dataset instead of rewriting historical source truth.

## Durable coverage

`timeseries.liquidation_capture_heartbeats_1m` distinguishes:

- a complete connected minute with zero observed events;
- a minute with an event;
- a missing heartbeat;
- a heartbeat made incomplete by startup, reconnect, invalid input, pending or
  failed persistence, queue loss, or a writer integrity mismatch.

Heartbeats contain process/session identity, expected and connected connection
counts, monotonic source/writer counters (including Binance missing-scope-tag
acceptance), and the venue coverage kind. They do not upgrade Binance's
censored feed into complete coverage.

## Runtime and safety

One binary is deployed as two isolated, opt-in Compose services:

- `liquidation-capture-bybit`
- `liquidation-capture-binance`

Each freezes its venue's strict USDT linear-perpetual universe at startup. The
event writer is bounded and never evicts an older ledger row: a full queue
rejects the new event and marks the minute incomplete. Health is published at
`market:liquidationcapture:health:{exchange}` with a TTL.

Before enabling either production profile:

1. apply migration `0038`;
2. start one venue only;
3. verify health, zero queue drops, zero invalid events, zero payload-hash
   mismatches, expected connection count, and at least two complete heartbeats;
4. compare a bounded sample with the venue's raw frames;
5. only then start the second isolated venue.

No profitability claim is allowed until an immutable event-study dataset joins
these events to point-in-time price/OI/funding/flow context, uses only admissible
coverage, charges executable costs, and evaluates pre-registered forward
horizons out of sample.
