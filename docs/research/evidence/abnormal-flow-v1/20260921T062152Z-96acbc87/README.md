# Abnormal-flow outcome-blind scan — 2026-08-16 .. 2026-09-18 (identity v2)

Read-only calibration/coverage run. NO forward price, PnL, or verdict was read.
Supersedes `20260920T204847Z-876c037d` (that run had the v1 identity interval defect).

- Window 2026-08-16 .. 2026-09-18 (34 days, no gap). Runtime ~2h23m, peak RSS ~4.6 GB, 0 swaps.
- Identity = point-in-time per-route identity_key, PERSIST-UNTIL-CHANGED (export v2), so the
  partial 2026-08-18 Binance snapshots no longer delist routes.

Before/after the identity fix:

- Binance `unresolved_identity`: 4,855,223 -> 0 (missing fraction 19.06% -> 0.0%, 0% every week).
- Bybit `unresolved_identity`: 4 -> 0.
- Available decisions 34.2M -> 38.7M; eligible 18.85M -> 21.74M.
- Primary episodes 1,235 -> 1,276 (+41 Binance episodes recovered, matching the gap diagnostic).

Remaining data-quality facts (by design, not defects):

- `stale_oi` is Bybit-only (~3.92M, ~12%): concentrated in ~24 junk/stablecoin routes with
  day-old OI; the 120s Bybit freshness filters exactly these (kept).
- `below_eligibility_floor` dominates rejections (16.9M): the OI-notional floor filters hard.

IMPORTANT: fire/episode counts use PROVISIONAL thresholds (`contract` in the artifact). The
research outputs are coverage, per-venue funnel, OI age, identity coverage/snapshot age, and
distributions. `proposed_freeze` percentiles are DATA-DRIVEN but look too permissive
(buy_pressure P70 ~0.526, oi_growth P80 ~0.71%) because the distributions are squashed near
zero; thresholds are frozen in a separate outcome-blind step before any returns run.
