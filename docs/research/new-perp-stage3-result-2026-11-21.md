# I25 Stage 3 result and stop (2026-11-21)

**Status: record of one features-level read on the discovery year and of the resulting stop.
Verdict `insufficient_power`, stop code 6 of protocol section 8. I25 `new-perp-short-v1` stops
here: no body or outcome read of the backward holdout happens, Stage 1b Go A and Go B are not
asked for, and no return of the short strategy was read or computed.** This is not evidence that
the idea has no edge. The planning MDE is above the pre-registered 400 bps ceiling, so the study
stops under this contract; no evidence about profitability was obtained.

The run is the Stage 3 contract of [new-perp-stage3-power-calibration-v1.md](new-perp-stage3-power-calibration-v1.md),
merged in PR #629 (revision `67ba33f854fb51ae5e47359493909a6f28620010`, clean tree).

## Authorization and provenance

- **Who authorized it:** the owner, in chat on 2026-11-21, for the exact line shown to them:
  228 requests, at most 684 attempts, 48 MiB cap, `request_list_sha256`
  `3392f75267e684e46093cbc0fb55e0714036df585a98d220b416dae4594a08d9`, `go_sha256["1"]`
  `701fcfd51b9d10f0e4093ef9eeb585b8a97a95e66bba7685dcfd9666dead7daf`, cohort SHA-256
  `60aa139bf33bf129ee9ff5c530c47a5151fa99b9a712c59e90e82a8285ebaa4c`.
- **First attempt:** the first launch of the command failed before any request, because the code
  resolves its pinned documents by relative path and the command had been started from
  `apps/analytics`. The run root was empty afterwards (no receipt, no attempt). The command was
  repeated from the repository root with the same arguments and a fresh root; that run is the one
  recorded here.
- **Run root:** `i25-stage3-67ba33f`, mode 0700, outside Git and sealed. Nothing from its per-launch
  `attempts/` or `receipts/` folders was opened for this note; only the stored aggregate
  `report.json` was read.
- **Source fingerprint** of the sealed Stage 1 root: `b4226e63093d0cfdfdd7fa388d93a49af9f93e21fb5a50615c71271e9501ad54`.

## What ran

- **Window:** the discovery year only. One hourly trade-bar page per primary V1 episode, covering
  the whole hours between `ceil_hour(T0 + 1 d)` and `floor_hour(E)`, so every bar ends at or before
  the entry minute. Nothing at or after any entry minute, no exit or hold-week bar, nothing from
  2025-10-01 or from 2026-09-29, no mark price, no funding, no order book, no minute bar, no holdout.
- **Requests:** 228 attempts, 228 outcome `ok`, no retry, no stop, no API error code.
- **Bytes:** 2,667,639 response bytes of the 48 MiB (50,331,648) cap.
- **What was computed:** one log return per episode over the pre-entry days, scaled to a
  seven-day equivalent `z = L * sqrt(168 / h)` and also expressed as a simple return in bps
  (`1e4 * expm1(z)`); dispersion and week-block dependence only. Those per-episode values stay in
  the sealed root. The report's
  `returns_read` is false: no return of the short trade (entry to exit) exists anywhere.

## Result (aggregates only)

| Item                                        | Value                                                                                                                |
| ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Episodes, ISO weeks, entered at `k` = 8     | 228, 48, 215                                                                                                         |
| Seven-day-equivalent log return, sd         | 0.393 (`sd_log_7d`)                                                                                                  |
| Same, as simple return in bps, sd           | about 4,603 bps (`sd_bps`); the SE and MDE below use this scale; top 5 episodes hold 55.0% of its squared deviations |
| Design effect (largest of blocks 2, 4, 8)   | 1.84 (block 2); effective sample about 123.9                                                                         |
| Standard error of the mean                  | 413.6 bps                                                                                                            |
| MDE, V1 (7 days): `2.80 * SE`               | **1,158.0 bps**, against the 400 bps limit: fails                                                                    |
| MDE, V2 (3 days) and V3 (14 days)           | 1,163.1 and 1,176.2 bps (extrapolated from the V1 SE, conditional)                                                   |
| Sensitivity of the V1 MDE to the multiplier | 878.0 bps at 2.123 and 1,028.5 bps at 2.487: both still fail                                                         |
| Hurdle (5% a year) on `k` = 8 slots         | 18.6 bps per trade; the MDE is 62 times that                                                                         |
| Annual return the MDE would imply           | about 311% of reserved capital                                                                                       |
| Missing hourly rows                         | 0 of the expected rows; two episodes have a 144 h grid span, 226 have 143 h                                          |

The 400 bps limit is the pre-chosen spending rule of the contract. It is not a claim of what can
be detected; the result exceeds it by a factor of 2.9, and neither multiplier of the registered
sensitivity check changes the verdict.

## What follows (all pre-registered)

- Protocol section 8 row 6: stop `insufficient_power`, **no return is read**, and no variant is
  rescued by a smaller `k`, a different block rule or a looser limit after seeing this number.
- **Stage 1b body phase (Go A, Go B): withdrawn.** The contract of
  [new-perp-stage1b-body-v1.md](new-perp-stage1b-body-v1.md) stays merged and unrun; asking for
  either go would read holdout files for a study that cannot reach a verdict.
- **No body or outcome read of the sealed backward holdout happens.** The earlier registered
  Stage 1b metadata look (catalog, listings, HEAD of first files) stays on record. The holdout
  remains one shared exam batch for any later study that is registered with a design that passes
  its own power gate.
- Discovery-ledger count: unchanged (no variant outcome was read).

## Limits and what this does not show

- **Not a statement about the idea.** The gate compares the planning MDE with a pre-chosen
  ceiling; it does not test any true effect. It rests on the transfer assumption below. The
  result is a lack of planned power under the registered criterion, not proof that no effect of
  any size can be found, and not evidence about profitability.
- **Transfer assumption.** The dispersion of days 1 to 7 stands in for the dispersion of the hold
  days 7 to 14; V2 and V3 are extrapolations, and the verdict does not depend on them.
- **Tail dominated.** Five episodes explain more than half of the squared deviations, so the
  dispersion is a property of listing outliers, not of a typical launch.
- **Overlap, as registered.** Hourly trade klines of this year were read at features level by
  earlier work; whether the pre-entry days of these 228 launches were among those reads is not
  verified. A laboratory result on this year would be a hypothesis in any case.

## Evidence in Git

[`evidence/new-perp-stage3-v1-result-2026-11-21/`](evidence/new-perp-stage3-v1-result-2026-11-21/):
the aggregate `stage3-report.json` (SHA-256
`1192e1b2a4a0d5f314423bb65a69db99028596cbba5b38436adc483e8b6aad2f`) and a manifest with the scan
result. The report holds no symbol, no per-launch value and no date. The sealed run root, its
`cohort.json`, receipts and attempts stay outside Git.
