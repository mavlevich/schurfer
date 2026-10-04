# Gate trade collection: storage, load and recovery budget v1

Status: **measurement and decision for PR 5 of the priority queue; its growth estimate
was corrected on 2026-10-04 (see Correction).** It sizes the
collection of Gate's trade archives chosen by
[PR 3](pre-move-source-selection-v1.md) and records whether the current server can
carry it. It starts no collector, changes no retention, deletes nothing and reads no
market value: only archive sizes, disk, relation and chunk sizes, row counts by
creation time before the blind window, and backup metadata. Gate open interest is not
budgeted, because PR 3 deferred it (no enforceable request bound).

## Correction (2026-10-04)

The decision below rested on an estimate of the database's other growth that a first
direct measurement does not support. The original text and evidence are kept
unchanged under it.

- **What was estimated.** `other_growth` charged every row created in 2026-09-15..28
  at its table's mean bytes per row. That mean includes space PostgreSQL reuses (old
  row versions that vacuum frees), so heavily updated tables were charged for growth
  they did not make.
- **What was measured.** The same eleven plain tables were read again at 2026-10-03
  22:20 UTC (relation sizes only,
  [`growth-reading-2026-10-03.json`](evidence/gate-collection-budget-v1/growth-reading-2026-10-03.json),
  SHA-256 `6f9b5c3aba2521ac1df6588c2cf7e971a11c5d41cb68a36162f061476f97abc4`), 1.16 days
  after the `growth-inputs.json` reading. Together they grew about **56 MiB/day**
  against the 220 MiB/day estimate. The paper probes (estimate 49.6), the context
  samples (57.2) and their runs grew by zero bytes; `trade_decision_outcomes` grew
  faster than estimated (23.1 against 11.7). The hypertables were not re-measured.
- **What follows.** One interval is not a rate: free space inside a table can run out
  and growth then steps up, and a day can be atypical. The days-to-reserve figures and
  the "for any universe" verdict are therefore withdrawn, and no new date is given.
  The disk verdict for the registry set is **undetermined** until a series of at
  least a week (the measurement follow-up of
  `docs/runbooks/history-archive-design-v1.md`).
- **What still holds.** The Gate volumes, the recovery evidence and the limits. The
  full 592-base universe still does not fit on the current disk: it needs 24.1 GiB
  over six months against 15.7 GiB above the reserve even with no other growth at all.
  The registry set fits for six months only if all other growth stays under 74 MiB/day
  after the hot-bars release; the measured interval (56 MiB/day for the plain tables,
  plus unmeasured hypertables estimated at about 31 MiB/day) does not decide that.

## Decision (superseded in part by the correction above)

**Insufficient for a six-month collection on the current disk, for any universe, and
the reason is not Gate.** The database outside the hot bars grows by about
**0.25 GiB a day** (measured below; 0.33 GiB a day in the conservative scenario) with
no retention on its largest tables. At that rate the disk reaches its 10 GiB reserve
in about **24 days** from now, or about **64 days** once the hot bars retention has
released the 9.85 GiB already due, with or without Gate.

- **Gate itself is small for the registry set.** The 44 registry v4 Gate-to-Bybit
  bases need 0.40 GiB a month at the larger measured month, 2.4 GiB over 6 months
  (about 13 MiB a day). They fit for six months if all other growth stays below
  **74 MiB a day** after the bars release (19 MiB a day today), against a measured
  252 MiB a day.
- **The full 592-base universe does not fit even with no other growth:** 24.1 GiB
  over 6 months against 15.7 GiB above the reserve after the bars release.
- **What makes a collection supportable:** bring the other growth under the
  threshold (retention or an offsite archive for the large `app` tables, the next
  item of the data lifecycle work) or add a volume, then confirm the rate with a
  seven-day series of free disk and database size outside the bars. Until then
  overall feasibility is unresolved and no collector starts.

## Server measurements (2026-10-02, read-only)

