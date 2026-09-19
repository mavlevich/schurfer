# Cold-bar provenance audit — 2026-09-19

A read-only provenance reconciliation of the `timeseries.bybit_momentum_bars_1m` cold-bar history,
run before the gated-deletion PR2 arms real deletion. This is a **provenance record**, not a list of
"lost days": every day with a readable, manifest-consistent cold copy is recorded as such.

Nothing was deleted, no receipt or `fidelity_verified` was created, and no production data was
changed. Borg was used for `list`/`extract` only; extracts were streamed and discarded.

## Preconditions (verified)

- Old retention job `1001` on `bybit_momentum_bars_1m` is `scheduled = false` (paused). It must stay
  paused; PR2 removes the policy entirely. (Retention jobs 1003/1005/1007 on other hypertables remain
  active and are out of scope: bars only in v1.)
- Disk: 24 GB free. PG chunks: 37 contiguous single-UTC-day chunks, 2026-08-14 .. 2026-09-19, no drift.

## Per-day evidence

| Day(s)               | Chunk in PG                                                 | Manifest     | Fingerprint | Receipt | Receipt offsite                                                            | Parquet in named archive         | Extract SHA     | Read-back                      | Class                                                        |
| -------------------- | ----------------------------------------------------------- | ------------ | ----------- | ------- | -------------------------------------------------------------------------- | -------------------------------- | --------------- | ------------------------------ | ------------------------------------------------------------ |
| 2026-08-10 / 11 / 12 | no                                                          | yes (legacy) | **no**      | no      | n/a                                                                        | yes (`bars-2026-09-08T14:17:07`) | == manifest sha | rows == manifest               | **archived_readable, `source_fidelity=unverifiable_legacy`** |
| 2026-08-13           | no (aged past 35d before job 1001 paused; NOT gate-dropped) | yes          | yes         | yes     | yes                                                                        | yes                              | OK              | —                              | fingerprinted + receipted + offsite; not in PG               |
| 2026-08-14           | yes                                                         | yes          | yes         | yes     | **yes**                                                                    | yes                              | OK              | 742,981 == manifest == receipt | **PROVABLE NOW**                                             |
| 2026-08-15 .. 09-18  | yes                                                         | yes          | yes         | yes     | **pending cycle-2** (receipts written `09-19T04:30`, archived next backup) | yes (`09-19T04:30`)              | 08-15 spot OK   | —                              | **provable after the next backup**                           |
| 2026-09-19           | yes                                                         | no (today)   | —           | no      | —                                                                          | —                                | —               | —                              | today; not yet exported, not a candidate                     |

The three `unverifiable_legacy` days have a per-day machine-readable record here:
[bars-2026-08-10](bars-2026-08-10.provenance.json), [bars-2026-08-11](bars-2026-08-11.provenance.json),
[bars-2026-08-12](bars-2026-08-12.provenance.json).

## Conclusion

- **Provable** (safe to gate-drop once past the 40-day cutoff): 2026-08-14 now; 2026-08-15 .. 09-18
  after the next offsite backup archives their receipts (the known two-cycle property; not corruption).
  2026-08-13 is fingerprinted + receipted + offsite-confirmed but was removed from PG by the old
  automatic retention (before job 1001 was paused), NOT by the gated path.
- **Needs repair:** none among in-PG days. The only "not yet" is the cycle-2 receipt-offsite timing,
  which self-resolves on the next backup. Re-run this audit afterwards to confirm.
- **Unverifiable legacy (permanent provenance gap):** 2026-08-10 / 11 / 12. A readable, manifest-
  consistent cold copy exists, but the source is gone from PG and no fingerprint was ever recorded, so
  faithful capture of the source cannot be proven. No `data_missing` day was found.
- No in-PG chunk is eligible at the 40-day cutoff yet (oldest range_end is 2026-08-15); no deletion
  pressure.

## Research-use restriction

The `unverifiable_legacy` days (2026-08-10 / 11 / 12) are **NOT admissible as formal research
evidence**. A matching Parquet SHA (recorded in the manifest) and a successful read prove only an intact, readable cold copy
consistent with its manifest; they do not prove the file faithfully captured the now-deleted source.
Do not backfill a receipt or `fidelity_verified` for them. Any research use must treat these days as
provenance-gapped.

## Reproducing (read-only)

- Retention job state: `SELECT job_id, scheduled FROM timescaledb_information.jobs WHERE proc_name='policy_retention'`.
- Chunks: `SELECT range_start, range_end FROM timescaledb_information.chunks WHERE hypertable_name='bybit_momentum_bars_1m'`.
- Offsite integrity: `borg extract --stdout <repo>::<archive> runtime/cold-bars/bars-<day>.parquet | sha256sum` compared to the receipt/manifest SHA.
- Read-back: DuckDB `read_parquet` row count of the extracted file, compared to the manifest/receipt `row_count`.
