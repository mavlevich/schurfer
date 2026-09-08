# ADR-0010: Offsite backup to a Hetzner Storage Box with Borg

Status: accepted, 2026-09-08.

## Context

Backups were local only: a nightly `pg_dump | gzip` into `/opt/schurfer/backups`
on the same disk as the database it protects, with `RETENTION_COUNT=1`. That has
two independent problems.

**It does not survive the host.** One disk holds the database, the only backup,
and everything else. Any loss of that host is total.

**It blocks deploys.** `backup.sh` refuses to run unless free space is at least
twice the previous backup's size, which is correct: the new dump has to be
written before the old one can be dropped. On 2026-09-08 that guard failed a
deploy twice, short by 136 MB and then by 320 MB, on a 75 GB disk holding a
27 GB database volume and a 12 GB dump. The documented remedy
(`make prod-docker-prune-run`) reclaimed 173 MB, because the build cache was all
newer than its own 168h staleness filter. The deploy only proceeded after a full
`docker builder prune -af`, which is not a remedy but a cost paid in rebuild
time.

Neither problem gets better on its own: the database grows daily.

A third fact decided the shape rather than the existence of this: minute bars
are dropped after 35 days by a Timescale retention policy, and
`runtime/market-path-cache` is not in the PostgreSQL dump at all. History is
being deleted on a schedule, and part of what makes frozen research
reproducible lives outside the thing we back up.

## Decision

Back up offsite to a Hetzner Storage Box (BX11, 1 TB) using BorgBackup, in
addition to the existing local path, which stays until a restore has actually
been performed from the new repository.

**Streaming, not a local file.** `pg_dump` feeds Borg directly, so the dump
never lands on the local disk. This is what removes the deploy blocker: there is
no longer a large artifact needing twice its own size in free space.

**`--content-from-command`, not a shell pipe.** A pipe lets Borg see a clean EOF
when `pg_dump` dies partway and store a truncated dump as a complete archive.
Verified here on 2026-09-08: a producer writing partial output and exiting 3
makes Borg exit 2 and leave no archive at all.

**`pg_dump -Z0`.** The custom format compresses by default. Handing Borg an
already-compressed stream dedups badly and pays for the same work twice. Borg
compresses instead, with `zstd,3`.

**Two archive families with separate retention.** `db-*` on 7 daily / 4 weekly /
6 monthly; `research-*` on 7 / 8 / 24. The research family covers
`runtime/market-path-cache`, `runtime/research-dataset-artifacts` and
`backups/reports` -- the inputs a frozen report needs to be reproducible, none
of which are in the dump. A report frozen a year ago is only reproducible while
the candles behind it still exist somewhere, so these must outlive the dump
schedule.

**The job runs as root.** The analytics container writes
`runtime/market-path-cache` as `root:root` mode 600. The first run of this
backup, as `deploy`, archived 57 of 1624 research files and exited 1: a warning,
not an error.

**Completeness is checked against an explicit file list.** The list is captured
once and handed to Borg with `--paths-from-stdin`, which archives exactly those
paths and no others; the archive's contents are then compared to that same list
as a set, not as a count. Two matching counts do not establish matching
contents, and letting Borg walk the directories itself made the check race
against the containers writing them: a new cache entry appearing mid-run made a
perfectly good archive look wrong.

The limit of what this proves is worth stating, because an earlier draft of this
ADR overstated it. It establishes that every file found under the three listed
paths reached the archive. It does not notice a fourth directory that nobody
added to the list, and it says nothing about file contents beyond what Borg's
own integrity checking covers. Running as root is what makes the check pass
rather than what makes it correct, but neither one turns this into a guarantee
that everything worth keeping is being kept.

**The alert watches the last successful archive, not the last run.** Stamp files
are written only after an archive succeeded. A unit that fires punctually and
does nothing is a failure this repository has already shipped: the Docker prune
unit was silently a no-op for months because of a permissions error.

**Access is scoped so that a compromised production host cannot destroy
history.** A dedicated Storage Box sub-account, restricted to its own directory,
with its own SSH key generated on the production host and used for nothing else.
Samba and WebDAV are off on both the box and the sub-account; SSH is the only
protocol enabled. External reachability is off, because the production host
reaches the box over Hetzner's internal network. Storage Box snapshots are
scheduled and managed by the main account, which production has no access to.

That last point matters because Borg's own protections are not sufficient for
it: repository encryption does not extend to files sitting beside the
repository, and append-only mode still permits logical deletion of archives.
The acceptance criterion is stated in terms of capability, not configuration:
production credentials must not be able to destroy previously stored history
through any connection method available to them.

## Alternatives considered

**Raising the Timescale retention window instead.** Cheaper, and it does address
the research bottleneck, but it consumes the same disk that already fails
deploys, and it does nothing about surviving the host.

**Plain `rsync` of the existing gzip dumps.** Simplest, and the wrong shape: a
12 GB gzip changes wholesale every night, so a year of history would cost
roughly a year of full copies. Deduplication is the reason for Borg.

**`borg serve --append-only` as the primary protection.** Kept as a future
hardening step, not relied on: it permits logical deletion, and it protects only
the repository, not other files reachable by the same credentials. Snapshots
under a separate account are the stronger guarantee and were available
immediately.

## Consequences

The local backup path and the offsite path both run for now. That is deliberate:
an unverified offsite archive is not a reason to drop the only verified copy.
Switching the deploy gate to the offsite path, and retiring the local dump, is a
separate change, gated on an actual restore.

That restore is not a formality. `pg_dump` warns about circular foreign-key
constraints in `_timescaledb_catalog.continuous_agg`, so a plain `pg_restore`
may not reproduce a working database. The existing `restore-local.sh` does
exactly a plain `pg_restore`, with no `timescaledb_pre_restore()` /
`timescaledb_post_restore()` wrapping, and it restores from a locally downloaded
dump rather than from the repository. Verifying the offsite path needs a
separate procedure into an isolated instance, checking schema, data ranges and a
few working queries.

Cost is roughly 4 EUR per month. Measured on the first archive: 40.50 GB
streamed, 11.19 GB stored, 15 minutes 21 seconds.
