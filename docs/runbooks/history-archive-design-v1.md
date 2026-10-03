# History archive on the Storage Box: audit and pilot design v1

Status: **design review 1 folded in (2026-10-04); pilot code follows.** No export,
deletion or production policy change has run. The audit below used production metadata only (relation, chunk and
column-statistics sizes, write counters, retention job stats) and the repository; it
read no market value.

Goal: bound the PostgreSQL working set on the server by moving closed history to the
existing 1 TB Storage Box, while keeping every research result reproducible. An extra
volume stays the fallback.

## 1. Audit of the large tables (2026-10-03)

"Estimate" is the PR 5 bytes-per-row times new-rows estimate (window 2026-09-15..28,
[gate-collection-budget-v1](../research/gate-collection-budget-v1.md)). "Measured" is
the change of `pg_total_relation_size` between 2026-10-02 18:28 and 2026-10-03 22:20
UTC (1.16 days; one interval, so a first reading, not a rate). "Writes" is
`pg_stat_user_tables` since the 2026-09-08 restart.

| Table                                                                                |                                                On disk |  Estimate / measured | Writer and write pattern                                                                                                                                   | Readers                                                                                                              | Hot need                                      | Protected                                               |
| ------------------------------------------------------------------------------------ | -----------------------------------------------------: | -------------------: | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- | --------------------------------------------- | ------------------------------------------------------- |
| `app.live_long_short_ratio` (hypertable, 7-day chunks, no compression, no retention) |                     897 MB, 8 chunks, ~0.9 M rows/week |  19.8 / not measured | `market-hotset` LSR subscriber: `INSERT ... ON CONFLICT (exchange, base, ts) DO NOTHING`, `ts` = Binance period time (`limit=1`); no update or delete path | `api-gateway` pumps signal: 4 h before the anchor of every **open** pump episode (MAD score); **no research reader** | the window of every open episode, however old | none                                                    |
| `app.pump_derivatives_context_samples` (+ `_runs`)                                   |                     3.6 GiB (heap 3.0 GB, idx 0.65 GB) | 57.2 / **0** MiB/day | `outcome-resolver` derivatives context: upsert `ON CONFLICT DO UPDATE` (2.8 M inserts, **59.9 M updates**)                                                 | derivatives/LSR regime and long-horizon funding reports                                                              | research reads by event                       | funding consumers; not HYP-015                          |
| `app.trade_decisions`                                                                |      2.6 GiB (TOAST 1.76 GB: `liquidity` JSON, ~745 B) |  50.5 / 20.6 MiB/day | `execution` decisions: insert only                                                                                                                         | outcome resolver, episode selection, replays, source-lead shadow, API decisions, restore check                       | whole history for replays                     | source-lead shadow rows of the HYP-012 v2 window        |
| `app.trade_decision_outcomes`                                                        |                                               1.55 GiB |  11.7 / 23.1 MiB/day | `outcome-resolver`: upsert per horizon (4.8 M inserts, 1.5 M updates)                                                                                      | same as decisions                                                                                                    | whole history                                 | as decisions                                            |
| `app.momentum_flow_paper_probes` (+ `_outcomes`)                                     | 2.1 GiB for **14,889 rows** (heap 1.63 GB, idx 0.5 GB) | 49.6 / **0** MiB/day | `momentum-paper*` workers: insert, then **11.5 M updates, 0 HOT** (`updated_at` is in a partial index)                                                     | HYP-015 verdict reader and funding resolver, API trades, notifier                                                    | open positions, then the HYP-015 cohort       | **HYP-015** (cohort 2026-10-05..11-02, read from 11-04) |
| `app.funding_rate_snapshots`                                                         |                                               0.76 GiB |   14.8 / 6.2 MiB/day | `pump-scanner`: insert per pump event                                                                                                                      | API pumps (by `event_id`), abnormal-flow funding snapshot                                                            | by event                                      | none known                                              |
| `app.oi_snapshots`                                                                   |                                               0.53 GiB |   10.3 / 4.8 MiB/day | `pump-scanner`: insert per pump event                                                                                                                      | API pumps (by `event_id`)                                                                                            | by event                                      | none known                                              |

