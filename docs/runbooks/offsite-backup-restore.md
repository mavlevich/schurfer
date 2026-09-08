# Runbook: offsite backup and restore

What to do at three in the morning. Decisions and their reasons are in
[ADR-0010](../adr/0010-offsite-backup-hetzner-storage-box.md); this file is only
the procedure.

## Where things are

| Thing                 | Location                                                   |
| --------------------- | ---------------------------------------------------------- |
| Repository            | `ssh://u664974-sub1@u664974.your-storagebox.de:23/./borg`  |
| Connection config     | `/opt/schurfer/runtime/backup.env` (mode 600)              |
| Repository passphrase | `/opt/schurfer/runtime/borg-passphrase` (mode 600)         |
| Exported repo key     | `/opt/schurfer/runtime/borg-repokey.txt` (mode 600)        |
| SSH key               | `/home/deploy/.ssh/schurfer_storagebox`                    |
| Pinned host key       | `/opt/schurfer/runtime/storagebox_known_hosts`             |
| Success stamps        | `/opt/schurfer/runtime/offsite-backup-{db,research}.stamp` |

The passphrase also exists in the owner's password manager and as an offline
copy. **Both the passphrase and the exported key are required to read an
archive, and neither can be recovered from the other or from Hetzner.** The
export exists because a `repokey` repository stores its key inside itself: a
damaged repository would otherwise take the key with it.

Everything below assumes the environment is loaded:

```bash
set -a; . /opt/schurfer/runtime/backup.env; set +a
```

Run as root. The research archive covers files the analytics container writes as
`root:root` mode 600; as `deploy` you will read a fraction of them and Borg will
report a warning rather than an error.

## Routine checks

List what exists, newest last:

```bash
sudo bash -c 'set -a; . /opt/schurfer/runtime/backup.env; set +a; borg list'
```

Verify repository integrity, including the stored data rather than just the
index. This is not a restore test and does not substitute for one:

```bash
sudo bash -c 'set -a; . /opt/schurfer/runtime/backup.env; set +a; borg check --verify-data'
```

Run one backup by hand:

```bash
sudo systemctl start schurfer-offsite-backup.service
```

Check health the way the alert does:

```bash
sudo /opt/schurfer/infra/scripts/offsite-backup-health.sh
```

## Restoring the database

Restore into a **throwaway instance**, never over the live database. The
existing `infra/scripts/restore-local.sh` is a different procedure: it pulls a
local dump from production and overwrites a local database, and it does not go
through this repository.

### 1. Extract the dump

The database archive holds a single file, `schurfer.dump`, in PostgreSQL custom
format.

```bash
sudo bash -c 'set -a; . /opt/schurfer/runtime/backup.env; set +a; \
  cd /var/tmp && borg extract --stdout ::db-YYYY-MM-DDTHH:MM:SS > schurfer.dump'
```

Substitute a real archive name from `borg list`. The extracted file is about
40 GB, so pick a filesystem with room, and delete it afterwards.

### 2. Start an isolated PostgreSQL with TimescaleDB

Use the same image as production. Do not point this at the production volume,
the production port, or the production network.

### 3. Restore with the TimescaleDB wrapper

**A plain `pg_restore` is not sufficient.** `pg_dump` warns that
`_timescaledb_catalog.continuous_agg` has circular foreign-key constraints, and
TimescaleDB requires its catalogue to be put into restore mode around the load:

```sql
SELECT timescaledb_pre_restore();
```

```bash
pg_restore -U schurfer -d schurfer --no-owner schurfer.dump
```

```sql
SELECT timescaledb_post_restore();
```

If `pg_restore` reports errors that are not ownership-related, stop and read
them. A restore that "mostly worked" is the failure this whole procedure exists
to detect.

### 4. Verify, and record that you did

A restore is not verified because it finished without errors. Check at minimum:

- hypertables exist and report chunks: `SELECT * FROM timescaledb_information.hypertables;`
- row counts and time ranges on `timeseries.bybit_momentum_bars_1m`, `app.trade_decisions`, `app.trades`
- the retention and compression policies are present: `SELECT * FROM timescaledb_information.jobs;`
- one real research query returns a plausible result

Record the date, the archive name and the outcome. An untested backup and a
backup tested a year ago are close to the same thing.

## Restoring research inputs

Paths inside the archive are relative (`runtime/...`, `backups/...`). Extract
into a **new, empty directory** and inspect before moving anything: extracting
from `/` would scatter `runtime/` and `backups/` into the filesystem root.

```bash
sudo mkdir -p /var/tmp/schurfer-restore && cd /var/tmp/schurfer-restore
```

```bash
sudo bash -c 'set -a; . /opt/schurfer/runtime/backup.env; set +a; \
  cd /var/tmp/schurfer-restore && borg extract ::research-YYYY-MM-DDTHH:MM:SS'
```

Check what landed before touching the live tree:

```bash
find /var/tmp/schurfer-restore -type f | wc -l && ls /var/tmp/schurfer-restore
```

Only then move the parts you actually need into `/opt/schurfer`, preserving
ownership, and delete the staging directory afterwards.

