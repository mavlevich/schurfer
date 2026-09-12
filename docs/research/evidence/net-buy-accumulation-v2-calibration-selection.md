# net-buy accumulation v2: calibration selection (evidence)

Companion to `net-buy-accumulation-v2-calibration-selection.json` (fingerprint
`3f9f4caa...`). The deterministic algorithm's SELECTION for the v2 amendment,
derived on 2026-09-12 from the committed real-data fire counts.

## Provenance

- **Source**: the real-data availability-ON fire counts in
  `net-buy-accumulation-v2-calibration-onoff.json` (fingerprint `9a7ff6d6...`,
  43.45M real rows with `created_at`, calibration window
  `[2026-08-18T00:00Z, 2026-09-10T20:00Z)`, 23.83 days).
- **Derivation**: `generators/derive_calibration_selection.py`, run with
  `uv run --package schurfer-analytics python
docs/research/evidence/generators/derive_calibration_selection.py`. It uses the
  maintained package algorithm (`n_target`, `select_primary`, `decide_window`) with
  the rev.7 weekly-RATE diversity gate; no new data scan, so it needs no prod.

## Result

| primary | chosen theta | fires/week | clusters | required days |
| ------- | ------------ | ---------- | -------- | ------------- |
| P-MAG   | 0.25         | 21.7       | 71       | 50.9          |
| P-SHAPE | 0.25         | 37.0       | 117      | 29.9          |

`N_TARGET = 157.9` (100 resolved / 0.95 unresolved \* 1.5 sizing margin),
`MAX_WINDOW_DAYS = 120`, `WEEKLY_MIN_FIRES = 20`, `min_clusters = 30`.

**Prospective window = max(50.9, 29.9) rounded up = 51 days**, `too_slow = False`.
51 days is >= 4 full weeks, so the cohort will satisfy the contract's "`>= 4`
fully-covered weeks each with `>= 20` fires" floor (checked at read time). The
exploratory `0.30/0.35` are REJECTED: they run 14.7 and 11.5 fires/week, below the
`WEEKLY_MIN_FIRES = 20` floor.

## Weekly-floor note (rev.7)

The contract diversity floor (`>= 4` fully-covered UTC weeks, each `>= 20` fires) is
a property of the eventual cohort, checked at read time. It cannot be measured on a
calibration window with fewer than 4 full weeks (the available history is ~3.4
weeks), so the calibration uses the weekly fire RATE (`>= 20`/week) as the
sufficient statistic; a threshold clearing it, sized to `N_TARGET`, yields a
prospective window `>= 4` weeks by construction.

## Still open before a FROZEN contract

- Rule A bias/stability (`f=0.99` vs `f_ref=0.999`) -- needs the in-package
  comparator run in an ISOLATED environment (the prod box must not be used).
- `MAX_FINALIZATION_LAG` SLA freeze (distribution recorded; on/off is invariant to
  it here).
- Economically-meaningful MDE and the uncertainty gate (from `ECONOMICS.md` or an
  outcome-seen training window).
- The entry treatment (rev.7 forward-open) and the mandatory report metrics gate the
  eventual FORMAL read, not this outcome-blind selection.

So `0.25/0.25/51d` is the calibration SELECTION under the current candidate
constants, reproducible and real-data-backed, but not yet a frozen contract.
