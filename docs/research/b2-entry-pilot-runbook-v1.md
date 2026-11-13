# B2 entry-only pilot runbook

The [contract](b2-entry-pilot-contract-v1.md) and registry must be present in
`origin/main` before a native read. The CLI checks their published content before
opening a native input; an unmerged local registration is not sufficient.

The entry runner reads only existing, hash-pinned inputs. It has no downloader,
exchange client, broker, orders, exit policy or strategy-return calculation.
Unknown native sizing remains explicit on every modeled event. Source-only
diagnostics and conditional model results are distinct artifacts.

## Verify and freeze code

Run focused tests, Ruff and strict mypy, then commit. The runner refuses a dirty
tree. Synthetic tests cover price priority, volume-limited partials, unknown/special
flow, clock ties, publication versus engine time, gaps, EOF, resets, same-ID
conflicts, post-only rejection, registration gating, real ZIP/CSV paths and receipt
relocation. Freeze this revision before seeing pilot fill statistics.

## Run after publication

```sh
git fetch origin main

python -m schurfer_analytics.bybit_presettlement_entry_pilot \
  --source /absolute/path/to/runtime/research/b2/source-pilot \
  --output runtime/research/b2/entry-pilot/result.json

python -m schurfer_analytics.bybit_presettlement_source_quality \
  --run /absolute/path/to/runtime/research/b2/source-pilot \
  --output runtime/research/b2/entry-pilot/quote-diagnostics.json
```

The second command supplies the requested source-only split of discrepancies
inside the preceding engine-clock spread versus at/beyond its old opposite quote.
Both may reflect changes between public book frames; neither is an automatic data
corruption or fill label. It uses the already frozen 12-day source-quality scope,
including the two format fixtures separately from the ten-day replay denominator.

All source files and their manifest must match their retained hashes. The native
receipt reader reconstructs the bound filename under the supplied source root,
checks identity and refuses symlink/path escape; old worktree-relative metadata
does not silently select another file.

Outputs are publish-once. Preserve failed runs and do not overwrite or resample.
No raw price, quantity, funding payment or strategy return is emitted. Record all
configuration denominators and unresolved reasons, then discuss the result before
a larger corpus or B3. Exact exit windows and a separately published reservation
amendment remain required before any masked exit tape read.
