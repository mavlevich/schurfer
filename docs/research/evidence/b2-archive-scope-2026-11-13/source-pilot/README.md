# B2 native source-pilot evidence

This directory retains small metadata and quality artifacts, never the native
archive bodies. The 24 exact bodies remain in the ignored local runtime directory
`runtime/research/b2/source-pilot/bodies/`, identified by the receipts' SHA-256.
There is no strategy-return or fill result in this directory.

## Provenance

- `run.json`: fetch contract, clean revision `c1c9fae`, plan/source fingerprints,
  fixed task URLs, ETags and byte limits.
- `attempts/`, `headers/`, `completed/`: all 24 attempts, frozen source versions,
  body sizes/checksums and publication receipts. No retry was needed.
- `budget.jsonl`: fsynced whole-attempt reservations, including read guards;
  1,558,183,936 bytes charged against the 2 GiB cap.
- `manifest.json`: sealed transport result. Actual native bodies total
  1,555,848,890 bytes; manifest SHA-256 is
  `527b7446887d1f6c466c9891d9f1ef67134dc4b86aaf7a30d3f83c8ddc9bb0a0`.
- `schemas.json`: an auxiliary first-record/header-only inventory from the same
  bodies. It emits sorted book field names, cts presence and native CSV column
  names, without field values. The full inspector independently checks the
  actual CSV schema and clock field over its stream.
- `quality-v1.json`: the first inspector's exact result, clean `c1c9fae`, reporting
  all 12 pairs invalid because of the strict file-day boundary assumption.
  This artifact is retained, not overwritten by a successful rerun.
- `boundary-metadata.json`: auxiliary timestamp-only first/last book records.
  For each ZIP member, read the first line and the last 1 MiB (discarding its
  partial initial line); emit only first/last system/engine timestamps relative
  to the filename's UTC day and the last message type. It establishes closing
  snapshots after the next midnight, without emitting levels or tape values.

The auxiliary schema and boundary inventories are diagnostics, not a selection
rule or an additional candidate. The second inspector version, registered on
clean `1f03acf` before rerun, counts and skips off-day book rows before converting
their levels or advancing tape alignment. It does not change the input archives.

## Reproduce the registered operations

From the research worktree, with the exact pinned body plan and runtime artifacts:

```sh
python -m schurfer_analytics.bybit_presettlement_source_fetch \
  --plan docs/research/evidence/b2-archive-scope-2026-11-13/results/pilot-body-plan.json \
  --run runtime/research/b2/source-pilot

python -m schurfer_analytics.bybit_presettlement_source_quality \
  --run runtime/research/b2/source-pilot \
  --output runtime/research/b2/source-pilot/quality-v2.json
```

The fetch must use its original clean revision to match `run.json`; replaying it
from another revision is refused. Completed native files are checksummed and
reused, not fetched again. The quality run must likewise use its recorded clean
revision. Output files are publish-once, so a reproduction needs a new output
location. Its observed time and revision are recorded explicitly.

Native trade minutes :55 through :04 remain row-count-only. Missing BT/RPI flags
remain unknown. Neither metadata completeness nor a parse can certify a native
USD 50 order, an identified maker fill or economic profitability.