Measured over the same 1.16 days, all eleven plain tables together grew by about
**56 MiB/day**, against 220 MiB/day in the PR 5 estimate. The estimate charged every
new row at the table's mean bytes per row, which includes reusable space, so it
overstates current growth, and the PR 5 decision built on it needs a correction. One
interval is not a rate either: free space inside a table can run out and growth then
steps up, so the measurement is repeated before any conclusion about the disk.

Findings:

1. **Update churn is confirmed; its disk effect is not.** Each paper probe is updated
   about 730 times with no HOT update (the partial index on `updated_at` serves the
   fair-share queue of open positions, so the index cannot simply go), and the heap
   holds about 110 KB per live row. Yet neither the probes nor the context samples
   grew in the measured interval: autovacuum (325 k runs on the probes) makes old row
   versions reusable. What a rewrite would return, and whether the bloat comes back,
   needs live, dead and free space (`pgstattuple`, not installed), the autovacuum
   history and several successive readings. No rewrite is planned until then.
2. **Stopping database growth is not the same as freeing filesystem space.**
   Dropping a hypertable chunk unlinks its files and frees disk at once. Deleting
   rows from a plain table only makes the space reusable inside PostgreSQL; the
   filesystem gets it back only after a rewrite, which needs temporary space of about
   the table plus its indexes, plus WAL.
3. **HYP-015 depends on a 45-day retention.** The verdict reader joins
   `timeseries.momentum_flow_watch_evaluations_1m`, which drops chunks after 45 days.
   Rows of the cohort's first day (2026-10-05) drop on 2026-11-19. The read is due
   2026-11-04, so the margin is 15 days. Protecting these inputs ranks above the LSR
   pilot (section 8).

## 2. First pilot: `app.live_long_short_ratio`

Confirmed. It is already a hypertable, so a closed range is one 7-day chunk that can
be archived and later dropped whole, which frees filesystem space. It has one insert-only
writer, no update path, no research reader and no protected cohort. It is small: the
pilot proves the mechanism (which the HYP-015 inputs need next), not the disk budget.

| Item                                                      | Value                                                                                                                                                                 |
| --------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Deletable after a 14-day hot window (chunks 08-13..09-17) | about 0.59 GB, freed from the filesystem at drop                                                                                                                      |
| Growth stopped at steady state (3 live chunks, ~0.43 GB)  | 19.8 MiB/day, about 3.5 GiB over 6 months                                                                                                                             |
| Export staging per chunk                                  | measured peak 17.7 MB (gzip CSV plus Parquet) for a production-sized week; the reserve check uses the chunk size (145 MB) as its bound                                |
| Restore check                                             | measured: 907,200 rows restored in 3.8 s into a 62 MB table                                                                                                           |
| Storage Box                                               | 51.2 GB used of 1 TB; one week of LSR is a 5.0 MB Parquet file, about 0.26 GB a year. `db-*` dumps keep their own copies of the table for up to 6 months after a drop |

Measured on a disposable TimescaleDB (same pinned image) with a synthetic week of the
production shape (450 bases every 5 minutes, 907,200 rows, a 143 MB chunk against 145 MB
in production), on a workstation:

| Step                                                                                   | Result                                             |
| -------------------------------------------------------------------------------------- | -------------------------------------------------- |
| Export from one snapshot (count, fingerprint, COPY, Parquet, re-fingerprint)           | 7.3 s; Parquet 5.0 MB (28x smaller than the chunk) |
| Fingerprint of the chunk in PostgreSQL (the time a future drop holds the `SHARE` lock) | 3.7 s                                              |
| Fingerprint of the Parquet in DuckDB                                                   | 0.8 s                                              |
| Restore into PostgreSQL                                                                | 3.8 s                                              |

