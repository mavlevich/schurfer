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
  A mature 14-day path remains eligible for the interim capital gate while its
  21- and 28-day rows stay explicitly unresolved; later censoring does not erase
  already observable earlier-horizon evidence.

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

The report is a background boundary test, not an active promotion lane. It does not
consume one of the two active hypothesis-review slots while it is only waiting for
maturity; `liquid_taker_candidate_v1` retains priority for the Phase 3 decision.
A positive 14-, 21-, or 28-day row cannot authorize an indefinite production hold.
The only actionable use of a positive result is to calibrate a separately registered,
bounded, fixed-dollar-risk exit challenger such as the 1.5x wider-stop lane. It cannot
promote an open-ended hold, increase leverage, or remove the emergency exit.

The capital-efficiency no-go is frozen before the cohort starts:

- interim checkpoint: at least 30 exact 14-day paths, 10 asset clusters, and two UTC
  weeks;
- final checkpoint: at least 100 exact 28-day paths, 30 asset clusters, and four UTC
  weeks;
- at either ready checkpoint, if fewer than 80% of paths survive a 100%
  collateral/notional price-distance screen, the state is
  `no_go_capital_efficiency`, regardless of survivor-only net return.

The interim rule is logically conservative: path MAE cannot shrink when the window
extends from 14 to 28 days, so a failed 14-day survival floor cannot recover at 28
days. The 100% screen is already generous because maintenance margin, fees, mark
price, and liquidation penalties reduce the usable distance. A higher conditional
return among survivors cannot compensate for paths that the modeled capital budget
could not keep alive.

If the final survival floor passes, the state is only `boundary_only_ready`. Before
any positive economic interpretation, the final report must add a separately
versioned, point-in-time macro-regime sensitivity covering at least BTC-dominance
change and an aggregate funding index with a frozen historical constituent rule.
Unknown regime inputs remain explicit. Present-day constituents or present-day
metadata must not be projected backward. This requirement cannot postpone or reverse
the monotonic capital-efficiency no-go.

A future executable bounded challenger would still require a separate forward
contract with an exact venue liquidation model, fixed maximum dollar loss,
portfolio-level collateral occupancy, funding and delisting handling, an emergency
exit, and a finite operational kill condition.

The first formal economic read requires at least 100 mature episodes, 30 asset
clusters, four UTC weeks, complete exact-venue paths, funding coverage, and the macro
regime sensitivity above. Report the full cohort and sensitivities excluding the
busiest week and largest assets. Until then the state is `collecting`, unless the
pre-registered capital-efficiency no-go has already fired.
