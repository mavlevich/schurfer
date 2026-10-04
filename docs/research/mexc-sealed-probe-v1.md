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

## What it means for the canary

- **Load is small.** About 180 messages per second, with a peak of 1,390, is far below
  what one Go process handles. CPU is not the constraint; disk is.
- **The lag is mostly the path, not MEXC.** The pong round trip (server time in the
  pong) shows the same 112 ms as the trade lag. So about 110 ms is network plus clock
  offset, and MEXC adds about 10 ms of its own before the push. The p99 of 243 ms is
  2-3 orders of magnitude below today's 45-110 s pipelines.
- **Storage.** The full raw tape is 2.6 GB a day of JSON. Compressed canonical events
  are expected near a tenth of that. The recorder's caps and the choice between pinned
  windows and the full tape are sized from this, against the host's free disk and the
  storage-budget PR.
- **Two undocumented fields.** `i` and `cts` appear on every trade. Until their meaning
  is confirmed from the documentation or a fixture, the codec records them verbatim and
  uses neither (if `i` proves to be a trade id, it will serve deduplication).
- **Sharding.** 50 symbols per connection over 22 connections held for 30 minutes
  without errors. The canary starts with the same.
