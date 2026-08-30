# Source-lead forward cohort v1

## Purpose

This contract answers, on identity- and route-verified captures never touched
by any earlier analysis, whether buying on the selected target exchange the
moment Gate shows a leading source-lead capture has a real, after-cost edge
over the following half hour -- or whether the move is already priced in by
the time this pipeline can act on it.

This is a distinct question from the older `source_lead_report.py` discovery
screen (`SOURCE_LEAD_COHORT_START`, 2026-07-24): that report reads
`pump_events`/`pump_event_sources` directly, across every exchange that ever
first-saw a pump, with no identity registry and no executable-liquidity
check. This contract instead reads the live, forward-only
`source_lead_prospective_capture_v1` capture pipeline
(`source_lead_capture.py`, `source_lead_qualification.py`) -- exact Gate
identity, exact registered Binance/Bybit target identity, real captured
order-book liquidity, and (as of
research/gate-source-lead-registry-activation-v3) independently verified
derivative-market route evidence, not a provisional symbol guess. It is
prospective, research-only measurement. It does not modify `orders.py`,
`trader.py`, position size, leverage, or `DRY_RUN`/`AUTO_TRADE`.

## Why now, and why nothing is built yet

`ROUTE_EVIDENCE_INDEPENDENTLY_VERIFIED` flipped to `True` in
research/gate-source-lead-registry-activation-v3 (PR 3 of 3): `qualify_source
_lead` can, for the first time, actually return `status='qualified'`. Until
this cohort is registered, no untouched forward-only starting point exists
for a later confirmatory read -- every day this waits is a day of qualified
captures with no frozen contract governing whether they count. Registering
now, before any qualified capture exists, is what makes the read honest: the
contract is fixed before the data that will be judged by it.

No evaluation/report code exists yet, deliberately. `qualify_source_lead`
cannot return `status='qualified'` before `SOURCE_LEAD_FORWARD_COHORT_START`
(aliased to `IDENTITY_REGISTRY_V3_START`, `2026-09-03T00:00:00Z`), and the
evidence floor below needs four more calendar weeks after that -- nothing
exists to evaluate before ~2026-10-01 at the very earliest. Building the
outcome-resolution report today, against zero real qualified episodes, would
mean designing its mechanics against imagined data rather than real data
shape -- exactly the mistake this codebase's other reports are built to
avoid. The report lands as its own change once the floor is close to being
met, reading this same frozen contract.

## Frozen cohort and selection

All values live in `source_lead_forward_cohort.py`
(`CONTRACT_VERSION = "source_lead_forward_cohort_v1"`):

- **Cohort start**: `SOURCE_LEAD_FORWARD_COHORT_START`, aliased to (not
  copied from) `IDENTITY_REGISTRY_V3_START` -- `2026-09-03T00:00:00Z`. No
  v3-qualified capture can exist before this instant, so there is no earlier
  point that could meaningfully start the clock; the alias means the two can
  never silently drift apart.
- **Candidate set**: every `app.source_lead_qualifications` row with
  `status='qualified'` and `qualification_version='source_lead_qualified_
capture_v3'` whose capture's `source_first_observed_at` is at or after
  the cohort start. No manual asset selection -- exactly whatever the
  already-live capture pipeline produces going forward, governed entirely by
  the 14-asset identity registry (`source_lead_identity_registry_v3.json`)
  and its independently verified route evidence.
- **Entry**: frozen at `0m`, no artificial delay, no re-fetch. Uses the
  `TargetObservation` `qualify_source_lead` itself already selected --
  the exact bid/ask VWAP and impact captured at qualification time, on the
  fixed `SOURCE_LEAD_NOTIONAL_USD` ($50) quote. The entry this contract
  tests is literally the fill already proven executable against real
  order-book depth, not an assumed one.
- **Outcome horizon**: `+30m`, the single primary horizon. Resolved via
  exact-venue OHLCV on the selected target exchange -- no proxy path, same
  discipline as every other registered contract here.
- **Costs**: this codebase's shared conservative model
  (`packages/performance/schurfer_performance.DEFAULT_COSTS` --
  `taker_fee_bps_per_side=10.0`, `funding_cost_bps_per_8h=5.0`), not a
  bespoke one. Entry slippage is not modeled separately -- it is the real
  captured impact, not an estimate.
- **Evidence floor** (`EVIDENCE_FLOOR`): 100 resolved episodes, 7 distinct
  asset clusters, 4 distinct UTC weeks. `min_distinct_utc_weeks`/
  `min_resolved_episodes` match this codebase's other registered contracts.
  `min_distinct_asset_clusters` is deliberately **not** the usual 30: the
  entire identity- and route-verified candidate universe is 14 canonical
  assets, so a 30-cluster floor would be unsatisfiable forever. 7 is half
  the approved universe -- enough that no single asset's idiosyncrasy can
  dominate the verdict, without demanding coverage this candidate set can
  never reach.

## Verdict rule

Once the evidence floor above is met (not before -- an early peek that fails
the floor is `insufficient_data`, not a result), the not-yet-built report
must compute a 95% cluster-bootstrap CI on after-cost net return per episode,
matching this codebase's existing convention (see e.g. the liquid-taker
family's own verdict rule).

- **`candidate`** only if the evidence floor is met AND the CI's lower bound
  is positive (excludes zero).
- **`fail`** if the floor is met and the CI crosses zero, or is negative.
- **`insufficient_data`** if the floor is not yet met.

A `candidate` verdict on this contract authorizes building a real paper
execution/accounting layer for source-lead (none exists yet) -- it does not
by itself authorize live trading.

## Required output (once the report exists)

Coverage funnel (captured / qualified / resolved / unresolved and why),
per-asset and per-week concentration, the primary net-return sensitivity
with its cluster-bootstrap CI, and per-episode results -- same shape as this
codebase's other registered forward contracts, reading
`source_lead_forward_cohort.py`'s frozen constants directly rather than
redefining any of them.
