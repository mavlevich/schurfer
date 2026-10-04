# Realtime market capture: venue adapters, streaming triggers, shadow scanner v1

Status: **DRAFT for design review.** No code. Runs as a shadow beside today's
pipeline; changes nothing that HYP-012 v2 or HYP-015 depend on.

## Why

Today's detection is late by design, and the measurements in
[edge-loss-decomposition-v1](../research/edge-loss-decomposition-v1.md) show where:

- the pump scanner polls 16 exchanges over REST through ccxt;
- it recreates every client (and reloads every market catalogue) each cycle;
- its real cycle is 101 s;
- it fires at a 24h change of +20%.

59% of pumps happen on one venue only, and MEXC is first in 68% of the pumps it sees.
The owner can trade MEXC futures. MEXC keeps no historical trade tape (the MEXC
feasibility audit), so the only way to measure seconds-level early detection there is
to start recording now; every week we wait is data we will never have.

What we want is one architecture where adding a venue is an adapter, not a new
service, and where an event becomes a signal in milliseconds, with every timestamp kept
so latency is measured, not assumed.

## Scope and non-goals

- **In:** venue adapters (MEXC first, then LBank and BingX), a shared streaming
  runtime, normalized events, an in-process trigger engine, recording within a disk
  budget, latency and completeness telemetry.
- **Out, each a separate decision:**
  - order execution (it waits for a registered signal and MEXC API trading permission);
  - replacing the old scanner (only after HYP-012 v2 is terminal);
  - reading outcomes of captured events before HYP-012 v2 is terminal (the moves are
    shared with that cohort).

## Builds on what exists

`apps/collector` already has:

- the venue-agnostic capability interfaces (`momentumsource`: universe, trades,
  ticker, open interest, with an `Envelope` carrying `EventAt`, `ReceivedAt` and
  `SessionID`);
- the fail-closed capability matrix (`momentumvenue`, where MEXC is `not_audited`);
- websocket liveness helpers (`wsstream`);
- the Bybit and Binance adapters.

The design extends these; it does not start a parallel framework.

## Architecture

```
venue websocket(s) ──> adapter (parse, normalize) ──> runtime (sharded connections,
     reconnect, sequence and clock checks, bounded queues)
         │
         ├──> trigger engine (per-instrument ring buffers, 1 s buckets) ──> signal
         │        └──> NATS JetStream `signals.*` + durable signal row (Postgres)
         ├──> recorder (1 s bars for all, raw trades around signals) ──> history archive
         └──> telemetry (rates, lags, gaps, uptime) ──> Redis health + daily summary
```

1. **Adapter, one per venue.** Each adapter implements only the capabilities it has,
   from the `momentumsource` interfaces plus a new `BookTopSource` for best bid and
   ask:
   - **Universe:** native contract ids from the venue's own contract list, snapshotted
     and versioned; never `base + "/USDT:USDT"` string building.
   - **Trades:** with the venue's trade id and taker side.
   - **Ticker or book top:** best bid and ask.
   - **Open interest:** where the venue streams it.

   It parses the venue's message format into the canonical events and declares its
   limits (subscriptions per connection, ping protocol, rate limits) as data. Adding a
   venue means: an adapter, its capability-matrix entry with probe evidence, and
   fixture tests.

2. **Streaming runtime (shared).** It does the connection work every adapter needs:
   - shards instruments across connections within the venue's limits;
   - venue-specific ping and liveness;
   - reconnect with jittered backoff and resubscribe;
   - a new session id per dial;
   - sequence-gap detection where the venue gives sequences;
   - clock-offset estimation against the venue's server time (plus chrony on the
     host);
   - bounded queues that never block a reader. An overflow is counted, never silent.
3. **Canonical event.** The existing `Envelope` plus `Sequence` and `ProcessedAt`, with
   a schema version.
   - On the hot path, events stay in process (Go structs). NATS JetStream is the
     fan-out to other consumers.
   - Encoding starts as versioned JSON; protobuf only if the measured throughput needs
     it.
4. **Trigger engine.**
   - It keeps per-instrument ring buffers of 1-second buckets (price, buy and sell
     notional, trade count) for the last hour, and evaluates registered trigger
     definitions on every bucket close. The first versions are the T1/T2 families of
     the decomposition study.
   - A signal carries the triggering event's times and the engine's own, so
     event-to-signal latency is measured on every signal.
   - Signals go to JetStream and to a durable Postgres row. Postgres is never on the
     per-event path.
5. **Recorder.**
   - Writes 1-second bars for every instrument, and raw trades and book tops from
     30 minutes before to 4 hours after each signal.
   - Writes as hourly files handed to the history-archive engine (`history_archive`)
     for the Storage Box. The server keeps a bounded hot window.
   - Full raw tape for a subset only if the disk budget (the storage-budget PR, about
     2026-10-12) allows it.
6. **Telemetry.**
   - Per venue and connection: message rate, `ReceivedAt - EventAt` percentiles, gaps,
     reconnects, queue drops and uptime.
   - Per signal: event-to-signal latency.
   - Health in Redis, as the other workers do; a daily summary file next to the
     disk-growth readings.

## Performance targets (measured, not assumed)

- Event received to signal emitted: p99 < 250 ms in process.
- Reconnect gap: recorded with its bounds. An instrument with a gap in a trigger window
  produces no signal: the trigger fails closed.
- CPU and memory limits are sized from a probe. The service runs in its own container
  with its own limits, not under the analytics container's single core.

## Rollout

1. **MEXC probe** (read-only, 30 minutes): connect, subscribe to every USDT perpetual,
   count messages, measure lags and limits. It stores counts and timings only, never
   values. This gives the sizing and the capability-matrix evidence.
2. **MEXC adapter, runtime, trigger engine, recorder and telemetry**, in shadow:
   - fixture tests and a fake websocket server (reconnect, gaps, overflow);
   - a replay benchmark;
   - deploy as its own service.
3. **LBank and BingX adapters**, each with its probe and matrix entry.
4. **After HYP-012 v2 is terminal (2026-10-31 checkpoint at the earliest):** analysis
   of the recorded events against the decomposition study's decision rule, and the
   old scanner's fixes or replacement.
5. **MEXC execution adapter:** a separate, gated PR after a registered signal and a
   confirmed API trading permission.

## Risks

- **MEXC API terms:** public websocket limits and futures API trading permission must
  be confirmed by probe and by the owner's API key settings.
- **Disk:** the recorder is budgeted from the measured series; until then it records
  1-second bars and signal windows only.
- **Host load:** the host runs near 4 load on 4 cores. The daily readings and the probe
  decide whether a larger server is needed (an owner decision, about EUR 10-20 a month).

## Questions for review

1. Is extending `momentumsource` with `BookTopSource` and a shared runtime the right
   seam, or should the runtime live in its own package used by the existing Bybit and
   Binance adapters too (a later migration)?
2. Should JetStream carry every canonical event (replayable, heavier) or only signals
   (lighter), with recording done in process?
3. Recorder retention on the server before the archive takes over: 3 days?
