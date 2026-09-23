# Abnormal-flow economic screen v1 - Non-Promotional Diagnostic Validation

This directory contains the results of an inconclusive diagnostic run executed on the prospective evaluation window (2026-08-30 to 2026-09-18).

**STATUS**: The v1 hypothesis does not advance to live execution. This is not a strict negative-EV FAIL, but an `INSUFFICIENT_EVIDENCE` diagnostic result on a **burned window**.

## Why this is a diagnostic result (Burned Window)

The formal runner (`#436`) was not yet merged into `main`. To generate these results, an unversioned, remote scratch-script with monkeypatches (`FORMAL_RETURNS_RUN_ENABLED = True`) was used. During execution, the pipeline suffered multiple failures, and outcomes were read repeatedly. Because the formal process requires a strict, single-pass outcome-blind execution, the window is now considered burned.

## Ledger of Attempts

1. **Attempt 1** (~00:55 UTC): Failed after 50m of loading 29 million bars due to a `TypeError` in the scratch script calling `assemble_decisions` with two positional arguments instead of kwargs. Outcomes were NOT read. Scratch code for this attempt is unrecoverable.
2. **Attempt 2** (~02:18 UTC): Failed after 1h 45m due to `TypeError: '<=' not supported between instances of 'str' and 'datetime.datetime'` when validating the execution window bounds against the string `contract.window_start_utc`. Outcomes were NOT read.
3. **Attempt 3** (~04:18 UTC): Failed after 1h 45m on the final JSON serialization step (`AttributeError: 'EconomicsReport' object has no attribute 'as_json'`). Outcomes WERE read by DuckDB, but the result object was lost in memory.
4. **Attempt 4** (~04:57 UTC): Successful execution and JSON serialization using a custom `EnhancedJSONEncoder`. Outcomes WERE read.

**Known Git Revision during attempts:** `47dca46e66c38ffcb56c3cc3283cdf4a51e420d2`

_Note: We do not claim the integrity pipeline passed entirely, as it was bypassed via monkeypatches and scratch scripts._

## Numerical Report (Diagnostic only)

The numerical report (`formal_run_report.json`) is preserved exactly as emitted by the final attempt. Key metrics:

- **mean net return**: +0.61%
- **mean excess over control**: +0.45%
- **resolved episodes**: 125 out of 252 primary episodes
- **missingness fraction**: 50.4%
- **control coverage**: 53%
- **week concentration**: 52%
- **CI lower bounds**: negative (net return lower bound -1.74%, excess lower bound -0.95%)
- **portfolio simulation**: +$4.25 / maxDD $17.15

## Artifacts

- `contract.json`: The frozen decision rules and thresholds.
- `evaluation_manifest.json`: The integrity gates.
- `formal_run_report.json`: The outcome-bearing diagnostic results.
