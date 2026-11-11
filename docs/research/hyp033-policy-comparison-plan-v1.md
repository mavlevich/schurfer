# HYP-033 policy comparison implementation plan

Date: 2026-11-11. Status: **implementation plan and protocol proposal, before review and before any new data read.**

The owner selected the first route in the strategy discussion and asked to prepare
and follow its plan. The route compares a Bybit impulse strategy with the same
strategy filtered by funding and open interest. The first delivery is the research
contract, an outcome-blind feasibility check and a synthetic verification of the
calculation. An empirical read follows the design review and a frozen registration.

This is a proposed successor to [HYP-033 v1](bybit-squeeze-continuation-v1.md).
Its recorded `insufficient_data` result remains evidence about the original matched
comparison. Its preflight is now registered as
[HYP-037](hyp037-impulse-policy-preflight-v1.md). The empirical-read contract is the
next registration stage.

## Objective and authority

Deliver reproducible event-level estimates of net economics for the plain impulse
rule A and the funding/OI-filtered rule B. Either may become a discovery candidate
under the same absolute criteria, with a correction for testing two candidates.
Report the incremental event-level mean B minus A as a diagnostic. Capital and
slot constraints receive a separate replay before a candidate can be frozen for
confirmation. These stages answer different questions.

The owner's instruction authorizes preparation of this route after the
[program stop](program-review-2026-11-09.md). Registration must record this specific
exception to the stop and to the untouched-window requirement for closed lines in
[strategy v2](research-strategy-v2.md). It must describe the discovery window as
reused and features-exposed, including the known HYP-033 counts and power. The
registration does not claim that this is an independent replication of v1. A is
also related to closed impulse lines, so its inclusion as a separately selectable
candidate and that lineage must be explicit in the research exception.

The HYP-012 v2 blind boundary applies throughout data preparation and evaluation.
HYP-031 retains its reserved test year. The backward holdout retains its one-batch
confirmation rule. Production changes and live trades have their own later gates.

## First experiment

Use the event and feature definitions already implemented for HYP-033. The new
question estimates both rules on their complete eligible event sets, without a
slot limit or a requirement for another instrument to match each event.

| Item                    | Proposed contract                                                                                                                                 |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| Venue and market        | Bybit USDT-settled linear perpetuals, including instruments delisted inside the historical window                                                 |
| Full discovery boundary | 2024-10-01T00:00:00Z through 2025-09-30T23:59:59.999Z, including every lookback and exit                                                          |
| First decision          | 2024-10-16T00:00:00Z, following the v1 history requirement                                                                                        |
| Impulse                 | First hourly bar in 24 hours with return at least +2% and z1 at least 3                                                                           |
| Liquidity               | At least USD 2 million turnover in the 24 hourly bars before the impulse                                                                          |
| Decision                | One hour after the impulse closes, `d = t + 1 hour`                                                                                               |
| Baseline A              | Consider every eligible first impulse with a valid waiting bar                                                                                    |
| Filtered B              | Consider the same impulses only when the last settled funding strictly before d is negative and G is positive with own-history rank at least 0.80 |
| G and history           | Reuse v1's two-hour OI change, own 336-hour rank history and 269-defined-observation minimum                                                      |
| Entry                   | Taker, model reference is the open of the minute starting at d + 1 minute                                                                         |
| Exit                    | Four hours after the entry, at the close of the minute ending at that instant                                                                     |
| Primary cost assumption | 30 bps per round trip, comprising 11 bps fees and 19 bps spread/slippage, plus actual funding during the hold                                     |
| Cost stress             | 50 bps per round trip, plus the same funding                                                                                                      |
| Availability assumption | The OI record stamped t is available by d + 1 minute, as in v1. Historical stamps cannot prove publication time                                   |

The data adapter must make the last admissible decision derive from the exit-bar
boundary. Identity, listing and delisting rules and funding settlement semantics
come from v1. Intrahold prices needed for funding and risk reports are part of the
declared scope.

The 30/50 bps values are declared assumptions for this experiment. A positive
historical result earns a later measurement of execution costs and OI availability.
Both policies use the same cost assumptions. An order-book fill model, maker orders,
alternative exits, ML filters and additional horizons would each change the contract.