| Measurement        | Value                                                                                                                                                        | Source                                            |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------- |
| Root disk          | 80.3 GB; free **15.86-16.70 GiB** during the day (17.03 GB at 18:28 UTC, used for the budget)                                                                | `df -B1`                                          |
| Docker data        | 54 GiB under `/var/lib/docker`; local volumes 42.6 GB; build cache 3.5 GB and images 3.2 GB reclaimable                                                      | `du`, `docker system df`                          |
| Database           | 37.9 GiB: `bybit_momentum_bars_1m` 19 GB, `momentum_flow_watch_evaluations_1m` 4.6 GB, `pump_derivatives_context_samples` 3.6 GiB, `trade_decisions` 2.6 GiB | `pg_database_size`, relation and hypertable sizes |
| Logical dumps      | 40.5 GB on 09-08, 47.6 on 09-13, 56.5 on 09-20, 66.6 on 09-26, 65.4 on 09-29, 60.6 GB on 10-02                                                               | Borg `db-*` archive stats                         |
| Hot bars           | 37 daily chunks (08-27 to 10-02) for a 14-day cutoff; the gated deletion drops up to 3 verified days a night                                                 | chunk metadata, deletion journal                  |
| `runtime/research` | 876 MB (research archive 1.04 GB, 0.80 GB compressed)                                                                                                        | `du`, Borg `research-*`                           |
| CPU and memory     | 4 cores, load average 4.3-5.0; 7.6 GiB RAM, 3.6-3.7 GiB available, 1.4 GiB swap in use                                                                       | `uptime`, `free`                                  |
| Offsite repository | 51.2 GB unique compressed on a 1 TB Storage Box BX11 (about 4 EUR a month, ADR-0010)                                                                         | `borg info`                                       |
| Backup retention   | `db-*` 7 daily, 4 weekly, 6 monthly; `research-*` 7 daily, 8 weekly, 24 monthly; `bars-*` never pruned                                                       | `infra/scripts/offsite-backup.sh`                 |

## Growth of everything else

Measured by [`growth-inputs.sql`](evidence/gate-collection-budget-v1/growth-inputs.sql)
(metadata and row counts only, window 2026-09-15 to 2026-09-28, before the blind
window) into [`growth-inputs.json`](evidence/gate-collection-budget-v1/growth-inputs.json),
SHA-256 `9c60f5a2b15d49c315fdb6a222f3e5442708f273ed84927aaf15f5ec146e66ce`, and
computed by `other_growth`:

- a plain table grows by its mean bytes per row times the rows created per day in
  the window;
- a hypertable grows by the bytes per day of its chunks wholly inside the window,
  unless its retention job is shown holding it at steady state: the last run
  succeeded within one schedule interval, no run ever failed, the oldest chunk has
  reached the drop age at that run, and no chunk that run should have dropped is
  still there;
- the conservative scenario counts every hypertable at its window rate, retention
  or not;
- the hot bars are left out: the gated deletion holds them at the 14-day cutoff.

| Table                                                                |     Size | Rows created in the window |                                                    Growth |
| -------------------------------------------------------------------- | -------: | -------------------------: | --------------------------------------------------------: |
| `app.pump_derivatives_context_samples`                               | 3.58 GiB |                      21.9% |                                              57.2 MiB/day |
| `app.trade_decisions`                                                | 2.62 GiB |                      26.4% |                                              50.5 MiB/day |
| `app.momentum_flow_paper_probes`                                     | 2.08 GiB |                      32.6% |                                              49.6 MiB/day |
| `app.live_long_short_ratio` (hypertable, no retention)               |          |                            |                                              19.8 MiB/day |
| `app.funding_rate_snapshots`                                         | 0.76 GiB |                      26.7% |                                              14.8 MiB/day |
| `app.trade_decision_outcomes`                                        | 1.55 GiB |                      10.3% |                                              11.7 MiB/day |
| `timeseries.liquidation_events` (180-day retention, not yet reached) |          |                            |                                              11.6 MiB/day |
| `app.pump_events`, `app.pump_event_sources`                          | 1.22 GiB |                     24-27% |                                              22.6 MiB/day |
| `app.oi_snapshots`                                                   | 0.53 GiB |                      26.5% |                                              10.3 MiB/day |
| three smaller tables                                                 | 0.18 GiB |                     23-48% |                                               3.4 MiB/day |
| `momentum_flow_watch_evaluations_1m` (45-day retention, see below)   |   4.6 GB |                            |                            0 (conservative: 91.0 MiB/day) |
| **Total**                                                            |          |                            | **251.5 MiB/day (0.246 GiB); conservative 342.5 MiB/day** |

