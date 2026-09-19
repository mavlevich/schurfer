# Cold-bar gated deletion — design v1 (DRAFT, NOT FROZEN)

> **STATUS: PR2 CODE LANDED (pending review); deletion NOT yet enabled in production.** The pure
> gate, export fingerprint, dry-run job/collectors (PR1), and now the PR2 code — migration 0050
> removing the automatic 35-day retention, the real targeted `drop_chunk` (advisory-lock-guarded,
> re-fingerprint-before-drop, exactly-one-chunk with rollback), the `--execute` flag, and real-PG
> tests — are all in. Deletion stays OFF by default: the CLI is dry-run unless `--execute`, and the
> systemd timer remains dry-run. The three enablement gates (legacy fingerprint backfill,
> daily-volume benchmark, real Postgres→DuckDB parity test) are DONE. Turning deletion on is an
> OPERATIONAL rollout (verified backup → migrate → confirm the old job is gone → prod dry-run → one
> authorised canary drop → verify → enable the 40-day timer), gated separately. A read-only
> provenance audit precedes it: docs/engineering/audits/2026-09-19/.

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

## Provenance audit (2026-09-19)

A read-only per-day reconciliation before PR2 arms deletion:
[docs/engineering/audits/2026-09-19/](../engineering/audits/2026-09-19/README.md). Result: 2026-08-14
provable now; 2026-08-15..09-18 provable after the next backup (cycle-2 receipt timing, not
corruption); 2026-08-10/11/12 are `unverifiable_legacy` (readable manifest-consistent cold copy, but
source gone and no fingerprint ever recorded). The legacy days are **NOT admissible as formal research
evidence** and must never get a backfilled receipt or `fidelity_verified`. No `data_missing` day found.

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
4. **Content intact + source unchanged since export (P1 #1, #4, #2-corrected).** The fingerprint is a
   VERSIONED (`FINGERPRINT_VERSION`), SHA-256, order-independent aggregate over a per-row hash of the
   WHOLE row (`to_json(row)`, every column) — NOT `payload_hash`, which does not cover every exported
   column, and INCLUDING `created_at`, which is research-significant (it defines availability; see
   `net-buy-accumulation-discovery-v2.md`). So any changed, added, or removed row is detected. Two
   fingerprints are recorded at export: `source_fingerprint` (over the live source) and
   `file_fingerprint` (over the exported Parquet); their equality at export (`fidelity_verified`)
   proves the file faithfully captured the source (P1 #1) — recorded, never a hard export failure, so
   a cross-engine serialization quirk cannot break the exporter. The drop gate requires
   `fidelity_verified`, recomputes `file_fingerprint` from the EXTRACTED offsite parquet (content
   intact, order-independent — stronger than the whole-file byte sha), and recomputes
   `source_fingerprint` from the live table (unchanged since export). Any mismatch → **no drop**;
   a source change emits `needs_versioned_reexport`.
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

- A **separate systemd service + timer** (`schurfer-cold-bar-gated-deletion.{service,timer}`,
  Makefile `prod-cold-bar-gated-deletion-dry-run` / `-install`), run AFTER the daily export and
  offsite backup (05:00; export 03:30, backup 04:00). NOT embedded in `prod-deploy` or the backup
  script. The unit is hardened (`NoNewPrivileges`) and the make target uses NO in-target sudo (the
  bug that broke the export before). **Borg access (decided: run in the analytics container).** The
  analytics image ships `borgbackup`; the make target MOUNTS the offsite credentials/config at their
  same host paths (`backup.env`, `/home/deploy/.ssh/schurfer_storagebox`, `storagebox_known_hosts`,
  `borg-passphrase`, `BORG_BASE_DIR=/opt/schurfer/runtime/borg-home`) so `BORG_RSH`/`BORG_PASSCOMMAND`
  /`BORG_BASE_DIR` from `backup.env` resolve unchanged. This is acceptable because it is a single
  single-tenant box under one `deploy` user (the key already lives there), the job is READ-ONLY
  (borg list/extract; `drop_chunk` disabled until PR 2), and the offsite repo is a Hetzner Storage
  Box **sub-account** (`-sub1`) which can be scoped/append-only-restricted. End-to-end reachability
  (container -> storagebox) is validated the first time the job runs after deploy. PR 1 ships DRY-RUN only.
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
  [DONE: migration `0050_remove_bars_auto_retention`, `remove_retention_policy(... if_exists)`.]
- Its **downgrade must NOT silently re-add the unsafe automatic policy** (that would re-introduce
  ungated deletion on a rollback). [DONE: the 0050 downgrade FAILS LOUDLY — it raises rather than
  restoring the policy; retention is application-managed via the gated job.]

## Scope

First PR: **`bybit_momentum_bars_1m` only.** Do not generalize to the other retention hypertables
(watch*evaluations 45d, liquidation*\* 180d) until this one path has run a full cycle in production.

## Delivery order

1. Restore Sep 11-12 [DONE, evidence]: manually backfilled 2026-09-11 (1,491,770 rows, 349.4 MB, sha256
   381f85d40c6d2145…), 2026-09-12 (1,492,184 rows, 329.5 MB, sha256 0d978c90d7ec61e0…), 2026-09-13
   (1,492,034 rows, 378.4 MB, sha256 f26877d2f49c9107…); confirmed in offsite archive
   `bars-2026-09-14T18:08:31`; `offsite-backup-health.sh` returned exit 0 ("healthy; 27GB free; 35 bar
   days exported") on 2026-09-14T18:07 UTC.
2. Implement the validator, per-day receipts, and the gated-drop job in **dry-run** + unit tests.
   [DONE, PR1.]
3. Dry-run on production; reconcile chunks ↔ days ↔ receipts; confirm chunk ranges and no drift.
   [DONE: commissioned 2026-09-15; provenance audit 2026-09-19 (docs/engineering/audits/2026-09-19/).]
4. Migration removing the automatic 35-day policy + real targeted `drop_chunk` + `--execute` + tests.
   [DONE, PR2 — this PR; deletion still off by default.]
5. Enable the gated timer with the 40-day cutoff. [OPERATIONAL, after review/deploy: verified backup
   → migrate → confirm old job gone → prod dry-run → one authorised canary drop → verify → enable.]
6. After the first real deletion: verify DB size, remaining chunk ranges, Borg archives, manifests,
   alerts, and that a research reader still reads the boundary day. [OPERATIONAL.]

## PR 2 correctness requirement: close the fingerprint TOCTOU [DONE]

Implemented in `drop_one_chunk_under_lock` (cold_bar_gated_deletion_collectors.py). In one
transaction the drop takes `pg_advisory_xact_lock(COLD_BAR_MUTATION_LOCK_KEY)`, re-derives the
source fingerprint WHILE holding the lock and requires it to still equal the receipt's, then issues
the targeted `drop_chunks` (bounded to the one chunk) and asserts exactly one chunk was removed
(else ROLLBACK + raise). The lock auto-releases at transaction end.

**Writer-protocol verification (required for the lock to mean anything).** The lock only closes the
race if every late writer/repair of these bars takes the same key. Verified 2026-09-19: the SOLE
writer of `timeseries.bybit_momentum_bars_1m` is the Go collector
(`apps/collector/internal/momentumcapture/writer.go`); its INSERT is a no-op `ON CONFLICT`
(existing rows are immutable), it appends only current-minute buckets, and gaps are RECORDED, not
backfilled. So it can neither mutate nor insert into a chunk old enough to be a deletion candidate,
and is exempt. There is NO historical backfill/repair path today; if one is added it MUST take
`COLD_BAR_MUTATION_LOCK_KEY` before writing an eligible day (documented at the constant). Proven by
real-PG tests (`test_cold_bar_gated_deletion_drop_integration.py`): a concurrent holder of the key
blocks the drop (`LockNotAvailable`), a changed source aborts it, a multi-chunk affected set is
rolled back, and a targeted drop removes exactly one chunk.

## Gates before enabling deletion (must all pass)

- **Daily-volume benchmark.** DONE (2026-09-18, read-only prod run). On a real day (2026-09-17,
  1,491,376 rows / 371 MB) the whole-row `to_json` fingerprint added +177s (+134%) on top of the
  132.6s baseline export, ~5 min total for a once-nightly job: well within budget. `--with-fingerprint`
  is now ON in the nightly export (`make prod-cold-bar-export`).
- **Legacy fingerprint backfill.** MECHANISM READY. Manifests exported before fingerprints existed have
  no `source_fingerprint`, so the gate blocks their days forever and the contiguous prefix can never
  advance past them. `cold-bar-export --refresh-fingerprints` (`make prod-cold-bar-export-refresh-
fingerprints`, batch with `ARGS='--max-days N'`) re-exports the in-window days that lack a fingerprint,
  oldest first; it is self-terminating and safe to re-run. Must complete for the in-window days before
  the automatic retention is removed (PR 2).
- **Real PostgreSQL→DuckDB integration test.** DONE. `test_cold_bar_fingerprint_parity_integration.py`
  builds a real Postgres table over the type mix that could serialize differently (timestamptz, double,
  integer[], double precision[], bytea, boolean, NULLs, empty arrays), exports it to Parquet, and asserts
  `source_fingerprint == file_fingerprint` — the cross-engine `to_json` parity `fidelity_verified` relies
  on. It skips without a local Postgres and runs in CI (same pattern as the other `*_integration` tests).
  The daily-volume benchmark independently confirmed the same equality on 1.49M real production rows.

**Concurrency.** The exporter and the fingerprint backfill WRITE the cold-bar Parquet the offsite backup
ARCHIVES and RECLAIMS. `infra/scripts/with-cold-bars-lock.sh` wraps both writers, and `offsite-backup.sh`
takes the same lock around its cold-bar section, so a reclaim can never delete a day out from under an
in-flight export. Fail-closed: a writer that cannot take the lock does not run (retried next run), and a
backup that cannot take it skips only its bars section (archived next run), never blocking the DB dump.

## Open items for review

- Receipt storage: DECIDED — an **immutable, versioned per-day JSON next to the manifest** in
  `runtime/cold-bars/` is the canonical source (mirrors the manifest pattern, and is itself archived to
  Borg by the next backup so gate B can require its offsite presence). A small DB table, if added, is an
  OPTIONAL index only, never the source of truth.
- The fingerprint hashes the WHOLE row via `to_json` rather than reusing `payload_hash` (which does
  not cover every column) and deliberately INCLUDES `created_at` (research-significant for
  availability). Row-hash serialization is `to_json`; if a future need arises for a stricter canonical
  form (explicit per-column casts), bump `FINGERPRINT_VERSION`.
