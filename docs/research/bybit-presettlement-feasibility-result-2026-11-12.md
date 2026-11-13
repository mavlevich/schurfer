# B1 local discovery read, 2026-11-12

Status: **blocked at data consistency, `superset_rebuild_needed`**, not an economic
failure and not permission to continue to B2. The report was calculated locally from
the already fetched, hash-pinned discovery snapshots. No new market request,
perpetual price or return was read. The HYP-031 test year remains untouched.

## Reproduction and code

[Evidence](evidence/bybit-presettlement-feasibility-v1/read-2026-11-12/) preserves the
original result, raw-data diagnostic, its script and the execution manifest. Report
SHA-256 is `1b3b6ecf52e2422357d0a2daa9c4164c8c36b115f96946dc8fe3c74f965ae04b`.

Code revision: `1a9f9f8dc96e8a2cb7bade545df3cc56f53516fc`, clean before the read.
It combines the interval-isolation fix of PR #587, main's non-finite-data checks and
snapshot-pinning commits `7260e55` / `3356fa5`. The overlapping consistency loops
were resolved in favour of the unique-minute interval union; both branches' tests
were retained. This is a local reviewed-code integration, not a claim that the
combined revision is merged or deployed. PR #587 had all six CI checks green when
checked; it was still open.

The exact report entry point was `bybit_presettlement_feasibility.main`, with
`report`, the three pinned snapshots, the matching revision and
`--no-working-tree-dirty`. The run took 130.63 seconds; maximum resident memory was
1,936,637,952 bytes on macOS. All snapshot checksums were verified by the command.

## Registered consistency gate blocks every interval class

| Quantity                                   |                    Pooled |          4-hour contracts |
| ------------------------------------------ | ------------------------: | ------------------------: |
| S1 scheduled-interval minute coverage      |                  99.9945% |                  99.9863% |
| Formula within 2 bps on scored settlements |                  96.5353% |                  97.0762% |
| Trigger sensitivity                        |                  99.5509% |                  99.5484% |
| Trigger specificity                        |                  99.7270% |                  99.7335% |
| Minutes compared to their hour             |                19,085,862 |                 5,284,500 |
| Minute close outside hourly low/high       |              **59.5358%** |              **61.8504%** |
| Allowed outside share                      |                      0.1% |                      0.1% |
| B1 verdict                                 | `superset_rebuild_needed` | `superset_rebuild_needed` |

1-hour, 2-hour and 8-hour classes receive the same blocked verdict. The 4-hour
formula/count subchecks do not override the consistency gate. Its three diagnostic
event lists contain 1,636 / 1,593 / 1,573 events at d30/d15/d10, but they are **not
accepted event universes**: the hourly-low superset cannot yet be trusted to include
every potential decision-time trigger. No return is attached to these lists.

## Source-snapshot diagnostic

Bybit's [premium-index endpoint documentation](https://bybit-exchange.github.io/docs/v5/market/premium-index-kline)
labels the array as start, open, high, low, close, matching the loader. That does not
resolve the discrepancy in these saved responses.

A second read of the same snapshots counted 4,373,501 hourly rows including repeated
pages. None had low greater than high, but **1,841,269 had open outside low/high**;
close was inside in every row. Of 11,362,914 minute closes outside their hour,
9,351,431 belonged to an hour whose OHLC was itself inconsistent. The diagnostic
also checked adjacent-hour alignment; neither shift explains all mismatches. Three
native examples and their timestamps are preserved in the diagnostic artifact.
These are discovery premium-index values, not trade returns.

For a bounded independent cross-check, the same saved 0GUSDT hour was located in
HYP-034's earlier premium snapshot after verifying its pinned digest
`ec05a2c39f6e9eaa5fba52396ff286b331253ad42a30678e24204c720c28e43a`.
The four hourly values match the B1 snapshot exactly and the open is outside
high/low there too. This rules out a discrepancy between these two downloads for
that one hour; it does not establish the cause or validate the rest of the data.
The comparison outputs booleans only in `independent-snapshot-check.json`.

This establishes a data-consistency problem, not its ultimate cause. It does not yet
prove an exchange defect, a different documented aggregation rule or a timestamp
bug. Reordering fields, tolerating a larger error or broadening the superset after
seeing results would require a justified change and registration before a new read.

## Next action

Trace the hourly and minute payloads through the capture/parser and compare a bounded
set of identical historical requests or an independent saved snapshot, under a
registered data-quality check. Establish a valid lower envelope or a superset that
does not rely on unverified hourly extrema before any S3 universe is accepted. Print
request and byte budgets before any additional download. B2, strategy returns and the
test-year read remain blocked until the registered B1 conditions pass.

## Verification

- All 62 tracked B1 tests passed after resolving the integration conflicts.
- Ruff check and format check passed for the two integrated Python files; the normal
  commit hooks also ran mypy successfully.
- The report was actually executed on the existing snapshots from a clean revision;
  source and evidence hashes are retained in `run.json`.
- No full `make verify`, production deployment, order or test-year read was run for
  this local report. Its next review must include the pending upstream integration.
