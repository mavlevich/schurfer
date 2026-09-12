# net-buy accumulation v2: finalization-lag distribution / SLA (evidence)

Evidence for the `MAX_FINALIZATION_LAG` open decision (rule B) of the v2 amendment
(`net-buy-accumulation-discovery-v2.md`). Outcome-blind; freezes nothing. Produced
2026-09-12 from a LIGHT read-only aggregate on the live PostgreSQL (no heavy
scan): `timeseries.bybit_momentum_bars_1m`, `market_type='linear'`,
`capture_version='v1'`, decision window `[2026-08-18T00:00Z, 2026-09-10T20:00Z)`.

`bucket_end = bucket_start + 60s`; lag columns below are seconds past
`bucket_start`, so subtract 60 for "past bucket_end".

| venue   | n          | null | p50   | p999  | p99999 | max   | > be+15s | > be+30s | > be+60s | > 5min |
| ------- | ---------- | ---- | ----- | ----- | ------ | ----- | -------- | -------- | -------- | ------ |
| bybit   | 17,672,673 | 0    | 60.80 | 65.93 | 69.36  | 69.4  | 0        | 0        | 0        | 0      |
| binance | 17,835,987 | 0    | 62.34 | 69.25 | 77.02  | 194.3 | 365      | 3        | 3        | 0      |

("be+Xs" = later than `bucket_end + X seconds`, i.e. `bucket_start + 60 + X`.)

## Reading

- `created_at` is present on every bar (0 NULL) and tightly clustered at
  `bucket_end + 1-3s` (p50 ~~61-62s). bybit's whole distribution is inside
  `bucket_end + 10s` (max 69.4s). binance has a small tail: 365 of 17.8M bars past
  `bucket_end + 15s`, only 3 past `bucket_end + 30s`, max 194s (~~`bucket_end +
134s`).
- **Zero bars past `bucket_end + 5min` on either venue.** So there is a clear gap
  between the normal-finalization band (<= ~194s) and real backfill (minutes to
  hours, of which none occurred in this window).

## SLA implication (candidate, still to be frozen)

The freeze needs a pre-registered rule that separates "normal finalization" from
"backfill", not a hand-picked number. The data bounds it: normal tops out at
`bucket_end + ~134s`; backfill would be `>> 5min` (0 observed). Two defensible
`MAX_FINALIZATION_LAG` choices, from bucket_end:

- `~135s` (or a high percentile with margin): treats the whole observed normal band
  as timely, excludes only true backfill.
- `~15s`: tighter; excludes the 365 binance-tail bars as well.

Both give the SAME fire set on this window (the availability on/off artifact,
`net-buy-accumulation-v2-calibration-onoff`, shows delta 0 either way), so the
choice does not move the calibration here; it must still be frozen on principle
with this distribution as its justification.

## Out of scope / still open

This only characterises the lag; it does not freeze `MAX_FINALIZATION_LAG`, and it
is separate from the entry-timing treatment (rev.6: economic entry is the first
tradeable price after `decision_at`, `close(t-1)` diagnostic only). The rule A
bias/stability check (f vs f_ref) is NOT run here -- it needs an isolated
environment, never the live production box.