Synthetic values compress differently from real ones, and the server is slower than the
workstation; the first production export records its own numbers.

Context samples follow as the next stage (upserts and funding consumers).

## 3. Pilot design

**Dataset contract** (`lsr_history_v1`, one module, not a framework):

- source `app.live_long_short_ratio`;
- columns in table order with their PostgreSQL types;
- key `(exchange, base, ts)`;
- unit = one chunk `[range_start, range_end)` in UTC, taken from the chunk catalog;
- schema version, export version, hot window (14 days), readers and their lookback,
  protected windows (none, with the evidence above).

**Export:**

- One `REPEATABLE READ READ ONLY` transaction on psycopg, with `TimeZone=UTC` and
  `DateStyle=ISO`.
- From that one snapshot, the export reads: the row count, the data-key summary, the
  content fingerprint and `COPY ... TO STDOUT (FORMAT csv, FORCE_QUOTE *)`. The COPY
  is streamed into a gzip staging file.
- DuckDB converts the staging file to zstd Parquet with explicit column types:
  - `NUMERIC` is kept as its exact PostgreSQL text, never `DOUBLE`. DuckDB's
    `postgres` scanner reads an unconstrained `NUMERIC` as a double, so the cold-bar
    export path cannot be reused as-is.
  - `TIMESTAMPTZ` becomes a UTC microsecond timestamp.
  - `NULL` and empty strings stay distinct (`allow_quoted_nulls=false`).
- The file is renamed into place only after its row count and content fingerprint
  match the snapshot.

**Content fingerprint:**

- Each row is turned into a canonical string. Every column becomes `N` for `NULL`,
  or `V<length>:<text>` otherwise. Timestamps are written as
  `YYYY-MM-DDTHH:MM:SS.ffffff` in UTC.
- Each row string gets its own SHA-256; the result is the SHA-256 over those row
  hashes, sorted.
- The same expression runs in PostgreSQL on the source and in DuckDB on the
  Parquet. Equality proves the whole content, not just the row count.

**Manifest:** the contract and versions, chunk name and range, row count, file
bytes, file SHA-256, content fingerprint, data keys, snapshot time, code revision.
Written beside the file and stored in the catalog.

**Catalog** (Alembic):

- `app.history_archive_datasets` holds one row per (dataset, contract version, range,
  revision).
- States move forward only: `exported -> archived -> verified`. `superseded` is
  allowed from any state. `dropped` is reserved for the deletion PR.
- Database `CHECK`s require each state's evidence: the manifest at `exported`, the
  archive name at `archived`, and the extraction proof at `verified`.
- A trigger rejects any other transition and any change to content columns once
  exported.
- A partial unique index allows one live revision per range. Re-running a verified
  range does nothing. A re-export after a failure supersedes the earlier revision
  instead of overwriting it.

**Borg:**

- A host script creates `history-lsr-<UTC>` from the staging directory and verifies
  the archive listing. It is serialized by an flock, the same way as cold bars.
- The `history-*` prefix is never pruned. A test asserts that no prune glob in
  `offsite-backup.sh` matches it.
- Verification extracts the Parquet and its manifest from that named archive into a
  temporary directory. The Parquet must match the catalog's SHA-256 and content
  fingerprint; the manifest must match the catalog's manifest SHA-256 and describe the
  same contract, range, file and content. Only then does the catalog row move to
  `verified` (the database requires both extraction proofs, each `IS NOT NULL`).
  Local Parquet is removed only after that.

**Research access:** an explicit `fetch` downloads one dataset range from its
recorded archive into a cache with a byte cap and least-recently-used eviction. It
checks the free-space reserve first (as `cold_bar_fetch` does) and verifies the
SHA-256 before use.

**Deletion dry-run:** a per-chunk verdict with a reason for every blocker. The checks:

