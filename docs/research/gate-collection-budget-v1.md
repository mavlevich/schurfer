# Gate trade collection: storage, load and recovery budget v1

Status: **measurement and decision for PR 5 of the priority queue.** It sizes the
collection of Gate's trade archives chosen by
[PR 3](pre-move-source-selection-v1.md) and records whether the current server can
carry it. It starts no collector, changes no retention, deletes nothing and reads no
market value: only archive sizes, disk and database sizes, and backup metadata. Gate
open interest is not budgeted, because PR 3 deferred it (no enforceable request bound).

## Server measurements (2026-10-02, read-only)

| Measurement        | Value                                                                                                                                                   | Source                                            |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| Root disk          | 80.3 GB total, 59.6 GB used, **17.35 GB free (16.16 GiB)**, 78%                                                                                         | `df`                                              |
| Docker data        | 54 GiB under `/var/lib/docker`; local volumes 42.6 GB; build cache 3.5 GB and images 3.2 GB reclaimable                                                 | `du`, `docker system df`                          |
| Database           | 38 GB: `bybit_momentum_bars_1m` 19 GB, `momentum_flow_watch_evaluations_1m` 4.6 GB, `pump_derivatives_context_samples` 3.7 GB, `trade_decisions` 2.7 GB | `pg_database_size`, hypertable and relation sizes |
| Database growth    | dump (uncompressed) 40.5 GB on 09-08, 47.6 on 09-13, 56.5 on 09-20, 66.6 on 09-26, then 65.4 on 09-29 and **60.6 GB on 10-02**                          | Borg `db-*` archive stats                         |
| Hot bars retention | 14-day cutoff active; the gated deletion of older bars runs nightly                                                                                     | `runtime/cold-bar-cutoff-days`, timers            |
| `runtime/research` | 876 MB (research archive 1.04 GB, 0.80 GB compressed)                                                                                                   | `du`, Borg `research-*`                           |
| CPU and memory     | 4 cores, load average 4.3-5.0; 7.6 GiB RAM, 3.6-3.7 GiB available, 1.4 GiB swap in use                                                                  | `uptime`, `free`                                  |
| Offsite repository | 51.2 GB unique compressed on a 1 TB Storage Box BX11 (about 4 EUR a month, ADR-0010)                                                                    | `borg info`                                       |
| Backup retention   | `db-*` 7 daily, 4 weekly, 6 monthly; `research-*` 7 daily, 8 weekly, 24 monthly; `bars-*` never pruned                                                  | `infra/scripts/offsite-backup.sh`                 |

The database grew about 1.4 GB a day in September and has shrunk since the 14-day hot
bars cutoff began converging; the disk has not yet gained back that space. Growth
outside the bars tables was not separated and is the main unmeasured load on the
disk.

## Gate trade volume

Measured with `schurfer_analytics.gate_collection_budget` from clean revision `6a6ccfe`.
Artifact: [`evidence/gate-collection-budget-v1/gate-collection-budget.json`](evidence/gate-collection-budget-v1/gate-collection-budget.json),
SHA-256 `cc1b6c7534529319c7e743918aaf3a67c95cfa82712c2b54fe284d9a1a37bd55`.

- **Sizes:** `HEAD` of Gate's June and July 2026 trade archive file for every base of
  PR 3's 592-base universe (1,184 requests). Each request passed PR 3's window guard,
  so nothing after 2026-08-01 was touched. 65 Bybit bases have no same-named Gate file,
  mostly multiplier-prefixed names (`1000PEPE`, `1000BONK`, ...); they need name and
  identity mapping, not just a download, and count as absent here.
- **Expansion and conversion:** PR 3's three July files (C98, FIGHT, VELODROME): CSV
  is **3.58 x** the gzip; Parquet (zstd) is **0.68 x** the gzip; DuckDB converts
  straight from the gzip at about 111 s per GiB of gzip on a workstation.

| Universe                    | Bases with both months | Raw gzip per month | Parquet per month | Kept per month | Kept after 3 / 6 months | Largest file | Scratch (stream / gunzip) |
| --------------------------- | ---------------------: | -----------------: | ----------------: | -------------: | ----------------------: | -----------: | ------------------------- |
| Registry v4 Gate-to-Bybit   |               44 of 44 |           0.17 GiB |          0.12 GiB |   **0.29 GiB** |     0.87 / **1.75 GiB** |     0.07 GiB | 0.05 / 0.24 GiB           |
| Full universe               |             527 of 592 |           2.38 GiB |          1.62 GiB |   **4.00 GiB** |     12.0 / **24.0 GiB** |     0.28 GiB | 0.19 / 1.02 GiB           |
| Full without the 10 largest |             517 of 582 |           1.49 GiB |          1.02 GiB |   **2.51 GiB** |      7.5 / **15.1 GiB** |     0.04 GiB | 0.03 / 0.13 GiB           |