## Event sets and missingness

A includes every eligible impulse with a valid waiting bar, including impulses with
undefined F or G. B is the subset with both F and G determinably true. The same event
has the same ID and the same resolved outcome in A and B. B's skipped opportunities
are reported by reason and do not enter its conditional mean as artificial zeroes.
F/G determinability across all baseline opportunities must reach v1's 90% floor
before B is evaluable.

Freeze both event lists and intended holds from pre-entry inputs before obtaining
minute outcomes. Report counts and missingness separately for A and B. A missing
entry, exit or funding mark remains in the intended-event denominator and is never
assigned a zero return. A candidate needs at least 95% complete intended holds.
Publish complete-case net with the unresolved share and missingness by reason,
symbol and week. The portfolio replay below applies the stricter accounting rule
to unresolved accepted positions before confirmation.

Step 1 may read the approved historical features to identify decisions, conditions,
funding/OI availability, event counts, overlap and required file coverage. Pre-entry
return dispersion may be used only under its registered power scope. Entry and exit
minute prices, funding marks during the hold and post-entry paths belong to the
later empirical read. Step 1 does not fetch or print them.

## Event-level questions and inference

**Two selectable candidates.** A and B are evaluated separately. Either becomes a
discovery candidate if its mean event net is at least +30 bps and its corrected
lower bound is above zero, subject to the evidence and concentration gates below.
B need not beat A to remain a candidate. A is eligible even when B adds nothing.

Use 10,000 whole ISO-week resamples and a derived seed from base 33. Apply the same
drawn week multiplicities to the underlying A event rows and their B membership.
Within each resample recompute the two event means and their difference. This
preserves that B is a subset of A and that simultaneous instruments share weeks.
Resamples without B observations are undefined and counted, never zero-filled.
Require at least 95% of resamples to define each reported bound.

For the two absolute candidate decisions, use a Bonferroni family alpha of 0.05:
each lower bound is the 2.5th percentile, a one-sided 97.5% bound. With a central
interval helper that means `confidence_level=0.95`. Check the tail convention and
correction on a synthetic sample. The confirmation batch also counts every other
screened family and variant under strategy v2.

**Filter diagnostic.** Report mean net(B) minus mean net(A), its paired-week
uncertainty and the disjoint F/G cells. This describes conditional event returns.
It does not establish causal attribution or greater portfolio earnings. The
diagnostic's sign does not gate either candidate. Report a two-week-block
sensitivity for absolute means and the difference.

Retain v1's minimum 150 clusters, 40 symbols and 26 weeks, and top-five symbol
share at most 30%, separately for each candidate. Retain positive net in both
calendar halves and after separate removal of the five largest contributing
symbols and weeks. Report profit factor, median, monthly results, return per
dollar-hour and BTC-relative returns. A tradable BTC hedge would require its own
two-leg contract.

The old B MDE of 43.7 bps is a planning estimate under v1's uncertainty assumptions.
The two-candidate recalculation gave 49.23 bps, but still used the assumed design
effect 1.2. Clustered readiness remains pending the
[calibration amendment](hyp037-power-calibration-amendment-v1.md). A's 12,278 events
do not make it noise-free: synchronized market moves and repeated symbols can
dominate its variance. The B-minus-A variance includes their covariance, so its
power cannot be declared from B's MDE alone. Freeze the additional pre-entry
dispersion/dependence read before it runs. Diagnostic power does not become an
extra promotion gate.

With at least 150 resolved symbol-days, at least 95% outcome completeness and
valid feature determinability, negative mean net is `fail` even if symbol/week
diversity or concentration fails. Keep those failed checks in the report. Smaller
or incomplete samples retain `insufficient_data`. Positive candidates still need
all diversity and robustness checks. Positive but uncertain absolute economics
are `inconclusive`.
If both pass, both enter the frozen batch as separate candidates. If neither
passes, record both results before discussing a new variant.

## Portfolio feasibility before confirmation

The event read gives neither an executable return on account capital nor a live
sizing decision. For each surviving candidate, replay the same frozen rule under
declared capital constraints before freezing it into the confirmation batch.

