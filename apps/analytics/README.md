# Analytics

Python workspace package for pump scanning, point-in-time market measurements, and
strategy-agnostic forward outcomes.

Entrypoints:

- `pump-scanner` — scans supported exchanges and maintains pump episodes/snapshots.
- `outcome-resolver` — idempotently resolves forward price, MAE, and MFE windows.
- `measurement-report` — read-only dataset-health and raw-outcome report. It aggregates
  in PostgreSQL and prints Markdown by default or JSON with `--format json`.

Run against the local development database:

```bash
make measurement-report
make measurement-report ARGS="--since 2026-07-22 --exchange-horizon 240"
```

The report shows both decision count and distinct pump-episode count. Its return and
win-rate tables are descriptive: repeated decisions inside an episode are correlated.
Use the future versioned virtual-replay layer for strategy comparison, costs, exits,
confidence intervals, and champion selection.
