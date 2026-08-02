# Open-ended margin-buffer research v1

## Purpose

This contract measures whether a pump short that is not closed by the production
`max_hold` can survive the adverse path and remain economically useful at 14, 21,
and 28 days. It tests the collateral hypothesis: keep notional fixed and allocate
enough collateral to tolerate a continued pump before an eventual reversion.

This is prospective, research-only measurement. It does not modify the production
exit policy, paper positions, leverage, position size, or `DRY_RUN`.

## Frozen cohort and selection

- Contract: `prospective_no_time_exit_margin_buffer_v1`.
- Default cohort start: `2026-08-03T00:00:00Z`, after the historical pattern was
  inspected.
- Strategy scope: `pump_short_v1_market_quality`.
- Episode selection: the recorded open decision when present, otherwise the first
  chronological decision in the episode.
- Required exact-venue checkpoints: 20,160, 30,240, and 40,320 minutes.
- Cross-venue fallback is unresolved, not equivalent evidence.
- The outcome strategy scope and resolver version are not CLI-overridable.
- Right-censored checkpoints remain unavailable until their full path is mature.

The generic `forward_v1` resolver records checkpoint price, MFE, MAE, bar coverage,
and provenance. Public funding history is collected independently under
`open_ended_margin_funding_v1`. The wider funding lane deliberately has a new
resolver version: completed seven-day `long_horizon_funding_v1` rows cannot satisfy
the 28-day contract.

## No-time-exit model

"Open-ended" means there is no strategy clock exit before the report cutoff. It
does not mean infinite solvency. Each row marks the position to market at a finite
checkpoint and models signed funding over the same interval. The path can still
fail because of insufficient collateral, maintenance margin, fees, liquidation
penalties, mark-price differences, a margin-tier change, delisting, unavailable
history, or exchange failure.

The report compares observed MAE with collateral equal to 25%, 50%, 75%, 100%,
150%, and 200% of initial notional. For a recorded $50 notional, those screens
correspond to $12.50, $25, $37.50, $50, $75, and $100 of collateral. This is a
price-distance screen only. It is not an exact exchange liquidation calculation.
Actual usable distance is smaller and must later be reconstructed from the venue's
point-in-time maintenance-margin tier, mark price, fees, and margin mode.

## Required output

For every horizon, report:

- mature selected episodes, exact paths, unresolved reasons, assets, venues, and
  calendar-week concentration;
- gross short return, signed funding return, explicit taker costs, net return, and
  capital occupancy;
- MFE and MAE, baseline initial-stop survival, and collateral-buffer path survival;
- the number and share of paths crossing each collateral/notional screen;
- mean collateral dollars at the recorded notional and survivor-only net economics;
- survivor return on allocated collateral and an upper bound on concurrently occupied
  collateral;
- instrument availability and any delisting or identity failure when those fields
  become available.

Missing funding is never zero. Positive unified funding rates credit the modeled
short and negative rates debit it under
`positive_rate_long_pays_short_v1`. Public-history results are not an authenticated
funding ledger.

## Interpretation and gate

The report is descriptive discovery output. A positive 14-, 21-, or 28-day row
cannot authorize an indefinite production hold. A future executable challenger
would require a separate forward contract with an exact venue liquidation model,
fixed maximum dollar loss, portfolio-level collateral occupancy, funding and
delisting handling, an emergency exit, and a finite operational kill condition.

The first formal read requires at least 100 mature episodes, 30 asset clusters, four
UTC weeks, complete exact-venue paths, and funding coverage. Report the full cohort
and sensitivities excluding the busiest week and largest assets. Until then the
state is `collecting`.