- the chunk lies beyond the hot window;
- its catalog row is `verified`;
- the live fingerprint still equals the manifest;
- the archive still exists;
- the fence is active up to the chunk end;
- no open pump episode's API window `[anchor - 4 h, anchor)` reaches into the chunk,
  where the anchor is `entry_qualified_at`, else `first_seen_at`, of every
  `app.pump_events` row with `closed_at IS NULL`;
- every registered reader is ready (see Q2);
- no protected window overlaps;
- the drop would remove exactly one chunk.

The deletion PR adds the execute path.

**Make targets, no timers:** `prod-history-lsr-export`, `prod-history-lsr-archive`
(host: borg create plus verify), `prod-history-lsr-fetch`,
`prod-history-lsr-deletion-dry-run`, and `history-lsr-restore-check` (a disposable
PostgreSQL).

## 4. The two required answers

**Q1. How is the dataset kept from changing between export and deletion?**

The current writer takes no lock. It can also insert an old `ts`: `limit=1` returns
the latest period Binance has. For a halted symbol, or one whose first poll comes
late, that period can be old, and `ON CONFLICT DO NOTHING` inserts it if the row is
missing. An advisory lock therefore proves nothing. The protocol does not depend on
the writer's cooperation:

1. **Export from one snapshot.** All export numbers come from the same
   `REPEATABLE READ` snapshot. A concurrent insert is either wholly in it or wholly
   outside it.
2. **Detection.** The dry-run and the future drop recompute the live fingerprint.
   Any row added since the export blocks the drop with "changed since export;
   re-export".
3. **Atomic verify-and-drop** (deletion PR). In one transaction:
   - `SET LOCAL lock_timeout`;
   - `LOCK TABLE app.live_long_short_ratio IN SHARE MODE`. It conflicts with the
     writer's `ROW EXCLUSIVE`, so it waits for in-flight inserts and blocks new ones
     until commit;
   - recompute the chunk fingerprint;
   - compare it to the manifest;
   - drop exactly that chunk;
   - raise the fence.
4. **Fence.** `app.history_archive_fences(dataset, closed_before)` plus a
   `BEFORE INSERT` trigger on the table. The trigger rejects any row with
   `ts < closed_before` with a dedicated SQLSTATE. The writer already logs failed
   inserts (`lsr.db_insert_failed`), so a late old row becomes a visible rejection.
   It can neither silently recreate a dropped chunk nor make the archive and the
   live table overlap. The trigger reads the fence row `FOR SHARE`, so a writer whose
   transaction snapshot predates a fence move gets a serialization failure instead of
   inserting under the old fence. The fence moves only to the end of the contiguous
   run of verified and dropped ranges from the start of the dataset: one archived
   chunk says nothing about the history below it. The migration installs the fence
   table empty and the trigger with it; with no fence row the trigger lets every row
   through. Raising a fence is a separate, approved action.

The pilot PR proves 1-4 against real PostgreSQL/TimescaleDB. It covers:

- a writer inserting during the export;
- an old-`ts` insert after the export, which the dry-run detects;
- a writer blocked by the `SHARE` lock until commit;
- a fenced insert being rejected;
- a long writer transaction holding a snapshot from before the fence move;
- a rerun of every step.

The deletion PR then only wires step 3 into a CLI.

**Q2. How does research read history once rows leave PostgreSQL?**

- Today no research code reads this table. The only reader is the API, which reads
  4 hours before the anchor of every open pump episode; the deletion gate protects
  those windows however old the episode is. The API never downloads archives in a
  request; data outside open windows can be archived and later dropped.
- The pilot adds `read_lsr(start, end)`. It reads `verified` archive ranges below
  the fence from fetched Parquet and the rest from PostgreSQL. The fence guarantees
  the two never overlap.
- The fence, the catalog and the live rows come from one snapshot. A prune commits
  its drop and its fence move together, so the reader reads the fence again after the
  snapshot; if it moved, the whole read is repeated (at most three times), never
  returned short.
