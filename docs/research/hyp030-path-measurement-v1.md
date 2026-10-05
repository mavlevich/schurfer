# HYP-030 bounded path measurement v1 (protocol draft)

Status: **design review 1 folded in (2026-10-05); nothing runs until it is approved.** It implements
option 2 of [hyp030-burst-design-v1](hyp030-burst-design-v1.md) under that design's
boundaries. It registers no hypothesis and sends no order.

## Question

How fast can a new path act on the 1-minute burst rule on Bybit, and what does it
actually cost to enter and exit at those moments?

- **Latency** is measured and visible now.
- **Cost** is recorded sealed. It is read only after HYP-012 v2 is terminal, and then
  only by a study registered after that.

## The path

1. **Stream.**
   - `streamrt`, the shared runtime of the realtime capture design, with the Bybit
     codec: public linear trades, 200 instruments per connection, pings every 20 s,
     liveness, jittered reconnects, fresh session ids.
   - Lifecycle events (Connected, Disconnected, Overflow) travel in-band.
2. **Universe.**
   - Bybit linear USDT perpetuals in `Trading` status at the run start, from the
     existing `bybit.Adapter.FetchUniverse` with its exclusion counts.
   - Frozen for the run with its sha256; new listings wait for the next run.
3. **Rule, on its own data contract `burst_trade_bars_v1`.**
   - `burstengine` with `HYP030()`: the design's thresholds, cooldown and median window.
   - The data contract differs from the research. The research bars took OHLC from the
     ticker's last price; these take it from trades:
     - Open and Close are the first and last trade by exchange time, then the venue's
       sequence, then arrival;
     - a minute without trades is a bar with zero turnover and the previous close.
   - So this is a new signal contract. The historical flow (about 15 a day) and the
     power planning are approximations for it, and its firings will not match the
     research list one for one.
   - A minute is decided only once the connection is confirmed delivered past its end
     plus 250 ms plus the 2 s lag allowance (an in-band heartbeat of that connection; a
     break found later starts 2 s before its last frame), and every queued event is
     applied first. Decisions therefore come at least about 2.3 s after the bar's end.
   - Heartbeats are sent only for an acknowledged subscription and are never a state
     event: a dropped heartbeat leaves no gap. If a state event is dropped, the runtime
     re-confirms a live subscription right after the overflow report, and a heartbeat
     also closes the gap. While a gap is open the clock alone finalizes, and the minute is
     incomplete. So a break detected late still removes the minutes after the
     connection's last frame. Until then a trade of the minute still
     updates it; after that it is late, counted and never added.
   - A repeated trade id is dropped before it touches price or turnover. Ids are kept
     per open minute and freed at finalization; a repeat of a finalized minute is
     already refused as late. A trade without an id is refused.
   - **Completeness starts only when the venue has acknowledged as many subscriptions
     as were requested** (a count match: acknowledgements name no instrument), within
     10 s or the session restarts. Gap intervals run:
     - from a disconnect's last received frame, moved back by a 2 s lag allowance, to
       the next acknowledged subscription;
     - over an overflow, the union of the dropped trades' exchange times and its
       receive-time span (moved back by the same allowance). If a lifecycle event was
       dropped too, the gap stays open until the next acknowledged subscription.

     Completeness is evaluated against the gaps known at the decision, for the bar,
     its previous bar and every bar of the median window. A gap found after a minute
     was finalized still removes that minute.

     A bar that overlaps a gap is incomplete, empty minutes included. Incomplete bars
     never fire and never enter the median.

4. **At a signal:**
   - one REST order-book snapshot: `GET /v5/market/orderbook`, linear, 50 levels;
   - the funding rate and the next funding time from the ticker at entry;
   - 60 minutes after the bar's end, a second book snapshot for the exit;
   - after the exit, the **settled funding** of every settlement time the hold crossed,
     from the funding history (`/v5/market/funding/history`). The rate at entry alone is
     not the charge.
5. **Never:** an order, a write to the v2 or HYP-015 tables, or a change to the existing
   capture, watch or paper paths.

## What is recorded

**Visible now: counters and times only.** They go to Redis health (the whole snapshot
replaced atomically, named by its run id) and to a daily summary per run. Health leaves
the event loop as snapshots, so a slow Redis never delays events.

- Signals per day; suppressed by a gap; blocked (more than 3 open at once, counted
  against the design's portfolio limit).
- Latency stages per signal, as durations:
  - bar end to the bar's last trade received;
  - bar end to the rule's evaluation;
  - evaluation to the book request sent;
  - request to response received;
  - the book's exchange timestamp minus the bar end.
- Quote failures and timeouts; exit snapshots missed.
- Runtime and engine counters: frames, bytes, sessions, disconnects, dropped events,
  late trades, empty and incomplete bars.
- Process CPU and peak resident memory.

**Sealed until the blind window ends:**

- every signal's instrument, return, turnover, median and prices;
- both book snapshots;
- the funding rate at entry and the settled funding of any crossed settlement.

These go to gzip NDJSON segments, one per UTC day per run, created new and never
appended to, mode 0600. Each closed segment gets a manifest (line count, sha256). A run
killed without closing leaves its segment without a trailer; the next start writes that
segment an "unterminated" manifest with the lines still readable. A record that cannot
be sealed stops the run with an error: a measurement that cannot keep its records does
not go on, and nothing counts as completed unless sealed. Nothing opens them before v2 is terminal. A test asserts
that the health record and the daily summary carry no market value and no instrument
name.

## Duration and limits

- **Duration:** 28 days from the start, then the process stops on its own.
- **Container:** its own, with CPU and memory limits set from a local replay benchmark
  before start (frames per second as in the probe, peak RSS, and the evaluation
  latency).
- **Disk:** about 15 signals a day with two 50-level books each is small. The trade
  stream is not stored.

## Implementation and a local check

- **Code:** `cmd/burstprobe` (process), `internal/burstprobe` (REST snapshots, sealed
  daily NDJSON with manifests, counters-only health), `internal/burstengine`,
  `internal/streamrt`.
- **Operations:** `make prod-burstprobe-start | -stop | -health`; compose profile
  `burst-probe`, no restart, 256 MB and 0.5 CPU.
- **Local check on live Bybit (counters only, 2026-10-05, 150 s):**
  - 3 connections, 53 acknowledgements for 53 subscribe frames;
  - no drops, disconnects, duplicates or parse errors; about 300 trades/s;
  - the first minute after the subscription marked incomplete, as designed;
  - peak RSS about 25 MB after moving the id deduplication into the open minutes (an
    earlier ring of recent ids per instrument grew past 100 MB).
- No sealed file was opened.

## Readout (later, separately registered)

After HYP-012 v2 is terminal, a readout registered at that time may open the sealed
files, as historical data, with v2's closed windows excluded mechanically. It would
report the costs of entering at the measured latencies: half-spread, the depth impact
at USD 50, fees, funding over the hold, and the exit costs. Against those costs it would
check the 41 bps scenario.

The latency part can be read at any time, because it holds no market value.

## Review answers (design review 1)

- **Order books:** a REST book snapshot is enough for a first measurement of quoted
  costs. It is not evidence of execution or profitability.
- **Universe:** a run-frozen universe is acceptable.
- **Duration:** 28 days is reasonable for latency and collection quality.
