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

## If a fix to the backup script will not deploy

It will, now, and this note is here because it did not before 2026-09-08.

`prod-deploy` used to back up before pulling. The backup therefore ran from the
tree already on the host, so a fix to `offsite-backup.sh` could only arrive
through a deploy, and a deploy could not get past the broken backup to pull it.
Three deploys died that way in one day, each needing a manual `git pull
--ff-only origin main` on the host before `make prod-deploy` would work.

The order is now pull, then back up. The invariant is unchanged -- a backup
still exists before migrations, which are step 4 -- and `git pull` does not
touch the database. The deadlock is gone, and a broken backup script now fails
its own deploy instead of the next person's.

If you ever meet this shape again on some other guard, the manual escape is the
same: pull on the host first, then run the deploy.

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

## Checking bar coverage

The health check watches three stamps and, separately, whether the nightly cold
bar export is keeping up:

```bash
sudo /opt/schurfer/infra/scripts/offsite-backup-health.sh
```

It reads the manifests in `/opt/schurfer/runtime/cold-bars`, not the Parquet
files. The backup deletes each `.parquet` once it is confirmed inside a `bars-*`
archive and leaves the `.manifest.json` beside it, so the manifests are a
permanent local index of which days were exported: answerable without the
repository passphrase, without reaching the Storage Box, and without reading
8 GB of Parquet to answer a question about filenames.

Failures, reported separately:

| Message                                                               | What actually broke                                                                                              |
| --------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `last successful cold bars archive is Nh old`                         | The export or the archive stopped. The days themselves may still be in the database.                             |
| `N cold bar day(s) inside the 35-day retention window have no export` | Specific days are missing. Each names a deadline.                                                                |
| `no collection start recorded`                                        | `cold-bars/collection-start` is gone. Coverage cannot be checked at all, so the check fails rather than passing. |

**Why the start file exists.** The expected range used to begin at the oldest
surviving manifest, which meant deleting the five oldest manifests shrank the
expected range to match and the check stayed green while five unrecoverable days
had gone missing. The exporter writes `collection-start` on its first run and it
is then read, never re-derived. Expected coverage runs from the later of that
date and the retention edge.

**After deploying this for the first time**, run the exporter once so the file
exists; until then the check fails closed every hour, which is the intended
behaviour and not a reason to weaken it:

```bash
sudo make prod-cold-bar-export
```

**A missing day is recoverable until retention deletes it.** Re-run the export
for that day:

```bash
sudo make prod-cold-bar-export
```

The exporter skips days it has already written, so a plain run backfills every
gap still inside the window. Days already dropped from the database cannot be
recovered by any means this system has, which is why the alert is hourly rather
than daily.

## What this does not cover yet

**The retention policy is still automatic.** Migration 0024 sets
`add_retention_policy(timeseries.bybit_momentum_bars_1m, INTERVAL '35 days')`,
and it deletes on schedule whether or not the day was exported. Nothing checks
the export before the deletion, and the two mechanisms do not know about each
other.

What the health check above buys is time, not a guarantee: a broken exporter is
now reported within a day or two, and every skipped day stays recoverable for the
rest of its 35 days. What it does not do is prevent the deletion of a day nobody
looked at. Gated deletion -- replacing the automatic policy with an explicit
`drop_chunks` that runs only after that day's export is verified and confirmed
offsite -- is the remaining step, and it removes the case where the alert is
simply ignored for five weeks.
