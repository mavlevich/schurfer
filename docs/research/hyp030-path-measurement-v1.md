# HYP-030 bounded path measurement v1 (protocol draft)

Status: **draft for design review; nothing runs until it is approved.** It implements
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
3. **Rule.**
   - `burstengine` with `HYP030()`: the rule of the design, unchanged.
   - Bars by exchange time. Minutes without trades are bars with zero turnover and the
     last price.
   - Evaluated 250 ms after the bar's end.
   - Gaps make bars incomplete and block signals.
4. **At a signal:**
   - one REST order-book snapshot: `GET /v5/market/orderbook`, linear, 50 levels;
   - the funding rate and the next funding time from the ticker;
   - 60 minutes after the bar's end, a second book snapshot for the exit.
5. **Never:** an order, a write to the v2 or HYP-015 tables, or a change to the existing
   capture, watch or paper paths.

## What is recorded

**Visible now: counters and times only.** They go to Redis health and to a daily
summary.

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
- the funding values.

These are appended to daily NDJSON files (gzip), mode 0600, each closed day with a
manifest (line count, sha256). Nothing opens them before v2 is terminal. A test asserts
that the health record and the daily summary carry no market value and no instrument
name.

## Duration and limits

- **Duration:** 28 days from the start, then the process stops on its own.
- **Container:** its own, with CPU and memory limits set from a local replay benchmark
  before start (frames per second as in the probe, peak RSS, and the evaluation
  latency).
- **Disk:** about 15 signals a day with two 50-level books each is small. The trade
  stream is not stored.

## Readout (later, separately registered)

After HYP-012 v2 is terminal, a readout registered at that time may open the sealed
files, as historical data, with v2's closed windows excluded mechanically. It would
report the costs of entering at the measured latencies: half-spread, the depth impact
at USD 50, fees, funding over the hold, and the exit costs. Against those costs it would
check the 41 bps scenario.

The latency part can be read at any time, because it holds no market value.

## Review questions

1. Is a REST book snapshot at the signal adequate, or should the path keep a live
   `orderbook.50` subscription for instruments with a recent burst?
2. Are 28 days enough to size the costs, at about 15 signals a day?
3. Is a run-frozen universe acceptable for a bounded measurement?
