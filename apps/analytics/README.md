# Analytics

Python workspace package for pump scanning, point-in-time market measurements, and
strategy-agnostic forward outcomes.

Entrypoints:

- `pump-scanner` — scans supported exchanges and maintains pump episodes/snapshots.
- `outcome-resolver` — idempotently resolves forward price, MAE, and MFE windows.
- `measurement-report` — read-only dataset-health and raw-outcome report. It aggregates
  in PostgreSQL and prints Markdown by default or JSON with `--format json`.
- `episode-replay` — read-only replay-input validator. It groups complete chronological
  decision paths by direct `pump_event_id`, fails closed on partial paths, reports
  exclusion/concentration diagnostics, and emits a code/data provenance manifest. It
  does not simulate a strategy yet.

Run against the local development database:

```bash
make measurement-report
make measurement-report ARGS="--since 2026-07-22 --exchange-horizon 240"
make episode-replay
make episode-replay ARGS="--since 2026-07-22 --horizon 240 --horizon 480"
```

The report shows both decision count and distinct pump-episode count. Its return and
win-rate tables are descriptive: repeated decisions inside an episode are correlated.
Use the future versioned virtual-replay layer for strategy comparison, costs, exits,
confidence intervals, and champion selection.

The replay command defaults to the pre-registered confirmation cohort and requires
exact anchor-venue 8-hour outcomes. `--allow-fallback` exists only for an explicitly
identified sensitivity run. The Make target records the current Git revision and
working-tree dirty state automatically.