The watch evaluations retention job ran 50 times without a failure, last succeeding
at 2026-10-02 11:59 UTC; its oldest chunk (2026-08-18) has reached the 45-day age and
no older chunk remains. Its first actual drop is due on 2026-10-03, so a drop has not
yet been observed: the conservative scenario keeps its 91 MiB a day.

The window holds 10-33% of each large table's rows over 14 days, so these tables were
built at about this rate; none has a retention policy. The mean bytes per row
includes indexes and any bloat, which can overstate a table whose old rows are wider
than new ones. A disk-level cross-check was not possible: the only earlier byte-level
`df` reading (2026-09-07) was taken during a cleanup that freed 3 GB within a minute,
before the local dumps moved offsite, so it cannot separate growth from cleanup.

## Gate trade volume

Measured with `schurfer_analytics.gate_collection_budget` from clean revision `6ce027d`.
Artifact: [`evidence/gate-collection-budget-v1/gate-collection-budget.json`](evidence/gate-collection-budget-v1/gate-collection-budget.json),
SHA-256 `4b204640e293048b50afc9aeb2123be3c00e2551b48be3054f3dfb4aa343d3d5`.

- **Sizes:** `HEAD` of Gate's June and July 2026 trade archive file for every base of
  PR 3's 592-base universe: 1,184 requests, each logged with its status, length,
  checked window end and time. 1,055 returned 200 with a size and 129 a 404; any
  other outcome would have stopped the run. Each request passed PR 3's window guard,
  so nothing after 2026-08-01 was touched. 64 bases have no Gate file in either
  month, mostly multiplier-prefixed names (`1000PEPE`, `1000BONK`, ...) that need
  name and identity mapping; MVLL has only July and its July counts.
- **Planning month:** June and July totals differ by up to 2x for the registry set,
  so the budget plans on the larger month, not the mean.
- **Expansion and conversion:** PR 3's three July files: CSV is **3.58 x** the gzip;
  Parquet (zstd) is **0.68 x** the gzip; DuckDB converts straight from the gzip in
  about 111 s of elapsed time per GiB of gzip on a workstation.

| Universe                    | Bases with a file | Raw gzip per month | Parquet per month | Kept per month | Kept after 3 / 6 months | Scratch (stream / gunzip) |
| --------------------------- | ----------------: | -----------------: | ----------------: | -------------: | ----------------------: | ------------------------- |
| Registry v4 Gate-to-Bybit   |          44 of 44 |           0.24 GiB |          0.16 GiB |   **0.40 GiB** |       1.2 / **2.4 GiB** | 0.08 / 0.45 GiB           |
| Full universe               |        528 of 592 |           2.39 GiB |          1.63 GiB |   **4.02 GiB** |     12.1 / **24.1 GiB** | 0.20 / 1.05 GiB           |
| Full without the 10 largest |        518 of 582 |           1.57 GiB |          1.07 GiB |   **2.64 GiB** |      7.9 / **15.9 GiB** | 0.03 / 0.18 GiB           |

The 10 largest by their larger month are ETH, BTC, H, AKE, SNDK, LAB, BANK, SKHYNIX,
BEAT and DEXE.

## Headroom

The 10 GiB reserve stays free (deploy image builds, build cache, dumps in flight);
collection and all other growth come out of the space above it. The bars release is
the 9.85 GiB of hot bar chunks already past the 14-day cutoff, dropped over about
11 nights.

