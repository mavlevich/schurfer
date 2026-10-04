# Realtime market capture: venue codecs, streaming triggers, one MEXC canary v1

Status: **DRAFT, design review 1 folded in.** No code. Proposes one change to the
agreed order, which needs the owner's decision (below).

## Why

Today's detection is late by construction (see the measurements in
[edge-loss-decomposition-v1](../research/edge-loss-decomposition-v1.md)):

- REST polling of 16 exchanges through ccxt;
- clients and market catalogues rebuilt every cycle;
- a 101 s real cycle;
- a +20% 24h trigger.

The Bybit momentum line adds a fixed 30 s settle and two polling hops before an entry.

59% of pumps happen on one venue only, MEXC is first in 68% of the pumps it sees, and
the owner can trade MEXC futures (API trading permission still to be confirmed). MEXC
keeps no historical trade tape, so seconds-level data on MEXC exists only if we record
it.

## Proposed change to the agreed order (owner decision)

The ROADMAP allows **one** new bounded collector, after the 2026-10-31 direction
decision and the source and budget gates. This design proposes to move that single
slot forward to a **MEXC canary**: one venue and a registered universe, recording
before 2026-10-31. Everything else stays where it is:

- LBank and BingX are later conditional tasks;
- the Gate collector (PR 6) is still decided on 2026-10-31.

The canary may start before 2026-10-31 only under the sealed protocol below. If the
owner does not accept that protocol, the canary starts after 2026-10-31 as the ROADMAP
slot, and this design is otherwise unchanged.

**Sealed protocol (before HYP-012 v2 is terminal):**

- **What may be viewed:** operational counters only: message and byte rates,
  receive-minus-event lag percentiles, reconnects, gaps, queue overflow counts, bytes on
  disk.
- **What is never shown:** no code path prints, logs, plots or summarizes a price,
  size, side or signal content. The health endpoint and the daily summary carry
  counters only; a test asserts this.
- **Signals:** the trigger engine runs; signal rows are written, sealed, and counted,
  not inspected.
- **Status of the data:** exploration only. After v2 is terminal it may be read for
  discovery, with the v2 closed-window exclusions honoured. It can never be the
  confirmatory sample: a signal chosen from it needs a new, untouched forward period.