## Replacing the SSH key

Rotate by event, not by calendar: the key was exposed, the host changed, or
someone else gained access to production. There is no rotation schedule and
adding one would buy little, because revocation is immediate.

On the production host:

```bash
ssh-keygen -t ed25519 -N "" -C "schurfer-prod-borg" -f ~/.ssh/schurfer_storagebox.new
```

Install the new public key on the box, using the sub-account password from the
password manager:

```bash
ssh-copy-id -s -p 23 -i ~/.ssh/schurfer_storagebox.new.pub u664974-sub1@u664974.your-storagebox.de
```

Confirm the new key works before removing the old one:

```bash
ssh -p 23 -i ~/.ssh/schurfer_storagebox.new -o BatchMode=yes u664974-sub1@u664974.your-storagebox.de df -h
```

Then swap the files, remove the old public key from
`prod-borg/.ssh/authorized_keys` on the box, and run one backup by hand.

## When the Storage Box is unreachable

Backups fail and the health check alerts after 36 hours. Nothing is deleted from
PostgreSQL by this system, so the immediate risk is disk, not data loss.

The health check also alerts below 15 GB free. If both fire at once, the disk is
filling while the offsite path is down, and something has to give. In order of
preference:

1. Fix the connection to the box.
2. Reclaim disk (`docker builder prune -af` frees a few GB at the cost of slower
   next builds).
3. Only then consider letting Timescale retention delete unexported chunks, and
   record what was lost.

## Verification log

Record every verification here: the date, what was checked, and the outcome. An
untested backup and a backup tested a year ago are close to the same thing.

### 2026-09-08 -- archive integrity, no restore

Archive `db-2026-09-08T07:00:54`.

- **Read back end to end:** 40,534,809,206 bytes streamed out of the repository,
  matching the 40.53 GB the archive records as its original size.
- **`borg check --verify-data`:** exit 0. This reads and verifies every chunk,
  not just the index.
- **Dump structure:** `pg_restore --list` on the streamed dump returns 1989 TOC
  entries, `Format: CUSTOM`, `Compression: none` (so `-Z0` took effect), 203
  `TABLE DATA` entries, and the `timescaledb` extension with its compressed
  hypertables present.

**Not verified: that a database actually comes up from it.** A full restore
needs roughly 27 GB of free disk on a host that is not production, and
production has 24 GB free. `pg_restore --list` reads only the table of contents
at the start of the file and then closes the pipe, so it says the dump's header
is valid, not that its contents are complete -- the end-to-end read and
`--verify-data` are what cover that.

Until a restore has been performed, step 2 of the storage plan is not done, and
the local dump stays.

### 2026-09-08 -- restore performed, three mechanisms verified

Archive `db-2026-09-08T07:00:54`, restored into a throwaway container
(`timescale/timescaledb:latest-pg17`, `--network none`, own volume, no published
port) on the production host. The dump was streamed straight out of the
repository into `pg_restore`, so nothing landed on disk.

Restored in three passes, each with `timescaledb_pre_restore()` before and
`timescaledb_post_restore()` after:

1. **Schema, policies and application data.** All 5 hypertables, all 4
   columnstore policies and all 4 retention policies came back. 941 trades,
   488,356 decisions, 3,481,214 outcomes, 12,685 pump events, with date ranges
   matching production. A real research query -- exit reasons for closed paper
   `pump_short` trades since 2026-08-18 -- returned the same distribution as
   production.
2. **Uncompressed chunk data.** Writing into `_timescaledb_internal` chunk
   tables under `timescaledb.restoring` works: 35,396 rows.
3. **Compressed chunk data.** The columnstore path, which carries most of the
   real data -- 27 of 29 bar chunks are compressed.

`mfe_at` and `mae_at` were absent, correctly: the archive predates migration 0046.

**The one thing that will mislead you.** After pass 3 the hypertable still
reported 35,396 rows and the compressed chunk looked empty. It was not: the
database was still in restoring mode, because the restore script had aborted on
`set -e` before reaching `timescaledb_post_restore()`. Running it manually took
the count to 38,579. **Compressed data is invisible until `post_restore` runs**,
so a restore that ends without it looks like compressed data failed to restore
rather than like an unfinished restore. Check `SHOW timescaledb.restoring;`
before concluding anything about missing rows.

Note also that `borg extract --stdout | pg_restore` raises `BrokenPipeError` in
Borg whenever `pg_restore` finishes early, which it does for any partial
selection. That is expected, not a failure of the archive -- but under
`set -euo pipefail` it will abort the script, which is exactly how the
`post_restore` above got skipped.

**Not covered: a full-volume restore.** These passes deliberately excluded the
bulk chunk data, because a complete restore needs roughly 27 GB free on a host
that is not production and production has 24 GB. What remains untested is
therefore volume and duration, not correctness: every mechanism the dump relies
on has now been exercised against real data.

## What this does not cover yet

Parquet export of cold minute bars before Timescale retention drops them, and
the controlled deletion that replaces the automatic retention policy. Until that
exists, minute bars older than 35 days are gone regardless of these backups: the
dump only contains what is still in the database when it runs.
