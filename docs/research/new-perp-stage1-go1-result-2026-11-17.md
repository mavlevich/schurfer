# I25 stage 1, Go 1 result (2026-11-17)

**Status: record of one coverage-level read. No price, rate, volume or return was read, and no
verdict beyond the Go 1 report is claimed.** Go 1 is the metadata-only step of the
[stage 1 request list](new-perp-stage1-request-list-v1.md). It was run on the owner's go from a
clean `f6eb5c3` (PR 613), bound to `request_list_sha256`
`023b9863d1af73dbeaff5cc7cf64d0e77f3c0fdba295e4158f7c0e54982d6fe3` and the go 1 approval hash
`5f4d84d3d4294276fa2f91c144e8ab0f12999bd734642986c1b4ab91d213ece1`. Go 2 has **not** been run.

## What ran

- **Window and cut:** discovery year only, 2024-10-01 to 2025-09-30. Nothing dated from
  2025-10-01 was read, recorded or counted.
- **Requests:** 526 attempts, 526 requests, no retry, no stop, outcome `go1_complete`:
  C1 4 (catalog, linear), C3 1 (archive root), D1 263 (per-symbol listings; 262 answered, 1
  answered 404), D2h 258 (HEAD of the first daily file: status and size, no body).
- **Bytes:** 11,401,574 (11.4 MB) in total, all in C1, C3 and D1; the HEADs carry no body.
- **Time:** from 21:26:50 to 21:35:39 UTC on 2026-11-17.
- **Run root:** outside Git, sealed (`i25-stage1-v1`), with `phase0.json`, the receipts and
  `preflight.json`. Receipts hold timestamps, existence, sizes and counts only.

## Result (the go 1 report, aggregate only)

| Item                                          | Value                                                                                           |
| --------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| In-window launches                            | 263                                                                                             |
| M1 by file name alone                         | 236 first files still to read, 22 over the cap, 4 `premarket_or_ambiguous`, 1 no archive        |
| M2 (i) no archive file within a day of launch | 5 of 263 (1.90%), limit 5%                                                                      |
| M2 (ii) archive-only symbols                  | **`not_assessed`**: see below                                                                   |
| M3, provisional                               | 258 survivors, 0 `relist`, 0 `multiplier_ambiguous`, 0 `ticker_reuse_suspected`, 0 unverifiable |
| M6, a **partial** check, catalog fields only  | 259 of 259 feasible; it does not show that an order opens                                       |
| Provisional stop codes                        | none                                                                                            |
| Not assessable after go 1                     | first-trade times, path days, funding, bars, `n_primary`                                        |

**M2 (ii) is not assessed.** The archive-root page carries no directory modification dates
(`modified_dates_present` is false), so no directory could be proven to have its last file inside
the window, and none was kept (an unknown date is not allowed, protocol Amendment 1). The archive-only
share is therefore unknown, not low. M2 (ii) stays a lower bound at best, and here it is not even
that.

## The 22 over-cap launches: a selection limit, stated plainly

**22 of the 258 first daily files are over the 64 MiB per-file cap** of the request list as
frozen, so those 22 launches would have had no `T0` and would have dropped out of the sample
under the old cap. The measured sizes, from the `Content-Length` of the HEAD receipts:

- 258 files, **6,425.47 MiB** in total (6.275 GiB);
- the 22 over-cap files total 2,908.84 MiB, the largest 408.70 MiB (428,549,908 bytes);
- 236 files (3,516.63 MiB) are at most 64 MiB; 256 files (5.498 GiB) are under 256 MiB; **all
  258 are under 512 MiB**.

Large first-day files are launches with heavy first-day trading, so a 64 MiB cap would have
selected against exactly the most active launches. That is a selection limit, and it is the
reason for the cap amendment (PR 615), which sets one per-file cap of 512 MiB and rebuilds the
contract. The names of the 22 launches are not published: they stay in `preflight.json` in the
sealed run root. **Go 2 was not run at 64 MiB.**

## Manifest

`manifest.json` (counts only, no symbol): 263 launches, 0 archive candidates, 236 first-file
requests under the old cap, an entry-candidate upper bound of 258, a preflight of 3,687,449,174
bytes over 236 files with 22 over the per-file cap. Its SHA-256 is
`72395099c6110427f4b841cfb6bf4a477ed142bc829b6c0905fed158105317a0`. The manifest and the go 1
report are kept as [`manifest.json.gz`](evidence/new-perp-stage1-go1-2026-11-17/manifest.json.gz)
and [`go1-report.json.gz`](evidence/new-perp-stage1-go1-2026-11-17/go1-report.json.gz) (gzip of
the exact files, so the SHA-256 of the unzipped file can be checked: the report's is
`cb0e3a5daf552f0271563d96fb2505f1aa124ec6af259d3c22405dc121789a91`).
**`preflight.json` is kept out of Git:** it lists the per-symbol file sizes and the names of the
22 over-cap launches, a first-day activity proxy that the record does not need.

## What this does not say

This is a go 1 report, not a stage 1 verdict. It does not read M1 first-trade classes, M4
funding, M5 bars or `n_primary`, and its provisional M3 and M6 results rest on catalog fields.
Because the contract is amended (the per-file cap is in the shared rules, so all three hashes
change), Go 1 is **rerun in a new root** under the new hashes and Go 2 is approved on the new
manifest, as decided in the amendment. This read stays in the record as the first read.