This differs from the PR 3 probe rule ("discarding values after receipt does not count
as blind") on purpose. That rule governs research probes whose operator sees responses;
here no one sees values until the blind ends. Whether that is acceptable is the
owner's and the reviewer's call.

## Scope and non-goals

- **In:** a shared streaming runtime, a MEXC codec, an event-driven trigger engine, a
  bounded recorder with a file-archive contract, latency and completeness telemetry.
- **Out:**
  - order execution;
  - changes to the old scanner, watch or paper (until v2 is terminal and HYP-015 is
    read);
  - LBank and BingX;
  - migrating the existing Bybit and Binance sources.

## Architecture

```
MEXC websocket(s) ─> runtime (owns connections: shards, ping, reconnect, resubscribe)
      │  frames
      ▼
   codec (subscribe/ping frames, parse, units)  ─> canonical events + lifecycle events
      │                                                (in-band, same ordered stream)
      ▼
   trigger engine (incremental per event) ─> signal ─> durable signal row (+ JetStream)
      │
   recorder (rolling raw segments, pinned windows) ─> segment files ─> archive catalog
   telemetry (counters only) ─> Redis health, daily summary
```

1. **The runtime owns every connection; a venue is a codec.**
   - The runtime dials, shards instruments within the codec's declared limits, sends the
     codec's subscribe and ping frames, detects liveness, reconnects with jittered
     backoff, resubscribes, and assigns a session id per dial.
   - A codec builds subscribe, unsubscribe and ping frames, parses frames into canonical
     events, and declares limits and unit rules. It never dials.
   - **Lifecycle events** travel in-band on the same ordered stream as data:
     `Connected`, `Subscribed`, `Disconnected`, `Resubscribed`, `SequenceGap` and
     `Overflow`. So a consumer learns of a break at once, not at the next trade.
   - The existing `momentumsource` sources (Bybit, Binance), which manage their own
     connections, are untouched; migrating them is a separate later decision.
2. **Completeness is part of the data.**
   - After any `Disconnected`, `SequenceGap` or `Overflow`, the affected instruments are
     marked **incomplete** for the trigger engine's longest lookback.
   - No signal fires on an incomplete window, and recorded segments carry the gap
     bounds.
   - A counter alone is not enough: the incomplete state is what blocks.
   - Queues are bounded and never block a reader. An overflow drops data and marks the
     instruments incomplete; it is never silent.
3. **MEXC codec contract** (from the official contract API documentation, every point
   confirmed by the probe before use):
   - **Trades:** subscribe to deals with `compress=false`. MEXC aggregates by default;
     an aggregated stream is not comparable on trade counts and is refused.
   - **Fields:** price, volume in contracts, taker direction (buy or sell, by the
     documented code), and the exchange timestamp in ms.
   - **Units:** base quantity = contracts x `contractSize` from the contract-detail
     snapshot; notional (USDT) = price x base quantity. The contract snapshot is
     versioned with the universe.
   - **Times:** `EventAt` is the exchange time; `ReceivedAt` is the local receive time
     with the host clock disciplined by chrony; `ProcessedAt` is set after parsing.
     None substitutes for another.
   - **Universe:** native contract ids from the contract list, registered and versioned
     before collection. No symbol string building.
4. **Trigger engine: evaluated on every event, incrementally.**
   - Per instrument, a ring of 1-second buckets plus the open partial bucket. Every
     trade updates the running window sums in O(1) and re-checks the registered
     conditions at once, so a crossing never waits for a bucket to close.
   - Latency is measured in three segments, on every signal:
     - receive to processed;
     - condition crossing to signal emitted;
     - signal to durable write.
   - The target, measured and not assumed: p99 under 50 ms for the first two segments
     on the canary universe.
5. **Recorder: a bounded pre-buffer and pinned windows.**
   - Raw canonical events of every instrument are written continuously to rolling local
     segments of 10 minutes. That is the pre-buffer: a signal's 30 minutes of history
     already sit on disk, not in RAM.
   - A signal pins the segments covering 30 minutes before to 4 hours after it.
     Overlapping windows merge; unpinned segments older than the pre-buffer are
     deleted.
   - **Caps** (sized from the probe and the storage-budget PR):
     - process RSS;
     - total local raw bytes;
     - pinned bytes per day.
   - **At a cap:** new windows are not pinned. A signal still fires, but its row says
     "window not recorded". The pre-buffer keeps rolling.
   - **Mass-signal load:** the probe's message rate times the worst plausible number of
     simultaneous windows gives the bound. If pinned windows would approach the full
     tape, the full tape for the canary universe is the honest alternative, and the
     budget decides.
   - Separately, 1-second bars for every instrument (small) are kept for the analysis.
6. **File-archive contract** (new; today's archive engine only exports PostgreSQL
   tables).
   - Each closed segment is a zstd NDJSON file with a manifest: venue, codec and
     contract versions, universe version, time coverage, line count, gap bounds and
     SHA-256.
   - A migration (0060) adds a catalog unit `segment` with a coverage range. The
     existing archive and verify steps carry segments: the archive takes the files, and
     verify extracts and rechecks the SHA-256, the line count and the manifest.
   - A segment's local file is deleted only once its catalog row is `verified`, never by
     age. A restore check reads segments back into the analysis cache.
7. **Telemetry: counters only** (see the sealed protocol): rates, lags, reconnects,
   gaps, overflows, incomplete instrument-minutes, bytes, and the three latency
   segments. They go to Redis health, as the other workers do, and to a daily summary
   file.

## Technology choices (agreed with the reviewer)

| Area                | Choice                                                                                             |
| ------------------- | -------------------------------------------------------------------------------------------------- |
| Hot path            | Go, structs in memory, bounded queues; no PostgreSQL and no serialization between codec and engine |
| JetStream           | Signals and quality events first; the full tape only if a proven need appears                      |
| Historical analysis | Verified Parquet cache plus DuckDB; the initial conversion measured separately                     |
| Polars              | Only for a remaining measured bottleneck                                                           |
| JSON or protobuf    | Start with the current parser; change only after a whole-process profile                           |
| msgspec             | Not on the Go path; a Python parser changes only by its own profile                                |
| uv, ruff, UI work   | Already accepted or deferred on their own terms; they do not speed this up                         |

The largest expected gain is removing deliberate waits and extra hops, not parsing
speed. The full path is measured first, then its largest part is optimized.

## Rollout

1. **MEXC probe** (30 minutes, sealed): message and byte rates, lags, connection
   limits, `compress=false` behaviour, and the field semantics against the
   documentation. It gives the sizing and the capability-matrix evidence. It stores
   counters only.
2. **MEXC canary** (runtime, codec, trigger engine, recorder, file archive, telemetry):
   fixture tests, a fake websocket server (reconnects, gaps, overflow, mass signals),
   a replay benchmark of the three latency segments, and its own container and limits.
3. **After HYP-012 v2 is terminal:**
   - discovery reads of the canary data under the exclusions;
   - the old scanner's fixes or replacement;
   - the analytics container's CPU limit.
4. **After a registered signal and confirmed API trading permission:** a MEXC execution
   adapter, separately gated.
5. **LBank and BingX:** later, each conditional on its own probe and the canary's
   results.

## Questions for review

1. Is the sealed protocol acceptable for starting the canary before 2026-10-31, or does
   it start on 2026-10-31?
2. Pre-buffer sizing: are 10-minute segments with a 30-minute pre-buffer and 4-hour
   windows a sensible start?
3. Is `segment` as a third catalog unit the right extension, or should segments get
   their own catalog?
