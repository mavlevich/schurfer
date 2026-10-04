# HYP-015 reader inputs: archive and restore readiness v1

Status: **DRAFT, design review 1 folded in (2026-10-04).** No code yet. Builds on the history archive pilot
([history-archive-design-v1](history-archive-design-v1.md), merged in #488). This work
computes no verdict, opens no formal-read claim and reads no return, fee, funding or
PnL value.

## Why

The registered HYP-015 reader (`momentum_flow_hold12h_verdict_reader`) reads
`timeseries.momentum_flow_watch_evaluations_1m`, a hypertable with daily chunks on
`bucket_start` and a 45-day Timescale retention policy. The cohort window is
2026-10-05..11-02 and the read opens on 2026-11-04 12:00 UTC. The cohort's first
daily chunk drops on 2026-11-19. If the read slips past that date, the denominator
(every eligible WATCH decision) loses rows and the registered verdict can no longer
be computed as specified. An archive alone does not close this: the registered reader
has to be able to run on what is restored.

## Reader inputs (from the code)

The reader takes one `Schemas(timeseries, app)` pair, so a restore needs both schemas.

| Table                                                                  | Read by                                       | Rows needed                                                                                             | Retention                                     |
| ---------------------------------------------------------------------- | --------------------------------------------- | ------------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| `timeseries.momentum_flow_watch_evaluations_1m`                        | readiness, health, formal                     | WATCH decisions of the hold12h watch version, exchange and market type with `decision_at` in the window | **45 days**: cohort rows drop from 2026-11-19 |
| `app.momentum_flow_paper_probes`                                       | readiness (statuses, market id), formal       | probes of the hold12h paper version                                                                     | none                                          |
| `app.momentum_flow_paper_outcomes`                                     | formal                                        | outcomes of those probes                                                                                | none                                          |
| `app.momentum_universe_snapshots`, `app.momentum_universe_instruments` | readiness, formal (identity at decision time) | the latest snapshot at or before each decision, so snapshots before the cohort too                      | none                                          |
| `app.hold12h_funding_coverage_runs`, `app.hold12h_funding_settlements` | health, formal (actual funding)               | coverage and settlements over each holding window                                                       | none                                          |
| `app.hold12h_formal_read_claims`                                       | formal (written by the read itself)           | none for readiness                                                                                      | none                                          |

Column types to carry exactly: `uuid`, `text[]`, `bytea`, `boolean`, `integer`,
`bigint`, `double precision`, `jsonb`, `varchar`, `text`, `timestamptz`.

## Design

1. **Extend the pilot's contract for its second real consumer, and no further.** A
   contract pins its columns as (name, PostgreSQL type) from `information_schema`; an
   export whose live columns differ fails. Timestamp columns stay UTC microsecond
   timestamps; every other column is stored as its exact PostgreSQL text and restored
   by casting that text back, so `double precision` (shortest exact output), arrays,
   `bytea` (hex) and `jsonb` round-trip unchanged. The content fingerprint runs over
   the same canonical text in both engines, as in the pilot.
2. **Watch evaluations (at risk):** dataset `hyp015_watch_evaluations`, one catalog
   row per closed daily chunk from 2026-10-04 to 2026-11-02 inclusive (one day of
   margin before the window, because the filter is on `decision_at` and the chunks on
   `bucket_start`). Export, Borg archive and extraction verify exactly as in the
   pilot. No fence and no deletion: Timescale retention is the deletion, so every
   chunk must be `verified` before its 45th day.
3. **Plain inputs (not at risk, needed for a restore):** every plain input table is
   exported from **one** `REPEATABLE READ` snapshot, as one snapshot set:
   - each table is its own dataset (`hyp015_paper_probes`, `hyp015_paper_outcomes`,
     `hyp015_universe_snapshots`, `hyp015_universe_instruments`,
     `hyp015_funding_coverage_runs`, `hyp015_funding_settlements`);
   - the export is a superset of what the reader selects: all probes and outcomes of
     the hold12h paper version, and the universe and funding tables whole (2.5 MB and
     17 MB today);
   - the manifest states `unit = snapshot`, the filter, `snapshot_at` and the set id;
     a restore is accepted only for a complete set.

   A snapshot covers "the rows that existed at `snapshot_at`", not a time range, so
   the catalog gets a small migration (0059): a `unit` column (`chunk` or `snapshot`)
   and a `snapshot_set` column; a snapshot row has no range and one live revision per
   dataset and set. 0059 is deployed together with 0058.

   **Timing.** A snapshot at 2026-11-02 12:00 UTC is only preliminary: funding settles
   after the holding windows close, which is why the registered read waits until
   2026-11-04 12:00 UTC. Two things are checked separately, both blind:
   - **inputs preserved** (decides whether a set is final): every member dataset is
     verified, the watch revisions are pinned and still match the source, and every
     cohort event is in the archive whatever its outcome. Rejected entries
     (`rejected_stale`, `rejected_quote`) and unresolved events are kept as they are;
     a cohort that needs no outcome rows may have none;
   - **formal readiness** (reported, never required for preservation): for the events
     that need them only, opened probes are closed, closed positions have both
     registered horizons (240 and 720) complete, accounting is complete, and funding
     coverage is `complete`.

   The final set is the first one taken after readiness holds (expected from
   2026-11-04); if readiness never holds, the last preserved set is final and the
   archive records the unresolved events honestly. A later set replaces an earlier one
   only once the later set is complete and verified.

4. **Composition reference, bound to the snapshot and to pinned watch revisions.**
   Inside the same export transaction:
   - pin the verified watch revisions of the window (catalog ids, file SHA-256,
     content fingerprints) and recompute each chunk's fingerprint from the source in
     this snapshot; any difference aborts the set, the chunk is re-exported as a new
     revision, and the set is taken again;
   - compute a blind composition summary and record it, with the pinned revisions, in
     the set's manifest and set row. It reads no return, fee, funding amount or price:
   - SHA-256 of the sorted eligible WATCH ids, and their count;
   - SHA-256 of the sorted hold12h probe ids, with counts by `entry_status`,
     `position_status` and `accounting_status`;
   - outcomes counted by (`horizon_minutes`, `status`, `accounting_status`); every
     closed probe must have both registered horizons (240 and 720);
   - funding coverage runs counted by `status`, per probe window;
   - universe snapshots and instruments by count and id hash.
5. **Restore check:**
   - the watch chunks of the window hold about 13 GB uncompressed, too much to restore
     whole on this host and not needed: each chunk's completeness is already proven by
     its verified fingerprint. The check restores, from the pinned watch revisions, the
     rows the reader's denominator selects, plus a complete plain-input set, into a
     throwaway TimescaleDB container on the compose network (the ENG-025 pattern; the
     production database is not touched), into two schemas with the pinned columns;
   - compare each restored plain table's content fingerprint with its catalog row;
   - recompute the composition summary on the restored schemas and require it to
     equal the reference;
   - run the registered reader's readiness path (`load_readiness`) on the restored
     schemas and require its counts to agree with the reference (total WATCH, entries
     opened, positions closed). Readiness is never compared against the live database,
     which keeps changing;
   - never call `load_cohort`, `open_formal_claim` or the formal CLI; a test asserts
     the new targets cannot reach them.
6. **Late inserts into watch chunks.** A closed chunk can still receive a late row.
   The set's pinning step (4) is the recheck: it runs in the set's own snapshot, so the
   composition reference and the pinned revisions always agree. A changed chunk is
   superseded and exported again before a new set is taken.
7. **Order and dates:**
   - code PR, then migrations 0058 and 0059 in the next planned deploy window after a
     verified backup (a separate approved operation);
   - archive the watch chunks of the window (2026-10-04..11-02, one day of margin) in
     manual batches as they close;
   - a first restore check on the chunks archived so far plus a preliminary plain-input
     set, well before 2026-11-12;
   - the final set after the completeness checks pass (expected from 2026-11-04), the
     late-insert recheck, and the full restore check by **2026-11-12**, a week before
     the first cohort chunk would drop. If that slips, the fallback is a temporary
     retention extension on the watch hypertable (about 0.1 GB a day), an approved
     production change.

## Blindness

Export copies and hashes rows; no code path in this work selects a return, fee,
funding amount, price or PnL column into a computation, a log or an output. The
composition summary and readiness read ids, counts, statuses, horizons and identity
only. Fingerprints are hashes over whole rows and reveal no value.

## Review 1 answers folded in

- Snapshots are catalogued per table under one snapshot set, from one snapshot, and
  restored only as a complete set; they do not pretend to a time range (0059).
- The restore proof is fingerprints, the composition summary bound to the snapshot,
  and readiness on the restored data; not a comparison with the changing live
  database.
- Watch scope is the cohort window with one day of margin; early archives are
  rechecked against the source before the final check.

## Review 2 answers folded in

- Preservation and formal readiness are separate; rejected and unresolved events are
  preserved, and only the events that need outcomes, accounting or funding are
  checked for them.
- A set pins its watch revisions and checks them against the source in its own
  snapshot; a changed watch chunk forces a new revision and a new set.
- Migration 0059: conditional constraints for `chunk` and `snapshot` rows (a snapshot
  has no range and may be empty), separate uniqueness of live snapshots per dataset and
  set, `unit` and `snapshot_set` immutable, and a set table whose rows move
  `building -> verified -> superseded`, where a set is verified only when every
  required member is verified and is superseded only by a verified newer set. The LSR
  pilot's tests run unchanged as regressions.

## Checks

Real PostgreSQL/TimescaleDB:

- exact round trip of every listed type, including `NULL`, empty arrays, `NaN` and
  infinities in `double precision`;
- the composition summary and readiness on restored data equal the reference recorded
  at export, on seeded data;
- an incomplete set, an empty outcomes or funding archive, or a probe without both
  horizons fails the check;
- a late insert into a verified watch chunk is detected and re-exported as a new
  revision;
- 0059: a snapshot row needs a set and no range, a chunk row needs a range;
- a missing, superseded or corrupted archived input fails the check;
- a schema drift in a pinned column fails the export;
- the formal path is unreachable from the new targets.
