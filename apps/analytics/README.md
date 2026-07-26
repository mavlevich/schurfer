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
  exclusion/concentration diagnostics, and emits a code/data provenance manifest.
- `virtual-strategy-report` — replays the recorded pump-short v1 decision once per
  eligible episode over exact-anchor 5-minute OHLCV. It applies the production exit
  bands, conservative within-bar ordering, fixed fees/funding reserve, and the
  decision-time bid/ask impact snapshot, then emits Markdown or JSON.

Run against the local development database:

```bash
make measurement-report
make measurement-report ARGS="--since 2026-07-22 --exchange-horizon 240"
make episode-replay
make episode-replay ARGS="--since 2026-07-22 --horizon 240 --horizon 480"
make virtual-strategy-report
make virtual-strategy-report ARGS="--since 2026-07-26 --format json"
```

The report shows both decision count and distinct pump-episode count. Its return and
win-rate tables are descriptive: repeated decisions inside an episode are correlated.
The virtual report is descriptive. It does not run cluster-bootstrap confidence
intervals, multiple-comparison correction, challenger selection, or a go/no-go verdict.

The replay command defaults to the pre-registered confirmation cohort and requires
exact anchor-venue 8-hour outcomes. `--allow-fallback` exists only for an explicitly
identified sensitivity run. The Make target records the current Git revision and
working-tree dirty state automatically.

Virtual entry is the next complete 5-minute bar open after a decision. This avoids
using a candle that was still forming at decision time. The report fingerprints the
downloaded path and retains it in JSON output, fails closed on missing bars or missing
liquidity-cost inputs, and assumes the adverse stop fires first when a 5-minute bar
cannot establish event order.
The default 10 bps taker fee per side and 5 bps per 8-hour funding cost are explicit
conservative model inputs and can be overridden for a sensitivity run.
