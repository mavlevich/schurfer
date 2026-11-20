# I25 Stage 1b metadata result (2026-11-20)

**Status: record of one coverage-level read on the sealed backward holdout. No M1, M3, M4, M5 or
M6 pass is shown, this is not a Stage 1b pass, and no price, funding rate, return or exam outcome
was read.** The run is the metadata step of the
[Stage 1b metadata contract](new-perp-stage1b-metadata-v1.md), merged in PR #626. The body phase
is **not authorized** and has not been run.

## Authorization and provenance

- **Who authorized it:** the owner, confirmed in chat to the coordinating session on 2026-11-20.
  The run itself happened before this documentation session, in another working session, overnight
  from 00:15 to 00:53 Europe/Warsaw on 2026-11-20 (`finished_at_utc` 2026-11-19T23:53:05Z).
- **This PR makes no request.** It was written from the sealed aggregate report only. No network
  request was made. Besides the report, the run root's top-level file list and sizes were read, and
  `manifest.json` was opened only to checksum it and count its keys (no key is published). The
  `catalog/`, `admitted/`, `attempts/` and `receipts/` folders were not opened.
- **Revision:** clean `e01d67ce32cb46826cb570102db76b193055fa80` (`git_clean_before` true).
- **Contract:** rules `new_perp_stage1b_metadata_v2`, request-list SHA-256
  `31ae1a57970e7af98d2e52fd75534cf2604cd4a79bb889f25cd2f6a90fbada96`. The binding to the
  prerequisite discovery v3 report (`38b569aa5dc713179ded3db70f7cdf788cea560db7c0c99c65d9332a879070b8`) is recorded valid.
- **Run root:** `i25-stage1b-metadata-v2-e01d67c`, outside Git and sealed.

## What ran

- **Window:** backward holdout `[2023-02-01, 2024-09-16)`. No price series of the discovery
  year, the HYP-031 test year or the HYP-012 v2 blind window was requested. The shared metadata
  responses (the catalog and the HTML listings) can physically hold entries outside the holdout;
  their content was filtered by the registered rules during processing.
- **Requests:** 1,402 attempts, all outcome `ok`, no retry, no stop, `complete` true:
  C1 4 (catalog, linear), C3 1 (archive root), D1 1,150 (folder listings), D2h 247 (HEAD of the
  first in-window daily file: status and size, no body). The ceiling was 2,400 attempts.
- **Bytes:** 53,956,397 response bytes (about 51.5 MiB) of the 448 MiB cap: C1 1,880,630, C3
  105,062, D1 51,970,705, D2h 0 (HEAD has no body).
- **Outcome:** `metadata_complete_coverage_pending`, `binding_valid` true, `stop_reason` null,
  `known_coverage_failures` empty, `body_phase_authorized` false.

## Result (aggregate only)

| Item                                        | Value                                                                          |
| ------------------------------------------- | ------------------------------------------------------------------------------ |
| Catalog launches in the window              | 254 (slots 254)                                                                |
| M1 by file dates alone                      | 247 `csv_pending`, 7 `premarket_or_ambiguous` (all `t0_ambiguous`)             |
| M2 (i) no file within 1 day of launch       | 7 of 254 (2.76%), 0 unproven, no known coverage failure                        |
| M2 (ii) admitted observed file series       | 191: 1 archive-only in window, 190 pre-window                                  |
| M2 (ii) observed share                      | 0.0039 (1 of 255); `archive_only_proven` **false**                             |
| M2 (ii) unverified or colliding directories | 0 and 0                                                                        |
| M3 catalog states                           | 254 `not_contradicted`; ticker reuse suspected: 1                              |
| First-file HEADs                            | 247, all ok, 2,323,690,559 bytes known (2.164 GiB), 0 unknown or absent        |
| `not_assessed`                              | `m1_csv_pending`, `m3_historical_identity_not_proven`, `m4_m5_m6_body_pending` |

The 7 `premarket_or_ambiguous` launches and the 1 ticker-reuse suspicion are counts only; which
launches they are stays in the sealed root.

## What this does not show

- **No M1 result.** First-trade time cannot be read from a daily filename, so 247 launches await
  the CSV. The day predicates here are not an actual `T0`.
- **No M3 pass.** The catalog states say only that the current catalog did not contradict a
  launch. Historical identity (renames, relists, reused tickers) is not proven.
- **No M4, M5 or M6.** Funding coverage, entry and hourly bar coverage and executability checks
  need the body phase.
- **Not a Stage 1b pass.** The metadata step cannot return `stage1b_pass_partial` by design.
- **No price, funding, return or exam read** of any kind. No CSV GET, bars, funding, books or AWS
  was requested.

## Limits

- **M2 (ii) is an observed file series, not a census.** It counts archive file prefixes seen in
  the root listing, not verified instruments, and it cannot prove that no real launch is missing.
  `archive_only_proven` is false.
- **M1 awaits the CSV.** The 247 `csv_pending` launches have no proven `T0`.
- **Historical identity is not proven.** The catalog is a **current snapshot** of the venue, so
  delisted, renamed or relisted instruments may be missing or misdescribed.
- **Size is a known fact, not a read.** The 2.16 GiB is the sum of HEAD `Content-Length` values;
  it is the cost estimate for a body phase, not a body read.

## Evidence in Git

[`evidence/new-perp-stage1b-metadata-v2-result-2026-11-20/`](evidence/new-perp-stage1b-metadata-v2-result-2026-11-20/)
holds three files, aggregate only:

| File                      | SHA-256                                                            | Note                       |
| ------------------------- | ------------------------------------------------------------------ | -------------------------- |
| `metadata-report.json.gz` | `95007f511d708420126d3b2915d811ec4cb54e4310e715538cacafc766f89df9` | gzip of the exact report   |
| (unzipped report)         | `173347ae6785dd2fe5a5bbbce7e6fdfc10b82db37d98908e7ba0fc6f64458352` | check after `gunzip`       |
| `evidence-manifest.json`  | not self-referential                                               | checksums and counts below |

**The run manifest is not published.** It holds 254 opaque catalog keys and 191 opaque
admitted-series keys. They are hex digests, but whether they resist guessing from known ticker
lists was not verified, so only the manifest's SHA-256
`f3098e0840be489df640fbdab66c464257bac3d6aa5315d58e70d86d594d1341` (it matches the report's
`manifest_sha256`) and its counts (254 and 191) are recorded.

**Scan of the published report:** no symbol name (zero `USDT` hits) and no per-launch date. The
only dates are the run finish time and the two window bounds. Other text is file checksums of
the code and contract files bound to the run. The `catalog/`, `admitted/`, `attempts/` and
`receipts/` folders, `evidence-hashes.json`, `run.json`, `contract.json` and `metadata_done.json`
stay sealed outside Git.

## Next step

The body phase for the holdout (CSV reads for M1 first-trade times, then M4, M5 and M6) **needs its
own design, a review and the owner's go**; nothing here authorizes it. The holdout stays **one
shared sealed exam batch**: no holdout fact may tune Stage 3 parameters, and no holdout result
may be read before the exam batch is frozen.
