# HYP-015 reader inputs: archive and restore readiness v1

Status: **DRAFT for design review.** No code yet. Builds on the history archive pilot
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
3. **Plain inputs (not at risk, needed for a restore):** one point-in-time snapshot
   export per table, as a superset of what the reader selects: all probes and
   outcomes of the hold12h paper version, and the universe and funding tables whole
   (2.5 MB and 17 MB today). Taken after the last holding window closes
   (2026-11-02 12:00 UTC), archived and verified the same way.
4. **Restore readiness check:**
   - restore every archived input into a disposable PostgreSQL on the host (the
     ENG-025 drill's throwaway container), into two schemas created with the
     production DDL;
   - compare each restored table's content fingerprint with its catalog row;
   - run the reader's **readiness** path (`load_readiness`, counts, statuses and
     identity only) against the restored schemas and against production, over the
     cohort window, and require identical summaries;
   - never call `load_cohort`, `open_formal_claim` or the formal CLI; a test asserts
     the new targets cannot reach them.
5. **Order and dates:**
   - deploy migration 0058 (with a backup; it also installs the inert LSR fence
     trigger), a separate approved operation;
   - archive the watch chunks as they close, in manual batches;
   - run a first restore readiness check on the days archived so far, to prove the
     path early;
   - after 2026-11-02 12:00 UTC, snapshot the plain inputs and run the full check;
   - done by **2026-11-12**, a week before the first cohort chunk drops. If that slips,
     the fallback is a temporary retention extension on the watch hypertable (about
     0.1 GB a day), an approved production change.

## Blindness

Export copies and hashes rows; no code path in this work selects a return, fee,
funding or PnL column into a computation, a log or an output. Readiness reads counts,
statuses and identity only. Fingerprints are hashes over whole rows and reveal no
value.

## Questions for review

1. A snapshot of a plain table is not a chunk. Proposal: catalog it with
   `chunk_name = 'snapshot'` and `range = [cohort window start, snapshot instant)`,
   with the snapshot's filter recorded in the manifest. Acceptable, or should snapshots
   get their own catalog shape?
2. Is identical readiness on restored and live data, plus per-table fingerprints, a
   sufficient proof that the registered reader can run on the archive, given the
   formal path may not be exercised before 2026-11-04 12:00 UTC?
3. Should the watch archive cover only the cohort window, or every daily chunk before
   retention drops it (general history at about 0.1 GB a day compressed)? Proposal:
   the cohort window now; general history later, as its own decision.

## Checks

Real PostgreSQL/TimescaleDB:

- exact round trip of every listed type, including `NULL`, empty arrays, `NaN` and
  infinities in `double precision`;
- restored readiness equals source readiness on seeded data;
- a missing, superseded or corrupted archived input fails the check;
- a schema drift in a pinned column fails the export;
- the formal path is unreachable from the new targets.
