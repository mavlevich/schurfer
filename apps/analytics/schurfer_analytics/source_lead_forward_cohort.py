"""Frozen prospective forward cohort for source-lead qualified capture v3.

research/gate-source-lead-forward-cohort-v1. Registers the untouched forward
read the 3-PR registry-activation sequence
(research/source-lead-derivative-market-evidence-v1,
research/gate-source-lead-registry-activation-v3 PR 2/PR 3) was built to
answer: for identity- and route-verified assets, does buying on the selected
target exchange the moment Gate shows a leading, qualified source lead
capture a real, after-cost edge over the following half hour -- or is the
move already priced in by the time this pipeline can act on it?

This module holds only the frozen contract -- no evaluation/report logic
exists yet, deliberately. The earliest this cohort can produce a single
resolved episode is `SOURCE_LEAD_FORWARD_COHORT_START`
(`qualify_source_lead` cannot return `status='qualified'` before then), and
the evidence floor below requires at least four more calendar weeks after
that -- there is nothing to evaluate before ~2026-10-01 at the very
earliest. Building the outcome-resolution report now, before any real
v3-qualified data exists to shape it against, would risk exactly the mistake
this codebase's other reports are built to avoid: designing resolution
mechanics against imagined data rather than real data shape. The report
lands as its own change once the floor is close to being met.

See docs/research/source-lead-forward-cohort-v1.md for the full frozen
contract, including why the evidence floor's cluster requirement is not the
usual 30 (this candidate universe is 14 assets, not hundreds).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from schurfer_performance import DEFAULT_COSTS, CostParameters

from .source_lead_contract import IDENTITY_REGISTRY_V3_START

if TYPE_CHECKING:
    from datetime import datetime

CONTRACT_VERSION = "source_lead_forward_cohort_v1"

# Frozen strictly at the qualification cutover: no v3-qualified capture can
# exist before this instant (qualify_source_lead's own early-exit check
# against IDENTITY_REGISTRY_V3_START), so there is no reason to start this
# cohort's clock any earlier -- and starting it later would mean discarding
# real qualified data while we waited. Aliased, not copied, so the two
# constants can never drift apart; if IDENTITY_REGISTRY_V3_START is ever
# bumped (its own docstring: only forward, never earlier), this cohort's
# start moves with it automatically.
SOURCE_LEAD_FORWARD_COHORT_START: datetime = IDENTITY_REGISTRY_V3_START

# Candidate set: every app.source_lead_qualifications row with this exact
# (status, qualification_version) pair whose capture's
# source_first_observed_at is at or after SOURCE_LEAD_FORWARD_COHORT_START.
# No manual asset selection -- the candidate set is exactly whatever the
# already-live, identity- and route-verified capture pipeline produces
# going forward. Matches source_lead_qualification.py's live constants
# exactly; duplicated here as literals (not imported) so a future bump of
# those constants cannot silently widen this frozen cohort's own candidate
# set without a deliberate, reviewed change to this file too.
QUALIFICATION_STATUS = "qualified"
QUALIFICATION_VERSION = "source_lead_qualified_capture_v3"

# Entry: frozen at 0m, no artificial delay. Uses the *already-captured*
# TargetObservation for the qualification result's own
# selected_target_exchange -- the exact bid/ask VWAP and impact captured at
# qualification time, on the same fixed SOURCE_LEAD_NOTIONAL_USD quote
# qualify_source_lead itself selected. No second, later fetch for entry --
# the entry this contract tests is literally the fill qualify_source_lead
# already proved was executable, priced by real order-book depth, not an
# assumed one.
ENTRY_DELAY_MINUTES = 0

# Outcome: the single primary horizon. Resolved via exact-venue OHLCV on the
# selected target exchange (ccxt), matching the "exact venue only, no proxy
# path" discipline every other registered contract in this codebase uses.
OUTCOME_HORIZON_MINUTES = 30

# Costs: this codebase's shared conservative cost model
# (packages/performance/schurfer_performance), not a new bespoke one. Entry
# slippage/impact is not modeled separately here -- it is already the real,
# order-book-derived bid/ask impact TargetObservation captured, not an
# estimate this contract would otherwise have to assume.
COSTS: CostParameters = DEFAULT_COSTS

# Evidence floor. min_resolved_episodes/min_distinct_utc_weeks match this
# codebase's other registered contracts (100 episodes, 4 UTC weeks).
# min_distinct_asset_clusters is deliberately NOT the usual 30: the entire
# identity- and route-verified universe is 14 canonical assets
# (source_lead_identity_registry_v3.json's own link count / 2), so a
# 30-cluster floor would be unsatisfiable by construction, forever. Set
# instead to half the approved universe (7), so no single asset's
# idiosyncrasy can dominate the verdict, without demanding coverage this
# candidate set can never reach.
EVIDENCE_FLOOR = {
    "min_resolved_episodes": 100,
    "min_distinct_asset_clusters": 7,
    "min_distinct_utc_weeks": 4,
}

VERDICT_CANDIDATE = "candidate"
VERDICT_FAIL = "fail"
VERDICT_INSUFFICIENT_DATA = "insufficient_data"

__all__ = [
    "CONTRACT_VERSION",
    "COSTS",
    "ENTRY_DELAY_MINUTES",
    "EVIDENCE_FLOOR",
    "OUTCOME_HORIZON_MINUTES",
    "QUALIFICATION_STATUS",
    "QUALIFICATION_VERSION",
    "SOURCE_LEAD_FORWARD_COHORT_START",
    "VERDICT_CANDIDATE",
    "VERDICT_FAIL",
    "VERDICT_INSUFFICIENT_DATA",
]
