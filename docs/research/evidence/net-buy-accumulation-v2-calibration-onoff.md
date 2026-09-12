# net-buy accumulation v2 calibration: availability on/off (evidence)

Companion to `net-buy-accumulation-v2-calibration-onoff.json` (fingerprint
`9a7ff6d68cbb71a6a6fdd153841c77032dff9ac08818cdbfb236638531d14977`). This is a
CALIBRATION-window artifact for the v2 amendment
(`net-buy-accumulation-discovery-v2.md`), produced 2026-09-12. It is outcome-blind
(no returns read) and it does not freeze anything.

## Provenance

- **Calibration window**: `[2026-08-18T00:00Z, 2026-09-10T20:00Z)` (23.83 days),
  feature history from `2026-08-10`.
- **Data source**: a read-only streaming export of the seven needed columns
  (`exchange, symbol, bucket_start, buy/sell_total_notional_usd, trades_complete,
created_at`) for `[2026-08-10, 2026-09-10T20:00Z)` from the live PostgreSQL
  (`timeseries.bybit_momentum_bars_1m`, `market_type='linear'`,
  `capture_version='v1'`), 43,454,949 rows, on 2026-09-12. The window parquets are
  no longer in the borg `bars-*`/`research-*` archives (older ones absent); the raw
  data is still in PG and in the `db-2026-09-12` full pg_dump.
- **Code**: branch `research/net-buy-accumulation-v2-rev4-and-calibration` at
  `3c9a069`. The comparison ran as a self-contained script (only DuckDB + stdlib)
  inside the deployed analytics container over the exported parquet, so it did not
  depend on the not-yet-deployed v2 package. The v2 eligibility SQL it runs mirrors
  `net_buy_accumulation_v2_repository.py`.
- **Constants**: `B_COMPLETENESS_MIN_FRACTION=0.99`, `BASELINE_ACTIVITY_FLOOR=1e5`,
  cooldown 24h, `MAX_FINALIZATION_LAG=15s` (ON) vs unbounded (OFF).

## Finding: availability changes the fire set by ZERO

A true FIRE-LEVEL availability on/off comparison (not a row-level backfill count):
the deduplicated fire count is identical with availability ON (`lag=15s`, the
non-backfill guard) and OFF (no guard) at EVERY grid threshold, for both primaries.

| primary | thetas                           | ON == OFF dedup fires      |
| ------- | -------------------------------- | -------------------------- |
| P-MAG   | 0.10 / 0.15 / 0.20 / 0.25 / 0.30 | 372 / 196 / 120 / 74 / 50  |
| P-SHAPE | 0.10 / 0.15 / 0.20 / 0.25 / 0.35 | 712 / 416 / 226 / 126 / 39 |

Delta ON-vs-OFF = 0 at every threshold. The 365 of 35.5M untimely bars
(`created_at > bucket_end + 15s`, measured separately) do not sit at any crossing
of any firing instrument, so they change no fire. Because ON == OFF exactly, the
exact `MAX_FINALIZATION_LAG` value does not affect the fire set on this window
(the lag SLA still needs its own frozen distribution artifact regardless).

## Deterministic algorithm output (candidates, not frozen)

Same result ON and OFF: `THETA_M=0.30`, `THETA_S=0.35`, prospective window =
**97 days**, `too_slow=False`, `n_target=157.9` (100 resolved / 0.95 \* 1.5 sizing
margin), `MAX_WINDOW_DAYS=120`. Concentration 4-5%, diversity 49-417 clusters over
4 weeks. The family is COLLECTABLE.

## What this does NOT establish

Not frozen. Still open before a final freeze: the `f_ref` bias tolerance and
stability check (rule A), the `MAX_FINALIZATION_LAG` distribution + SLA (rule B),
the economically-meaningful MDE and uncertainty gate (from ECONOMICS.md / an
outcome-seen window), the entry-timing treatment (rev.6: economic entry is the
first price after `decision_at`, `close(t-1)` diagnostic only), and the colleague's
review. This artifact only establishes that (a) the family is collectable at a
~97-day window and (b) availability does not move the fires on the calibration
window. Slippage/capacity stay `capacity_unknown` pending the L2/latency shadow.
