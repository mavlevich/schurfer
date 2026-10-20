# Disk growth and the Gate collection budget (PR 2b, v1)

Status: **result.** Artifact
[`disk-growth-budget-20261017T120001Z.json`](evidence/disk-growth-budget-v1/disk-growth-budget-20261017T120001Z.json),
sha256 `33c10e99959006ff7e6416c6e3faf3b40c6a8836e652ea96812e395035cfff90`, from clean code
revision `4ed6cf3`, readings window `[2026-10-04T00:00Z, 2026-10-17T23:59:59Z]`. It covers 15
daily readings over **12.8 days** (2026-10-04 16:40 to 2026-10-17 12:00 UTC), with no gap.
Every number below is from it.

This answers what the [PR 5 correction](gate-collection-budget-v1.md#correction-2026-10-04)
left undetermined: how fast the disk fills outside the hot bars, and whether a Gate
collection fits six months above the 10 GiB reserve. It starts no collector, changes no
retention and deletes nothing.

## Answer

- **Free space:** 27.3 GiB, 17.3 GiB above the reserve. The hot bars have finished
  releasing to the 14-day cutoff (set on 2026-09-30): 15 daily chunks, none due.
- **Growth outside the bars:** **31 MiB/day central, 102 MiB/day conservative.** The
  conservative rate is the database outside the bars, which grows steadily every day.
  The central one is lower because `rest` (WAL, logs, the system) shrank on average,
  and the conservative rate does not count a shrinking part.
- **Without any collection** the conservative rate reaches the reserve in about
  174 days. The 30-day stop condition is not met.

| Gate universe (PR 5)            | Needs over 6 months, conservative | Largest other growth that fits | Class               |
| ------------------------------- | --------------------------------: | -----------------------------: | ------------------- |
| registry 44 Gate-to-Bybit bases |                          20.6 GiB |                   83.2 MiB/day | `fits_central_only` |
| full 592-base universe          |                          42.5 GiB |                              0 | `does_not_fit`      |
| 592 without the 10 largest      |                          34.0 GiB |                    8.0 MiB/day | `does_not_fit`      |

So the **registry 44** fit only if the database grows at the central rate. At the
conservative 102 MiB/day they need 20.6 GiB against 17.3 GiB above the reserve. The
full universe does not fit on this disk at any rate, as PR 5 already found.

## The one lever that changes the answer

About **43 MiB/day** of the database growth is two tables the closed pump-short line
still writes: `app.trade_decisions` (32.8 MiB/day: `pump_short_measurement_v1` and
`pump_short_v1_market_quality` rows) and `app.trade_decision_outcomes`
(10.5 MiB/day). The decision register closes that line ("Executable pump-short:
CLOSED"; "Delayed-short / orderflow (HYP-024): stopped; do not reopen").

Without them (the artifact's `what_if_without_pump_short`, reported beside the verdict,
never replacing it). The what-if removes the two tables' bytes from the database part in
every reading and re-estimates every part the same way as the verdict:

| Gate universe              | Conservative rate | Class               |
| -------------------------- | ----------------: | ------------------- |
| registry 44                |        58 MiB/day | `fits`              |
| 592 without the 10 largest |        58 MiB/day | `fits_central_only` |
| full 592                   |        58 MiB/day | `does_not_fit`      |

- HYP-012 v2's shadow also writes into `app.trade_decisions` and reconciles against it,
  so a stop or a retention must target the pump-short rows only, never the table. Its
  rows are few, so the what-if slightly overstates the saving.
- Stopping the writer is a production configuration change: an owner and colleague
  decision, not part of this PR.
- The other route is an added volume (an owner decision), as in PR 5.

## Method

The filesystem's used bytes minus the hot bars are split into parts, each with the
estimator that fits how it grows:

| Part                                   | How it grows                  | Estimator                                    |
| -------------------------------------- | ----------------------------- | -------------------------------------------- |
| database outside the bars              | steadily                      | least-squares slope                          |
| `rest` (WAL, logs, system, unreadable) | steadily, noisily             | least-squares slope                          |
| readable `runtime/`                    | in steps (a study's download) | median daily interval; steps > 1 GiB listed  |
| Docker images, build cache, containers | up and down with deploys      | no rate: covered by the reserve; range shown |

**Central** adds the parts as measured. **Conservative** counts a shrinking part as
zero. A universe fits only at the conservative rate; a series shorter than a week gives
no verdict. Each reading is checked against its `.sha256`, and PR 5's budget artifact
against its pinned sha256 (`4b204640…`), before use.

## Where the space goes

| Part                      | MiB/day | Note                                                                                           |
| ------------------------- | ------: | ---------------------------------------------------------------------------------------------- |
| database outside the bars |   101.6 | steady over every reading                                                                      |
| `rest`                    |   -70.7 | swings by about 0.4 GiB between readings                                                       |
| readable `runtime/`       |     0.1 | one step of 1.4 GiB on 2026-10-05: `runtime/research/edge-loss` (the HYP-030 edge-loss export) |
| Docker (reserve)          |       - | images 3.1 to 3.8 GiB, build cache 0.4 to 3.2 GiB across readings                              |

Largest tables by growth (a hypertable by the sum of its chunks): `app.trade_decisions`
32.8, `app.live_long_short_ratio` 19.5, `app.funding_rate_snapshots` 13.7,
`timeseries.liquidation_events` 12.8, `app.trade_decision_outcomes` 10.5 and
`app.oi_snapshots` 9.6 MiB/day. The order-rule capture (#511) adds about 1.5 MiB/day.

## What changes in PR 5

The correction's "undetermined" disk verdict is replaced by the classes above. The Gate
volumes, the recovery evidence and the limits of PR 5 stay as they are.

## Reproduce

```bash
make prod-disk-growth-budget FROM=2026-10-04T00:00:00+00:00 UNTIL=2026-10-17T23:59:59+00:00
```

After this PR is merged it runs on the host from a clean checkout. The readings window is a
required input and is recorded in the artifact. It mounts the readings and PR 5's artifact
read-only, takes the live bars cutoff, and writes
`runtime/research/disk-growth-budget/disk-growth-budget-<latest reading>.json` and its
`.sha256`, once. This result was produced locally from the same tool at the clean revision
named above, on the production readings with their hashes checked.

**For the 2026-10-31 decision** (brief #506): after that day's 12:00 UTC reading (or one
taken with `make prod-disk-growth-run`), run it with
`FROM=2026-10-04T00:00:00+00:00 UNTIL=2026-10-31T23:59:59+00:00`. The window and the
fresh reading are fixed here in advance, so the recompute is not chosen on the day.