Use the existing portfolio engine. Before outcome access, freeze 1x leverage,
initial normalized capital of 100, current-equity sizing, and diagnostic slot
counts of 1, 4 and 8. Report all three configurations and the outcome-blind
concurrency distribution. A selected operating configuration needs a named real
capital budget and venue minimum-order/quantity rounding. A passing configuration
cannot justify the capacity or risk of a different one. Selecting among the three
is an additional variant choice and must be counted before confirmation.

Process exits before simultaneous entries and use the engine's canonical-asset
and decision-ID tie order. Keep each policy's own slot state. Report `slot_busy`,
`insufficient_capital`, undefined features and other skips explicitly. A one-slot
run is deterministic, but its B-minus-A PnL answers the narrower question of two
constrained policies rather than the event-level filter question.

An unresolved accepted position uses `HOLD_TO_END`, retains capital and marks
accounting incomplete. Such a replay cannot support a confirmation candidate.
Include flat days, capital occupancy, maximum exposure and every rejected entry.
Before confirmation, derive daily equity and drawdown including open positions,
funding and costs on declared timestamps, and reconcile final equity with total
net PnL. The existing settled-position engine alone cannot supply mark-to-market
drawdown. This work follows the event read for a survivor and is outside the first
synthetic/preflight slice.

## Reuse and gaps identified on main 3bcfc4e

| Existing component                                                | Reuse                                                                                     | Work still needed                                                                                 |
| ----------------------------------------------------------------- | ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `apps/analytics/schurfer_analytics/bybit_squeeze_continuation.py` | Impulse detection, F/G states, event identity, pinned features and historical definitions | Export both complete event sets and pre-entry overlap counts, with no outcomes printed            |
| `apps/analytics/schurfer_analytics/portfolio_engine_v2.py`        | Entry/exit order, capital limits, sizing, cost provenance and unresolved holds            | After the event read, replay a survivor and add mark-to-market risk reporting before confirmation |
| `apps/analytics/schurfer_analytics/clustered_inference.py`        | Seeds and cluster-resampling primitives                                                   | Resample common week IDs for A, B and B-minus-A and apply the two-candidate correction            |
| `apps/analytics/tests/test_bybit_squeeze_continuation.py`         | Point-in-time feature and old-count regression                                            | Event membership, undefined inputs, scope boundaries and synthetic candidate decisions            |
| `apps/analytics/tests/test_portfolio_engine_v2.py`                | Slot, capital and unresolved-trade regressions                                            | Later, an end-to-end portfolio feasibility fixture for a survivor                                 |

## Delivery sequence

| Step                         | Deliverable                                                                                 | Gate and next action                                                                                                                                                      |
| ---------------------------- | ------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0. Plan, now                 | This document, code inventory, existing-rule reconciliation                                 | Review the specific research exception and comparison contract                                                                                                            |
| 1. Outcome-blind feasibility | Frozen A/B event IDs, per-week counts, missingness, overlap and permitted data scope        | Register an unused ID, both candidate variants, rule and registry scope before historical inputs. Recompute absolute-net planning power for the two-candidate correction  |
| 2. Synthetic verification    | Known positive, zero and negative event examples through the normal calculation entry point | Verify costs, funding signs, B membership inside A, week pairing, undefined resamples and multiplicity. A can pass when B adds nothing, and B can pass when A is negative |
| 3. Historical reader         | Window-capped minute fetch, immutable manifests and report command                          | Design reviewed, code checked, clean revision, registry scope fixed, one authorized discovery read                                                                        |
| 4. Discovery decision        | Absolute event net A/B, paired diagnostic, corrected bounds, costs and concentration        | Record each candidate's verdict, then replay survivors under registered capital and slot constraints before the confirmation freeze                                       |
| 5. Independent confirmation  | The unchanged contract on the strategy-v2 confirmation batch                                | One batch read with the recorded family/variant correction. HYP-031's reserved year remains separate                                                                      |
| 6. Forward paper             | Same strategy implementation, publication timing and quote measurements                     | Allowed forward window or separately reviewed ruling, independent confirmation passed, simulated versus observed execution reconciled                                     |
| 7. Live cost probe           | Existing execution path, journal, reconciliation, emergency close and loss budget           | Separate authorization and release review. Use actual venue order constraints and the same contract                                                                       |
| 8. Scaling                   | Registered 50/200/500-trade checkpoints                                                     | Existing joint uncertainty, shortfall and drawdown rules decide one size step                                                                                             |