- Below the fence the verified ranges must tile the requested interval exactly; a gap
  or an overlap is an error, not a shorter result. A week without any chunk has no
  catalog row and is therefore a gap: recording an empty week as evidence belongs to
  the deletion PR, together with the rule that the fence moves only over a contiguous
  verified prefix.
- A test fails the build if any analytics module other than the archive module
  queries `app.live_long_short_ratio` directly, so a new research reader cannot
  bypass the archive.
- The contract registers each reader with its lookback. The deletion gate stays
  blocked until every reader is either archive-aware or within the hot window.
- Losing data the API no longer reads (closed episodes) needs no API change; anything
  else is blocked by the gate rather than agreed away.

## 5. Checks the pilot PR will carry

All on a disposable PostgreSQL/TimescaleDB:

- export, then restore with every row matching both ways (`EXCEPT`), including
  `NULL`, exact `NUMERIC` text and microsecond timestamps;
- a concurrent writer during the export;
- an old-row insert after the export (detected);
- the `SHARE`-lock blocking and the fence rejection;
- rerun after a crash at each step: staging left behind, catalog left at
  `exported` or `archived`;
- an archive corrupted after creation (SHA-256 and fingerprint mismatch block
  `verified`);
- no space (the reserve refuses the export and the fetch);
- a protected-window overlap (blocked);
- catalog transition and immutability rejections.

Borg itself is faked in CI the way `test_offsite_backup_sh.py` does. A real local
Borg run is attached to the review if Borg can be installed locally.

## 6. Running the pilot

Manual make targets on the production host, no timers. Each prints a JSON report and
exits non-zero on a failed range.

1. `make prod-history-lsr-export ARGS='--max-chunks 1'`: closed chunks (ended more
   than a day ago) without a live catalog row, oldest first; refuses a dirty tree.
2. `make prod-history-lsr-archive`: one new `history-lsr-<UTC>` archive of every
   `exported` range; the listing must match exactly or the archive is deleted.
3. `make prod-history-lsr-verify`: extract each `archived` range from its archive,
   recheck SHA-256 and content, mark `verified`, remove the local Parquet.
4. `make prod-history-lsr-fetch FROM=... TO=...`: verified ranges into the capped cache.
5. `make prod-history-lsr-deletion-dry-run`: the per-chunk blockers; it deletes nothing.

Every step can be rerun after a failure: a range only advances when its evidence is in
place, and a lost local export is superseded and exported again. A Borg lock held by
the nightly backup fails the archive or verify step without touching the catalog; run
it again later. The first production run is a separate, approved operation.

## 7. Measurement follow-up

The 1.16-day reading is repeated (same query, at least three more points over a week)
before any disk conclusion or rewrite plan. A rewrite plan would state the confirmed
saving, free space for the temporary copy and WAL, the lock duration, the backup and
the stop criterion, and how the churn is contained so the bloat does not return.

## 8. HYP-015 inputs

The registered reader (`momentum_flow_hold12h_verdict_reader`) takes its schemas as
parameters (`timeseries`, `app`). The plan reuses the pilot's export, Borg and
restore steps for the watch-evaluation chunks of the cohort window
(2026-10-05..11-02) and adds a restore into a separate schema of a disposable
database that the registered reader can be pointed at. It is proven by a restore check
that runs the reader's readiness path only, never the verdict, before 2026-11-04
12:00 UTC. Deadline: archived and restore-checked before 2026-11-12, a week before
the first cohort chunk would drop. If that slips, the fallback is a temporary
retention extension for that hypertable (about 0.1 GB a day), an approved production
policy change.

## 9. Out of scope

Any deletion, fence activation or production policy change; production timers;
context samples, decisions, outcomes and probes; the churn fix for probes and samples;
compression policies. Each comes as its own reviewed change.