The 10 largest by July and June mean are ETH, BTC, LAB, H, SNDK, AKE, SKHYNIX, SOL,
ESPORTS and BANK. June and July differ by up to a factor of two for the registry set,
so a monthly figure is a two-month mean, not a forecast.

**Load:** downloading 2.4 GiB a month is negligible for the network. Conversion is
about 4.4 CPU minutes a month for the full universe on the workstation; the server is
already near 4 load on 4 cores, so the job must run niced, one file at a time, and
may take several times longer there. Streaming conversion needs scratch space for one
Parquet file only; a gunzip-first step would need up to 1 GiB.

## Recovery evidence

- **Database (ENG-025):** the automated drill restored the critical tables of
  `db-2026-09-28T17:12:59` into a throwaway container on 2026-09-28: 23 tables,
  134,278 rows, all row hashes matching, 416 s. Its weekly timer first fires on
  2026-10-04.
- **Research files (this PR):** from `research-2026-10-02T11:06:49`, `borg extract`
  of `hyp029`, `preblind-book-cost-baseline` and `hyp012c` into a throwaway directory
  on the server took 1.2 s for 30 MB. All 6 files with a `.sha256` sidecar matched
  both their sidecar and the live file; the directory was then removed. At that rate,
  6 months of the full universe (24 GiB) would restore in about 16 minutes, before
  verification.
- **Pre-move probe inputs (PR 3):** copied to the backed-up research directory and
  fetched back with every hash matching and an identical offline replay.

## Limits for a collection

- **Disk reserve:** keep at least **10 GiB free** at all times, for image rebuilds
  during deploys (build cache alone is 3.5 GB) and the unmeasured database growth
  outside bars. With 16.16 GiB free, **6.16 GiB** are usable for collection.
- **Retention:** native gzip archives are the evidence of record. A local copy may be
  removed only after a verified offsite archive under a never-pruned prefix (the
  `bars-*` pattern); `research-*` monthly archives expire after 24 months and are not
  enough. Parquet is a rebuildable cache.
- **Load:** one file at a time, `nice`/`ionice` idle, outside the backup window
  (about 04:00-04:30 UTC), with the job's CPU time and duration logged.
- **Gaps:** per included contract and month, a missing file or any `dealid` gap is
  logged with its reason; more than 5% of included contracts missing a month stops the
  collection for review.
- **Stop conditions:** free disk below 10 GiB, or projected below it within 30 days;
  a failed offsite archive or restore check; a missing month above the 5% threshold;
  or a collector run outside its registered universe.

## Decision

- **Supported now:** the **registry v4 Gate-to-Bybit set (44 bases)** with raw gzip
  and Parquet kept on the server: 0.29 GiB a month, 1.75 GiB after 6 months, about 21
  months to reach the reserve. This is also the only set with approved identity today
  (PR 3).
- **Not supported on the current disk:** the **full 592-base universe** (24 GiB after
  6 months, the usable 6.16 GiB lasts about 1.5 months) and the full universe without
  its 10 largest bases (15.1 GiB; about 2.5 months). The reason is the server's free
  disk above the reserve, not bandwidth, CPU or offsite capacity (the Storage Box has
  about 950 GB free at a fixed price).
- **Ways to support the full universe**, an owner decision when identity work extends
  the universe:
  1. keep only Parquet locally and the raw archives offsite under a never-pruned
     prefix with gated local deletion: about 9.7 GiB for 6 months, still above the
     usable space;
  2. also hold only the last 3 months of Parquet locally: about 4.9 GiB, fits;
  3. add a server volume: public price listings checked on 2026-10-02 give about
     0.044-0.052 EUR per GB a month excluding VAT, so about 2.2-2.6 EUR a month for
     50 GB; the current price is confirmed in the Hetzner console before ordering.

## Not measured

- database growth outside the bars tables, which the 10 GiB reserve covers;
- Gate's archive publication lag and whether delisted contracts appear (PR 3, H2);
- conversion time on the server itself (measured on a workstation);
- more than two months of archive sizes;
- the 65 multiplier-prefixed names.

## Requirements for the collector PR

Register its universe (start: the 44 registry bases), months and budget before it
runs; download by `HEAD`-checked size against these limits; keep native gzip with
SHA-256 and a per-month manifest; convert by streaming to Parquet; log gaps; archive
offsite under a never-pruned prefix before any local deletion; verify a restore of one
month; and stop on the conditions above. Collection itself still waits for the
2026-10-31 direction decision, and nothing dated on or after 2026-09-29 is read for
research before HYP-012 v2 reaches a terminal state.
