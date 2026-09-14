# Cold-bar gated deletion — design v1 (DRAFT, NOT FROZEN)

> **STATUS: DESIGN DRAFT — NOT FROZEN, NO CODE YET.** Colleague verdict: approve-after-changes;
> this revision folds in the three required corrections. It is the spec for the eventual PR, to be
> code-reviewed together. Nothing here changes production until the delivery order below is executed.

## Problem

Migration 0024 attaches an automatic Timescale retention policy
`add_retention_policy(timeseries.bybit_momentum_bars_1m, INTERVAL '35 days')`. It drops chunks on
schedule **without checking whether that day was exported to Parquet and confirmed present offsite**.
Nothing links the exporter, the offsite backup, and the deletion. When the exporter silently failed
(2026-09-11, NoNewPrivileges bug), Sep 11-12 were saved only because they were still inside the 35-day
window — the automatic policy would have deleted an unexported day with no second copy. Replace the
automatic policy with **gated deletion**: a day's chunk is dropped only after that exact day is proven
exported, verified, and present in a named offsite archive, and proven unchanged since export.

## Fact base (verified 2026-09-14, read-only)

- **1-day chunks already exist.** Migration 0024 creates `bybit_momentum_bars_1m` with a 1-day
  `chunk_time_interval`, so chunks align to export days; NO chunk migration is needed. `drop_chunks`
  removes only chunks lying entirely before the cutoff, which matches the per-day export model.
  Before enabling deletion, confirm on prod (read-only) the actual chunk `range_start/range_end` and
  that no config drift changed the interval.
- **The local Parquet is deleted after upload.** `infra/scripts/offsite-backup.sh` removes each
  day's local `bars-<date>.parquet` once it is confirmed in the Borg archive, keeping only the tiny
  manifest. So at D+40 the local Parquet is gone, and **no later archive re-lists that day** (bars
  archives are never pruned; a day lives only in the archives written while its Parquet existed).
  Consequence: a "does the LATEST bars archive contain day D" check cannot work, and a high-water-mark
  ("offsite ok up to date X") is unsafe — it would re-mask a single-day hole like Sep 11-12.

## Design

### Two distinct gate points

**A. Before deleting the local Parquet (in the export/backup flow) — reader-check MANDATORY:**

1. Parquet + manifest exist; size, SHA-256, schema version, and row count match the manifest.
2. The Parquet actually **reads back** (a real reader opens it and reads rows) — mandatory, not optional.
3. Borg-archive the cold-bars directory.
4. Write an **atomic per-day offsite receipt** recording: day, Borg archive name/ID, the parquet path
   inside the archive, parquet SHA-256, row count, schema version, and the source **row fingerprint**
   (see below).
5. Only then delete the local Parquet. (Manifest stays forever.)

**B. Before dropping a day's chunk (the gated-drop job):**

1. The local manifest for that day exists.
2. A per-day **offsite receipt** for exactly that day exists locally **and is itself present in a named
   offsite archive** (the receipt is archived by the backup that runs after it is written; if its
   offsite copy is not confirmed by the time of the drop, the drop is blocked).
