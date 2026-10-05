# MEXC sealed deal-stream probe v1: readout

Rollout step 1 of
[realtime-market-capture-design-v1](../engineering/realtime-market-capture-design-v1.md).

- **Run:** 2026-10-04 22:02-22:32 UTC on the production host, 30 minutes.
- **Probe:** `apps/collector/cmd/mexcprobe` at 3d87f13.
- **Report:** [probe-20261004T220218Z-3d87f13.json](evidence/mexc-sealed-probe-v1/probe-20261004T220218Z-3d87f13.json),
  sha256 `8c4575075a6aca9c0d51a8631ec5741ba2a5e13771b69ddcf672ecd2d4c3c469`.

The report holds counters only: no price, size, side or symbol. No market value was
printed or kept.

## Results

| What                     | Measured                                                                                |
| ------------------------ | --------------------------------------------------------------------------------------- |
| Universe                 | 1,077 enabled USDT-settled perpetuals; `contractSize` present on all                    |
| Connections              | 22 at 50 symbols each; 1,077 of 1,077 subscriptions acknowledged; no error              |
| Stability                | 0 reconnects in 30 minutes; pings every 15 s, 2,618 pongs                               |
| `compress=false`         | Accepted; every push carries exactly one trade as an object, never a list               |
| Fields per trade         | `p`, `v`, `T`, `O`, `M`, `t` (documented) and `i`, `cts` (undocumented); none malformed |
| Rate                     | 323,652 trades: about 180/s; per second p50 163, p90 245, p99 535, max 1,390            |
| Volume                   | 54 MB of JSON in 30 minutes: about 15.5 M trades and 2.6 GB of raw JSON per day         |
| Receive minus trade time | p50 121 ms, p90 145, p99 243, max 2,964 (200,000 sampled)                               |
| Receive minus push `ts`  | p50 113 ms, p90 136, p99 237                                                            |
| Receive minus pong time  | p50 112 ms, p99 138                                                                     |

## Limits of this report (design review 2)

The v1 report stays unchanged; its sha256 is above. Four of its readings are weaker than
v1 of this readout claimed:

- **Universe.** The probe did not check `futureType`, so delivery contracts, if any were
  enabled and USDT-settled, would have been counted as perpetuals.
- **Subscriptions.** "1,077 of 1,077 acknowledged" was a pooled count. MEXC's
  acknowledgement names no instrument, so two acknowledgements for one instrument would
  look like two subscriptions.
- **Lag samples.** The lag quantiles come from a sample that, once full, kept every new
  value and evicted a random old one. That biases the sample towards the end of the
  run; it is not a uniform sample of the 30 minutes.
- **Pong.** `receive minus pong time` is receive time minus the server time inside the
  pong. It mixes the one-way delay with the clock offset between the hosts; it is not a
  round trip. Network, clock offset and MEXC's own delay cannot be separated from
  these numbers.

`i` and `cts` are documented in MEXC's deal channel documentation; their exact
semantics are still checked before any use.

Probe v2 (`mexc_sealed_probe_v2`) fixes all four:

- perpetuals are selected by `futureType` (1), with every exclusion counted by reason;
- acknowledgements are accounted per connection, and the instruments actually seen
  trading are counted;
- a uniform reservoir sample (Algorithm R) is used;
- the ping round trip is measured from the ping sent to its pong received.

## What it means for the canary

- **Load is small in messages.** About 180 messages per second, with a peak of 1,390.
  Whether CPU or disk is the constraint is not measured yet: the canary's replay
  benchmark measures CPU, RSS and the decode-to-signal-to-write path before any cap
  is set.
- **Latency.** The trade-to-receive lag is about 120 ms (p99 about 240 ms) by the
  host's clock, chrony-disciplined. How much of that is network, clock offset and
  MEXC's own delay is not known from v1; v2 measures the round trip. This number also
  has different endpoints from today's 45-110 s path to a paper entry, so it is not a
  measured speed-up of any signal.
- **Storage.** The full raw tape is 2.6 GB a day of JSON. The compressed size is not
  measured; the canary measures it before the recorder's caps and the choice between
  pinned windows and the full tape are fixed.
- **Sharding.** 50 symbols per connection over 22 connections held for 30 minutes
  without errors or reconnects.
