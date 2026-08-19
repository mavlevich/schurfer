# Bidirectional buy/sell volume-burst discovery study v1

## What this is

A rebuild of the 2026-08-17 volume-burst screen
(`docs/analysis/momentum_flow_volume_burst_screen.sql`) after a colleague
review found real methodological holes in that first pass. Discovery-level
only: running it, or reading its output, does not authorize any change to
the live WATCH/paper contract (`momentum_flow_watch_contract.py`) or any
other frozen threshold.

Code: `apps/analytics/schurfer_analytics/momentum_flow_bidirectional_burst_study.py`
(pure logic), `..._repository.py` (Postgres adapter), `..._report.py`
(CLI). Tests: `apps/analytics/tests/test_momentum_flow_bidirectional_burst_study.py`
(11 unit tests) and `..._repository_integration.py` (4 real-Postgres tests).

## What the first pass got wrong, and the fix here

- **ROWS BETWEEN N PRECEDING / LEAD(N) do not equal N actual minutes** once
  incomplete bars are filtered out by `complete = true`. Verified against
  the real Bybit dataset the same night: 0.37% of rows (2,720 of 726,145)
  immediately follow a gap of more than 1 minute, the largest gap 1h57m.
  Fixed here with Postgres `RANGE BETWEEN INTERVAL '5 minutes' PRECEDING`
  window frames (keyed on the real `bucket_start` timestamp, not row
  position) for the burst percentage, and exact-timestamp equality lookups
  (never `LEAD`/`LAG`) for every precursor/horizon price. A real-Postgres
  regression test (`test_fetch_candidate_extreme_minutes_uses_a_real_5_minute_range_not_5_rows`)
  seeds an explicit gap and asserts the 5-minute window's numerator only
  ever includes bars from the real 5-minute span.
- **"13 independent episodes" only declustered by symbol, not by time** --
  a single multi-minute burst run for one symbol counted as N "independent"
  observations. Fixed with `decluster_episodes`: a real refractory-window
  segmentation where a new episode starts only once `refractory_minutes`
  has passed since the last extreme-burst minute for that symbol.
- **Selection bias** (only symbols that had already pumped) and **no fixed
  dataset window** (`now() - 7 days`, a moving target). Fixed by scanning
  the full captured universe over an explicit, pinned `[since, until)`
  range.
- **Buy-only.** Fixed by tracking buy and sell bursts as two distinct,
  separately-declustered populations (buy burst -> candidate long entry,
  sell burst -> candidate short entry, per the observation that a sell
  burst is informative on its own, not just the absence of a buy signal).
- **No matched control, no after-cost economics, no real cluster-bootstrap
  inference.** Fixed by using each symbol's own mean forward return across
  every bar in the study window as the baseline (same asset, not a
  cherry-picked comparison), reusing `schurfer_performance.accounting.
calculate_performance` for after-cost economics (the same engine every
  other paper/replay path in this repo uses), and reusing
  `challenger_inference.build_challenger_inference` for statistical
  inference (the same cluster-bootstrap + Holm-correction engine the
  pump-short reports use) instead of a hand-rolled mean/CI.

## Known, disclosed limitation this pass does NOT fix

No real bid/ask slippage/impact model exists for these synthetic entries
(unlike the pump-short paper contracts, which fetch a real order-book
VWAP). Net economics are fees + funding only (entry/exit slippage passed
as `0.0` bps explicitly, not omitted silently) -- labeled `costs_partial`
in the report, not presented as a full paper-trade cost model.

## Running it

```
make bidirectional-burst-study-report ARGS="--since 2026-08-10T00:00:00Z --until 2026-08-18T00:00:00Z"
make prod-bidirectional-burst-study-report ARGS="--since 2026-08-10T00:00:00Z --until 2026-08-18T00:00:00Z"
```

`--extreme-threshold-pct` (default 10.0), `--refractory-minutes` (default
60), and `--min-volume-24h-usd` (default 50,000) are this report's own
provisional scan parameters, tunable per run -- not a frozen contract.
`--exchange` defaults to `bybit`; pass `--exchange binance` to replicate
once Binance has enough post-remediation history (see
`binance-watch-input-coverage-v1.md`).
