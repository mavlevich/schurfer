# Abnormal-flow economic screen v1 - Non-Promotional Diagnostic Validation

This directory contains the results of an inconclusive diagnostic run executed on the prospective evaluation window (2026-08-30 to 2026-09-18).

**STATUS**: The v1 hypothesis does not advance to live execution. This is not a strict negative-EV FAIL, but an `INSUFFICIENT_EVIDENCE` diagnostic result on a **burned window**.

## Why this is a diagnostic result (Burned Window)

The formal runner (PR #436) was not yet merged into `main`. To generate these results, an unversioned, remote scratch-script with monkeypatches (`FORMAL_RETURNS_RUN_ENABLED = True`) was used. During execution, the pipeline suffered multiple failures, and outcomes were read repeatedly. Because the formal process requires a strict, single-pass outcome-blind execution, the window is now considered burned and the results diagnostic.

## Ledger of Attempts

| Attempt | UTC Date/Time        | Revision   | Command/Script                                              | Failure Stage                       | Outcomes Read | Artifact Created |
| :------ | :------------------- | :--------- | :---------------------------------------------------------- | :---------------------------------- | :------------ | :--------------- |
| 1       | 2026-09-22T20:55:09Z | `19430c04` | `uv run python /tmp/run_formal.py` (unknown/unrecoverable)  | Assembly `TypeError`                | No            | No               |
| 2       | 2026-09-22T23:29:16Z | `47dca46e` | `uv run python /tmp/run_formal4.py` (unknown/unrecoverable) | Window bounds `TypeError`           | No            | No               |
| 3       | 2026-09-23T04:18:05Z | `47dca46e` | `uv run python /tmp/run_formal5.py` (unknown/unrecoverable) | JSON serialization `AttributeError` | Yes           | No               |
| 4       | 2026-09-23T04:57:05Z | `47dca46e` | `uv run python /tmp/run_formal6.py` (unknown/unrecoverable) | Success                             | Yes           | Yes              |

_Note: We do not claim the integrity pipeline passed entirely, as it was bypassed via monkeypatches._

## Artifacts and Provenance

The input configuration is correctly frozen in the sibling directory `../formal/`.

- Frozen Contract (`../formal/contract.json`): SHA-256 `36502eeb4ffb63cb2d0eead5e81c97947c97130a85ac046e6d2759459e492256`
- Evaluation Manifest (`../formal/evaluation_manifest.json`): SHA-256 `7f4d3f58044d84898b40c2c5bcedc90a6173340a93f3d6e07e09c3e5cf6f2b8d`
- Diagnostic Report (`formal_run_report.json`): SHA-256 `08be4b11642eb540975f60c22ad9cc4f8962529feaaa1dda4d67a5c0b561ae2e`

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
