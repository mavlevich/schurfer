# B2 entry-pilot evidence

All native computations used clean `d6f0ece`. `main-publication.json` and
`invocation.json` establish publication of the contract by #591 before reading.
The prior local-only publication gap remains documented separately.

- `result.json`: every original-day/configuration ID and categorical conditional
  outcome. No observed price, native quantity, funding payment or strategy return.
- `quote-diagnostics.json`: all 12 frozen source-quality pairs, with the added
  preceding-engine-spread/opposite-quote split.
- `summary.json`: count-only aggregation of those sealed results and the fixed
  95% source-completeness check. Configuration counts sum to their denominators.
- `verification.json`: exact at-read code fingerprints and native core-diagnostic
  equivalence to the previous v2 source audit. The later merge retained the frozen
  implementation byte for byte.
- `archive-location.json`: the same 24 native source bodies retained centrally,
  outside disposable worktrees, without a new download. Native checksums match
  the original manifest.
- `manifest.json`: SHA-256 of the small evidence files, excluding this manifest.

The eight configurations produce 184 overlapping model instances on ten days;
the primary contains 24 decisions. These are not 184 independent trials. The
strict unknown-flow case and the all-unknown-eligible sensitivity are different
assumptions, not two independently confirmed strategies. No winner is selected.

Reproduce from the recorded clean code revision using the main-published contract
and a new output directory, as in `b2-entry-pilot-runbook-v1.md`. The central source
root is `runtime/research/b2/native-source-pilot-527b7446/` from the project root;
the portable reader checks the old receipt's bound native filename and SHA-256.
Observed execution times and metadata can differ across reproductions; compare
the categorical counts and fingerprints, not a fabricated identical wall clock.

The source-audit counters about flag completeness preserve the old diagnostic;
the entry decoder separately excludes any explicit special flag while keeping
missing BT/RPI unknown. No missing flag or native quantity constraint is filled
with a fabricated false/zero value. The tape mask remains in effect.