| Scenario                                                     | Above reserve | Registry 44: fits 6 months / days to reserve | Full universe: fits / days | Largest other growth for registry 44 |
| ------------------------------------------------------------ | ------------: | -------------------------------------------- | -------------------------- | -----------------------------------: |
| Now, measured other growth                                   |      5.86 GiB | no / 23                                      | no / 16                    |                           19 MiB/day |
| Now, conservative growth                                     |      5.86 GiB | no / 17                                      | no / 13                    |                           19 MiB/day |
| After bars release, measured other growth                    |     15.71 GiB | no / 61                                      | no / 42                    |                           74 MiB/day |
| After bars release, conservative growth                      |     15.71 GiB | no / 45                                      | no / 34                    |                           74 MiB/day |
| After bars release, no other growth (reference only)         |     15.71 GiB | yes / 1,198                                  | no / 119                   |                                      |
| No collection, measured / conservative growth, now           |      5.86 GiB | 24 / 18 days                                 |                            |                                      |
| No collection, measured / conservative growth, after release |     15.71 GiB | 64 / 47 days                                 |                            |                                      |

A universe without any archive, or with an unmeasured base, gets no verdict rather
than a fit.

**Load:** downloading 2.4 GiB a month is negligible for the network. Conversion took
about 4 minutes of elapsed time per month of the full universe on a workstation;
server conversion time and memory were not measured, and the server already runs
near 4 load on 4 cores, so a collector must run niced, one file at a time, and record
its own CPU time and memory.

## Recovery evidence

- **Database (ENG-025):** the automated drill restored the critical tables of
  `db-2026-09-28T17:12:59` into a throwaway container on 2026-09-28: 23 tables,
  134,278 rows, all row hashes matching, 416 s. Its weekly timer first fires on
  2026-10-04.
- **Research files (this PR):** from `research-2026-10-02T11:06:49`, `borg extract`
  of `hyp029`, `preblind-book-cost-baseline` and `hyp012c` into a throwaway directory
  on the server took 1.2 s for 30 MB. All 6 files with a `.sha256` sidecar matched
  both their sidecar and the live file; the directory was then removed.
- **Pre-move probe inputs (PR 3):** copied to the backed-up research directory and
  fetched back with every hash matching and an identical offline replay.

## Limits for a collection

- **Disk reserve:** at least **10 GiB free** at all times; the reserve is never
  budgeted for growth.
- **Growth:** a collector starts only when the measured other growth leaves the
  six-month budget of its registered universe inside the space above the reserve.
- **Retention:** native gzip archives are the evidence of record. A local copy may be
  removed only after a verified offsite archive under a never-pruned prefix (the
  `bars-*` pattern); `research-*` monthly archives expire after 24 months and are not
  enough. Parquet is a rebuildable cache.
- **Load:** one file at a time, `nice`/`ionice` idle, outside the backup window
  (about 04:00-04:30 UTC), with CPU time, memory and duration logged.
- **Gaps:** per included contract and month, a missing file or any `dealid` gap is
  logged with its reason; more than 5% of included contracts missing a month stops the
  collection for review.
- **Stop conditions:** free disk below 10 GiB, or projected below it within 30 days at
  the measured total growth; a failed offsite archive or restore check; a missing
  month above the 5% threshold; or a collector run outside its registered universe.

## Not measured

- a disk-level growth series (only the table-level estimate above);
- conversion time, CPU and memory on the server itself;
- Gate's archive publication lag and whether delisted contracts appear (PR 3, H2);
- more than two months of archive sizes;
- the 64 bases without a same-named Gate file.

## Requirements for the collector PR

Before it runs: the other growth brought under the threshold and confirmed by seven
days of free disk and database size outside the bars, or a volume added (an owner
decision); its universe (start: the 44 registry bases), months and budget registered.
Then: download by `HEAD`-checked size against these limits; keep native gzip with
SHA-256 and a per-month manifest; convert by streaming to Parquet; log gaps; archive
offsite under a never-pruned prefix before any local deletion; verify a restore of one
month; and stop on the conditions above. Collection itself still waits for the
2026-10-31 direction decision, and nothing dated on or after 2026-09-29 is read for
research before HYP-012 v2 reaches a terminal state.