The first design and event-level synthetic/preflight slice has a two-working-day
checkpoint. At it, produce a reviewable result or a specific unresolved dependency
with its cheapest next check. The later historical reader, portfolio replay and
mark-to-market adapter have separate estimates before implementation. This is a
work budget with no promised profit or live date. Historical fetching is scoped
and estimated before requests. The plan authorizes no blanket replay of other
closed families.

## First review decisions

- Confirm the successor ID, two selectable variants and the exception for reused
  discovery, including A's relationship to closed impulse lines.
- Review the absolute event-net gates, two-candidate correction, paired-week
  diagnostic and pre-entry power assumptions.
- Freeze the portfolio sensitivity scope before outcomes and require a specific
  capital-constrained implementation before confirmation.
- Confirm F/G availability is an explicit historical assumption with a live check
  required before paper, as in v1.
- Freeze both candidate rules and the confirmation path before fetching returns.

## Immediate work log

- 2026-11-11: inspected rules, existing HYP-033 protocol, scanner, portfolio engine
  and inference interfaces. Updated remote references and based this worktree on
  origin/main 3bcfc4e. Created branch `docs/hyp033-policy-plan`.
- Created this plan before new feature or outcome reads. Historical feature files,
  outcome files, production databases and exchange market-data endpoints were not
  queried for this planning step.
- Next deliverable is the reviewed registration and the synthetic/preflight code
  slice from steps 1 and 2.
- 2026-11-11, review response: changed the primary analysis to event means with
  paired week resampling, made both A and B selectable with a two-candidate
  correction, and moved portfolio allocation to a separate feasibility stage.
  Clarified the permitted step-1 data and the limits of the old power estimate.
- 2026-11-11, first code slice: added
  `apps/analytics/schurfer_analytics/bybit_squeeze_policy.py` and its synthetic
  tests. The work branch is now `feat/hyp033-policy-preflight` in the same worktree.
  `build_preflight` calls the existing scanner and reports A/B counts, F/G
  determinability, unconstrained peak concurrency and corrected planning MDE.
  `evaluate_events` accepts frozen intended IDs and in-memory outcomes, preserving
  missing events in the denominator and using paired week draws with separate
  candidate decisions. Resolved events must themselves meet the evidence floor.
- The initial slice accepted in-memory inputs and was tested on generated
  synthetic series and event populations before historical invocation. Two-week
  bootstrap sensitivity and the complete descriptive report remain part of the
  later historical-reader slice.
- 2026-11-11, registered and completed HYP-037 preflight. Added the counts-only CLI,
  machine registration, input/protocol hashes, actual Git/source fingerprints and
  sealed output binding. The [result](hyp037-impulse-policy-preflight-result-v1.md)
  records A=12,278 and B=986, exact reproduction of all old B identities, planning
  MDE 12.79/49.23 bps and peak unrestricted holds 334/17. Code passed 91 focused
  tests plus Ruff and mypy. This local run records its dirty tree explicitly.
  No trade returns were read. Next is the empirical-read contract and scoped
  minute reader, reviewed before the first outcome fetch.
- 2026-11-11, preflight review response: preserved the original source/evidence in
  local commit `b1ef6c7`; corrected negative-net verdict precedence and added
  regressions that fail on that source. The current preflight reports assumed
  power as requiring cluster calibration. A separate calibration amendment is
  proposed before any new historical dependence calculation. Original sealed
  artifacts remain the record of the first run.

- 2026-11-11, second review and calibration: fixed the per-candidate stop rule,
  added the one-hour waiting-bar dependence proxy, and registered all eight proxies
  before reading. Committed registration and tested code as `538abdc`, then ran
  from that clean revision. All proxies were estimable. The
  [calibration result](hyp037-power-calibration-result-v1.md) gives MDE 123.03 bps
  for A and 73.23 bps for B. Both stop at the registered power gate; no empirical
  trade-return reader or outcome fetch follows under this experiment. The signs
  of A/B net remain unknown. The original preflight and review corrections remain
  preserved. Later alternatives require their own explicit design decision.
