"""Frozen-contract regression tests for research/gate-source-lead-forward-cohort-v1.

No tuning after start: these values are not supposed to ever change once real
data starts accumulating against them. A test failure here means someone
edited the frozen contract, which this module's own docstring says never to
do -- these assertions exist to make that change loud, not to validate
business logic.
"""

from __future__ import annotations

from datetime import UTC, datetime

from schurfer_analytics.source_lead_contract import IDENTITY_REGISTRY_V3_START
from schurfer_analytics.source_lead_forward_cohort import (
    COSTS,
    ENTRY_DELAY_MINUTES,
    EVIDENCE_FLOOR,
    OUTCOME_HORIZON_MINUTES,
    QUALIFICATION_STATUS,
    QUALIFICATION_VERSION,
    SOURCE_LEAD_FORWARD_COHORT_START,
    VERDICT_CANDIDATE,
    VERDICT_FAIL,
    VERDICT_INSUFFICIENT_DATA,
)
from schurfer_performance import DEFAULT_COSTS


def test_cohort_start_is_aliased_to_the_registry_cutover_not_copied() -> None:
    """Must be the exact same object/value as IDENTITY_REGISTRY_V3_START, not
    an independently-frozen copy that could silently drift if the registry
    cutover is ever bumped (its own docstring: forward-only, never earlier)."""
    assert SOURCE_LEAD_FORWARD_COHORT_START == IDENTITY_REGISTRY_V3_START
    assert datetime(2026, 9, 3, tzinfo=UTC) == SOURCE_LEAD_FORWARD_COHORT_START


def test_candidate_set_matches_the_live_qualification_contract() -> None:
    assert QUALIFICATION_STATUS == "qualified"
    assert QUALIFICATION_VERSION == "source_lead_qualified_capture_v3"


def test_entry_and_outcome_are_frozen() -> None:
    assert ENTRY_DELAY_MINUTES == 0
    assert OUTCOME_HORIZON_MINUTES == 30


def test_costs_reuse_the_shared_conservative_model() -> None:
    """Not a bespoke cost model -- the same one packages/performance defines
    and every other registered contract in this codebase uses."""
    assert COSTS is DEFAULT_COSTS


def test_evidence_floor_cluster_requirement_fits_the_real_candidate_universe() -> None:
    """The usual 30-cluster floor (this codebase's other registered
    contracts) is unsatisfiable here: the entire identity- and
    route-verified universe is 14 canonical assets
    (source_lead_identity_registry_v3.json). Half the universe, not 30."""
    assert EVIDENCE_FLOOR == {
        "min_resolved_episodes": 100,
        "min_distinct_asset_clusters": 7,
        "min_distinct_utc_weeks": 4,
    }
    approved_universe_size = 14
    assert EVIDENCE_FLOOR["min_distinct_asset_clusters"] <= approved_universe_size // 2 + 1


def test_verdict_states_are_distinct() -> None:
    verdicts = {VERDICT_CANDIDATE, VERDICT_FAIL, VERDICT_INSUFFICIENT_DATA}
    assert len(verdicts) == 3
