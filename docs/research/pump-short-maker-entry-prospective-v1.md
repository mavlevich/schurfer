# Pump short maker-entry prospective confirmation v1

## Purpose

This contract answers, on decisions never inspected by the existing discovery
report, whether a passive (post-only) entry for pump_short's short leg has a
real net edge over the current taker entry -- not just an optimistic upper
bound over already-seen candles.

The existing discovery cohort (`MAKER_ENTRY_COHORT_START`, `2026-07-22`,
`maker_entry_validation_report_v2`) is descriptive-only and stays exactly as
it is; this is a separate, later cohort read by the same, unmodified report
code (`maker_entry_report.py`), never a re-tuned or re-selected variant of
it. This is prospective, research-only measurement. It does not modify
`orders.py`, `trader.py`, position size, leverage, or `DRY_RUN`/`AUTO_TRADE`.

## Why a second cohort, not the existing one

The discovery cohort's own conservative sensitivity
(`activation_marketable_as_cash`) read +0.22% mean episode net / +0.25% vs
the matched 1m-taker control as of 2026-08-24, but with a 95% cluster CI of
[-0.21%, 0.69%] -- not distinguishable from zero. Splitting that same window
in half chronologically (2026-07-22 to 2026-08-10 vs 2026-08-10 to
2026-08-24) shows the positive lean concentrated almost entirely in the
second half (implied mean net ~+0.63% vs ~-0.15% in the first half) --
temporally unstable within the very window it was discovered in. A lean that
appears only in the most recently inspected weeks of a discovery cohort is
exactly the pattern p-hacking produces; this contract exists to find out
whether it persists on data the discovery read never touched at all, not
just a later slice of the same window.

## Frozen cohort and selection

- Contract: `prospective_confirmation_v1` (`MAKER_ENTRY_PROSPECTIVE_COHORT_START`
  in `maker_entry_report.py`).
- Cohort start: `2026-08-24T11:10:00Z` -- frozen strictly after the discovery
  report's own last-inspected decision timestamp (`2026-08-24T10:21:44Z`) so
  there is zero overlap with anything already looked at.
- Strategy scope: `pump_short_v1_market_quality` (unchanged from discovery).
- Entry model, fill evidence, cost model, exit model, market path: unchanged
  from the discovery cohort (`MAKER_ENTRY_MODEL_VERSION`,
  `MAKER_FILL_EVIDENCE_VERSION`, `MAKER_COST_MODEL_VERSION`,
  `MAKER_SAME_RESOLUTION_TAKER_VERSION`) -- this contract tests the same rule
  on new data, not a new rule.
- Primary sensitivity, fixed before seeing any prospective data:
  `activation_marketable_as_cash`. Chosen because a resting post-only order
  would very likely be rejected by the exchange if it was already marketable
  on the activation bar, so treating that fill as real is the more
  optimistic, less defensible of the three variants the report already
  computes. The other two (`optimistic_all_potential_fills`,
  `activation_marketable_and_touches_as_cash`) remain descriptive context
  only, never the confirmatory read.
- Evidence floor: 100 fillable episodes, 30 distinct asset clusters, 4
  distinct UTC weeks (`MAKER_ENTRY_PROSPECTIVE_EVIDENCE_FLOOR`) -- the same
  numbers used throughout this codebase's other registered contracts.

## Verdict rule

Run `maker-entry-report --since 2026-08-24T11:10:00Z --strategy-version
pump_short_v1_market_quality`, unmodified, once the evidence floor above is
met (not before -- an early peek that fails the floor is
`insufficient_data`, not a result).

- **Confirmed** only if the evidence floor is met AND the primary
  sensitivity's 95% cluster-bootstrap CI does not cross zero on its lower
  bound.
- **Not confirmed** if the floor is met and the CI crosses zero, or excludes
  zero on the negative side.
- **insufficient_data** if the floor is not yet met.

A `Confirmed` result on this contract still only authorizes registering
maker-entry as a real shadow contract with actual order-placement logic
(none exists yet) -- it does not by itself authorize live trading, per the
report's own standing disclaimer.

## Required output

Same as the existing discovery report's own sections (coverage,
timeframe-separated economics, fill-evidence diagnostics, fixed fill
sensitivities, per-episode results) -- no new reporting code, just a second
registered `--since` value the same code accepts. See `Cohort:` in the
rendered report to confirm which cohort (`discovery_upper_bound_only` vs
`prospective_confirmation_v1`) a given run actually read.
