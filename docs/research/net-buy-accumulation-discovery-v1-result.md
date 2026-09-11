# Net-buy accumulation discovery v1 -- result

**Status: `too_rare_or_illiquid` on both primaries. Not a candidate, not a mature
negative -- the frozen thresholds fire too rarely to test the thesis. Discovery
only; no promotion, no prospective cohort earned, no live.** Contract:
[net-buy-accumulation-discovery-v1.md](net-buy-accumulation-discovery-v1.md).

## Run provenance

- Exploratory local read on 2026-09-11 (`code_revision 01dd88b`,
  `working_tree_dirty=True`, so **not a `formal_run`** -- an untracked local data
  directory dirtied the tree; the finding does not depend on formal status).
- Data: the frozen window's raw bars, still inside PostgreSQL 35-day retention,
  materialized once to local Parquet (43,703,625 rows, bybit+binance linear-USDT,
  `capture_version v1`, `bucket_start` 2026-08-10 .. 2026-09-10) and scanned with
  DuckDB. No production mutation; read-only.
- Decision window `[2026-08-18T00:00Z, 2026-09-10T20:00Z)`, baseline from
  2026-08-10, per the frozen contract.

## Verdict

| Primary           | Verdict                | Fires | Resolved | Mean adj_return (pp) | Clusters | Tradable share |
| ----------------- | ---------------------- | ----- | -------- | -------------------- | -------- | -------------- |
| P-MAG (magnitude) | `too_rare_or_illiquid` | 2     | 2        | -3.93                | 1        | 0.00           |
| P-SHAPE (breadth) | `too_rare_or_illiquid` | 0     | 0        | --                   | 0        | 0.00           |

The two P-MAG fires are one asset cluster, both in the illiquid segment; `N=2` is
not interpretable. P-SHAPE never fired.

## Why: the a-priori thresholds sit far in the tail

Scores computed cleanly; the machinery is not the problem. Over the 2,053,534
eligible minutes the `score_m` distribution is:

| p50    | p90   | p99   | max   |
| ------ | ----- | ----- | ----- |
| -0.012 | 0.072 | 0.285 | 3.082 |

`THETA_M = 1.0` sits far above the 99th percentile: only ~1053 eligible minutes
reach it, concentrated in a single asset's extreme accumulation, so the edge
trigger plus 24h reset collapses to two fires. `THETA_S = 0.60` (net buying above
the trailing norm in 60% of a day's minutes) is met by no instrument at all.

The thresholds were frozen a priori on interpretable grounds ("one normal day of
net buying", "elevated buying most of the day"), but those levels turn out to be
almost-never events. The thesis is therefore **not refuted -- it is untestable at
these thresholds** (no sample), which is exactly the `too_rare_or_illiquid` stop
the contract defined.

## Secondary: the full-presence rule is costly

The frozen "no fabricated zeros" rule requires 100% present W (1440) and B
(10080). Eligible minutes: **2.05M at 100% presence vs 28.9M at a 99% W / 95% B
tolerance** (a ~14x cut). This is principled (a missing bar is a real capture
gap, not a zero) but expensive. Even relaxed, `THETA_M = 1.0` stays in the tail,
so completeness is not the main driver of the `too_rare` verdict; but a small
tolerance is worth carrying in a successor.

## Disciplined next step

- **Do NOT lower the thresholds on this window.** Re-picking a threshold after
  seeing this window's outcome is tuning on the result and is forbidden.
- **v2 on a fresh, untouched forward window:** freeze a threshold calibrated to
  the now-known score distribution (e.g. `THETA_M` near the p99 ~0.28-0.30 so a
  testable number of fires is plausible; a correspondingly lower `THETA_S`), add a
  presence tolerance, register it, and scan a forward window that does not overlap
  2026-08-10..2026-09-10. Only that untouched read can test the thesis.
- The `2026-08-10..2026-09-10` window is now "seen" for this family and may not be
  reused for a threshold-calibrated read (it would be p-hacking).

## What this run established

The full pipeline -- frozen contract, pure verdict logic, DuckDB scanner over the
frozen bars, report -- is built, unit-tested, synthetically validated, and now
run once on real data end to end, producing an honest, non-fabricated result
before any merge or deploy. The only miss was threshold calibration, and the
distribution needed to fix it (on fresh data) is now recorded above.
