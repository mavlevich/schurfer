# SUPERSEDED — do not use for calibration

This run used identity export v1 (full-universe-replacement interval semantics), which
was DEFECTIVE: partial Binance capture-warmup snapshots on 2026-08-18 (6/50/150 vs ~525
instruments) wrongly delisted ~375 liquid Binance routes for 08-18..08-27, producing a
spurious ~19% Binance unresolved_identity (weeks W34/W35 only). Fixed by persist-until-
changed semantics (export v2); superseded by the re-run. Kept for the before/after
comparison only.

---

# Abnormal-flow outcome-blind scan — 2026-08-16 .. 2026-09-18

Read-only calibration/coverage run. NO forward price, PnL, or verdict was read.

- Window: 2026-08-16 .. 2026-09-18 (34 days, no gap; chosen_end = 2026-09-18).
- Inputs staged from prod Borg to a local Mac, per-day SHA verified; identity is the
  real point-in-time per-route `identity_key` (read-only prod export, 2,798 records).
- Runtime: ~2h23m, peak RSS ~5.0 GB, 0 swaps (single Mac, streamed).

IMPORTANT: the fire/episode counts in `scan.json` use PROVISIONAL thresholds
(`contract` in the artifact), not a registered freeze. The research-relevant outputs
here are the coverage, per-venue rejection funnel, OI age, identity coverage / snapshot
age, and feature distributions. Thresholds are frozen in a separate outcome-blind step
before any returns run. `proposed_freeze` is a data-driven proposal for review only.

Files: `manifest.json` (per-day archive/hash/fidelity + identity snapshot sha),
`scan.json` (window, per-venue funnel, oi_age, identity coverage, distributions,
counts, proposed_freeze). Raw Parquet is not stored in git.