3. **SHA proof by extraction, not by listing (P1 #4).** `borg list` shows names only. The job
   **extracts** the specific `bars-<day>.parquet` and its manifest from the receipt's named archive
   (`borg extract` to a temp path or stream), recomputes SHA-256, and requires it to equal the receipt's
   recorded hash for BOTH files. A name/size match from `borg list` is not sufficient proof.
4. **Source unchanged since export (P1 #2).** Between export (D+1) and drop (D+40) a backfill/repair
   could have changed the day's rows. The fingerprint is an **order-independent aggregate over every
   row of the day** of `(primary key, payload_hash)`. `payload_hash` already exists and, by the writer
   (`apps/collector/internal/momentumcapture/writer.go`), **intentionally excludes `created_at`** — so
   the fingerprint compares BAR DATA, not ingestion metadata, and does not churn on a created_at-only
   rewrite; row additions/deletions still change the PK-set and therefore the aggregate. The gated-drop
   **recomputes** this from the live table and requires equality with the receipt. Mismatch → the day's
   data changed → **no drop**, emit `needs_versioned_reexport` (a new versioned export must supersede
   the stale archive first).
5. **Exact drop set (P1 #3).** `drop_chunks(older_than=X)` deletes EVERY chunk fully before `X`, not one
   named chunk, so a broad call keyed on the cutoff would also delete an unvalidated day inside the
   range. The job therefore NEVER issues a cutoff-wide `drop_chunks`. It computes the ordered candidate
   set, validates it, and drops each passing chunk **individually and targeted**
   (`drop_chunks(hypertable, older_than=chunk.range_end, newer_than=chunk.range_start)`), asserting the
   affected set equals exactly that one chunk. It advances only across a **contiguous validated prefix**:
   the first day that fails any check halts advancement, and every older validated day is dropped one by
   one; nothing past the first failure is touched.

Any failed check on any day → that day is **not dropped**, it stays in the hot DB as long as needed,
and an explicit per-day refusal reason is recorded. Fail-closed: the worst case is disk grows (caught
by the existing disk-runway alert), never destruction of unconfirmed data.

### Cutoff

Eligibility = chunks with `range_end <= (UTC-midnight today) - 40 days`. The 40-day buffer beyond the
35-day export/verify window means a single failed day does not race a deadline; it waits for repair
while the disk alert fires. Cutoff computed at UTC-midnight, deterministic. Eligibility only makes a
chunk a CANDIDATE; the actual drop still requires the per-day gate B and is issued per-chunk (never as a
cutoff-wide call), so the set Timescale removes is provably exactly the validated chunks.

### The gated-drop job

- A **separate systemd service + timer**, run AFTER the daily export and offsite backup (e.g. 05:00,
  export 03:30, backup 04:00). NOT embedded in `prod-deploy` or in the backup script.
- **`--dry-run` by default**: it prints the candidate list and per-day verdicts and drops nothing.
  Active deletion is a separate, explicit enablement (a flag/env), turned on only after a dry-run has
  been reconciled on prod.
- **Configurable cutoff for reconciliation (P1 #1).** The dataset only began ~2026-08-10, so at a
  40-day cutoff there are NO eligible chunks yet and a plain dry-run would validate nothing. The job
  takes a `--cutoff-days` parameter (default 40) so a dry-run can reconcile the gate against real,
  already-exported days at a smaller offset (e.g. 20-25d) and exercise every check — extraction, SHA,
  fingerprint, set-equality — before the real 40-day boundary arrives. Only the ACTIVE (deleting) job
  is pinned to 40; the reconciliation dry-run may use a smaller offset because it deletes nothing.
- **Singleton** via a Postgres advisory lock (like the other workers) so two runs never overlap.
- Emits, every run: the candidate day list, and for each a pass verdict or an explicit refusal reason.

### Migration

- A migration **removes** the automatic 35-day retention policy from `bybit_momentum_bars_1m`.
- Its **downgrade must NOT silently re-add the unsafe automatic policy** (that would re-introduce
  ungated deletion on a rollback). Downgrade either leaves retention app-managed or fails loudly.

## Scope

First PR: **`bybit_momentum_bars_1m` only.** Do not generalize to the other retention hypertables
(watch*evaluations 45d, liquidation*\* 180d) until this one path has run a full cycle in production.

## Delivery order

1. Restore Sep 11-12 [DONE, evidence]: manually backfilled 2026-09-11 (1,491,770 rows, 349.4 MB, sha256
   381f85d40c6d2145…), 2026-09-12 (1,492,184 rows, 329.5 MB, sha256 0d978c90d7ec61e0…), 2026-09-13
   (1,492,034 rows, 378.4 MB, sha256 f26877d2f49c9107…); confirmed in offsite archive
   `bars-2026-09-14T18:08:31`; `offsite-backup-health.sh` returned exit 0 ("healthy; 27GB free; 35 bar
   days exported") on 2026-09-14T18:07 UTC.
2. Implement the validator, per-day receipts, and the gated-drop job in **dry-run**; unit-test the
   validator (fingerprint match/mismatch, missing receipt, archive-missing, reader-fail, cutoff math).
3. Dry-run on production; reconcile chunks ↔ days ↔ receipts; confirm chunk ranges and no drift.
4. Migration removing the automatic 35-day policy.
5. Enable the gated timer with the 40-day cutoff.
6. After the first real deletion: verify DB size, remaining chunk ranges, Borg archives, manifests,
   alerts, and that a research reader still reads the boundary day.

## Open items for review

- Exact shape of the row fingerprint (PK columns + `payload_hash`); whether `payload_hash` already
  exists per row or must be added.
- Receipt storage: DECIDED — an **immutable, versioned per-day JSON next to the manifest** in
  `runtime/cold-bars/` is the canonical source (mirrors the manifest pattern, and is itself archived to
  Borg by the next backup so gate B can require its offsite presence). A small DB table, if added, is an
  OPTIONAL index only, never the source of truth.
- `payload_hash` already exists per row (no new column); the fingerprint reuses it. Its deliberate
  exclusion of `created_at` (writer.go) is a feature here, not a gap — see gate B #4.
