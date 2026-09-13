# net-buy accumulation v2: executability / capacity curve

Companion to `net-buy-accumulation-v2-liquidity-floor.json` (fingerprint
`dbd8f2c9aba9...`, source parquet sha256 `cbb72f01edcf...`). The CHEAP, OUTCOME-BLIND
half of the L2/latency shadow: from traded notional we already capture, how much can
we actually deliver into each fire, and how many fires survive as economically worth a
slot. No return is read, so nothing here is a verdict on edge.

Supersedes the first cut of this artifact, which had two errors (both corrected here,
per methodology review): it sized on the fire minute's own volume (look-ahead) and it
stated an edge/L2 conclusion without reading returns.

## Method (frozen in `net-buy-accumulation-v2-sizing-preregistration.md`)

- POINT-IN-TIME flow: `conservative_trailing_flow = min(p25 trailing-15m, p25
trailing-60m)` of per-minute traded notional, over minutes STRICTLY BEFORE the fire
  minute. The fire minute's own notional is a diagnostic only (not known at
  `decision_at`).
- DYNAMIC sizing: `deliverable = min(target, 0.10 * conservative_trailing_flow)`; a
  fire is tradeable when `deliverable >= 50 USD` (candidate min economic notional).
- Capacity curve over target notionals 50 / 100 / 300 / 1500 USD.

## Provenance

- Source `cold-bars-window/bars-window.parquet` (sha256 `cbb72f01edcf...`, 43.7M rows,
  both venues), the same window that fed the calibration. Availability-OFF (subset has
  no `created_at`; on/off parity is delta 0).
- Generator `generators/v2_liquidity_floor.py`; pure sizing math is the unit-tested
  package `net_buy_accumulation_v2_liquidity`. Git rev and dirty state are recorded in
  the JSON.

## Result (theta = 0.25, cap = 0.10, min economic = 50 USD)

| primary | fires | tradeable | zero-flow | fires/week | deliverable @300 (median / p25) | deliverable @1500 (median) |
| ------- | ----- | --------- | --------- | ---------- | ------------------------------- | -------------------------- |
| P-MAG   | 74    | 15 (20%)  | 22 (30%)  | 4.4        | 300 / 158                       | 926                        |
| P-SHAPE | 126   | 80 (63%)  | 2         | 23.5       | 300 / 141                       | 484                        |

(fires/week over the 23.83-day window; the 20/week diversity-rate proxy is the floor.)

## Reading

Point-in-time sizing splits the two primaries sharply:

- **P-SHAPE is meaningfully tradeable.** 63% of fires can take at least the minimum
  size, only 2 of 126 have no trailing flow, and at a 300 USD target the median fire
  delivers the full 300 (p25 still 141). At 23.5 tradeable fires/week it stays above
  the 20/week diversity-rate proxy. At the full 1500 USD target most fires are
  flow-capped (median deliverable 484), i.e. the size the flow bears is nearer a few
  hundred dollars than 1500.
- **P-MAG is thin.** Only 20% of fires are tradeable and 30% have zero p25 trailing
  flow (a quarter or more of their trailing minutes simply do not trade). At 4.4
  tradeable fires/week it falls below the diversity-rate proxy after executability.

So the corrected, point-in-time read is NOT "the edge is dead". It is: at 300 USD-ish
size, P-SHAPE has a tradeable, diversity-clearing fire set; P-MAG largely does not.
The 1500 USD full-leverage target is unrealistic for most fires on both primaries.

## What this does and does not establish

- It bounds executability from one side only: high participation is decisively bad,
  but the trailing traded flow is realized volume, not resting order-book depth, so a
  deliverable that clears the cap does NOT prove the fill is good. The live L2 depth +
  latency shadow remains the authority.
- No return is read here. Whether the surviving (mostly P-SHAPE, ~300 USD) fire set
  carries positive after-cost EV against the 22 bps round-trip bar is the GATED returns
  pass defined in the pre-registration, run only after methodology approval and the
  report-metrics gate.
