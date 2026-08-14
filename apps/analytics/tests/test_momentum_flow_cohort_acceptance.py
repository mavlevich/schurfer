from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from schurfer_analytics.momentum_flow_cohort_acceptance import (
    COHORT_STATE_ENV_VAR,
    DEFAULT_COHORT_STATE_PATH,
    CohortAcceptance,
    CohortBoundaryConflictError,
    load_accepted_cohort,
    parse_accepted_cohort,
    resolve_capture_cohort_started_at,
    resolve_cohort_state_path,
    save_accepted_cohort,
    serialize_accepted_cohort,
)

T0 = datetime(2026, 8, 10, 19, 5, 41, 810000, tzinfo=UTC)
T1 = T0 + timedelta(days=3)


def test_first_acceptance_freezes_the_requested_value() -> None:
    resolved, acceptance, changed = resolve_capture_cohort_started_at(
        requested=T0,
        accepted=None,
        accept_new_cohort=False,
        now=T1,
    )

    assert resolved == T0
    assert acceptance == CohortAcceptance(capture_cohort_started_at=T0, accepted_at=T1)
    assert changed is True


def test_matching_request_reuses_the_accepted_value_without_change() -> None:
    """Regression for the third colleague review: the steady-state case --
    the operator re-supplies the SAME already-accepted value -- must not be
    treated as a change (no rewrite, no error)."""
    accepted = CohortAcceptance(capture_cohort_started_at=T0, accepted_at=T0)

    resolved, acceptance, changed = resolve_capture_cohort_started_at(
        requested=T0,
        accepted=accepted,
        accept_new_cohort=False,
        now=T1,
    )

    assert resolved == T0
    assert acceptance is accepted
    assert changed is False


def test_conflicting_request_without_override_raises() -> None:
    """Regression for the third colleague review: this is the exact failure
    mode a momentum-capture restart used to cause silently -- a report re-
    run after a restart getting a DIFFERENT cohort boundary and quietly
    discarding comparability with earlier data. It must now fail loud."""
    accepted = CohortAcceptance(capture_cohort_started_at=T0, accepted_at=T0)

    with pytest.raises(CohortBoundaryConflictError, match="already frozen"):
        resolve_capture_cohort_started_at(
            requested=T1,
            accepted=accepted,
            accept_new_cohort=False,
            now=T1,
        )


def test_conflicting_request_with_explicit_override_rebaselines() -> None:
    accepted = CohortAcceptance(capture_cohort_started_at=T0, accepted_at=T0)

    resolved, acceptance, changed = resolve_capture_cohort_started_at(
        requested=T1,
        accepted=accepted,
        accept_new_cohort=True,
        now=T1,
    )

    assert resolved == T1
    assert acceptance == CohortAcceptance(capture_cohort_started_at=T1, accepted_at=T1)
    assert changed is True


def test_naive_requested_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        resolve_capture_cohort_started_at(
            requested=datetime(2026, 8, 10),
            accepted=None,
            accept_new_cohort=False,
            now=T1,
        )


def test_resolve_cohort_state_path_precedence() -> None:
    # explicit > env > default
    assert resolve_cohort_state_path(
        "explicit.json", env={COHORT_STATE_ENV_VAR: "env.json"}
    ) == Path("explicit.json")
    assert resolve_cohort_state_path(None, env={COHORT_STATE_ENV_VAR: "env.json"}) == Path(
        "env.json"
    )
    assert resolve_cohort_state_path(None, env={}) == Path(DEFAULT_COHORT_STATE_PATH)


def test_serialize_and_parse_round_trip() -> None:
    acceptance = CohortAcceptance(capture_cohort_started_at=T0, accepted_at=T1)
    assert parse_accepted_cohort(serialize_accepted_cohort(acceptance)) == acceptance


def test_load_accepted_cohort_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert load_accepted_cohort(tmp_path / "does-not-exist.json") is None


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "cohort.json"
    acceptance = CohortAcceptance(capture_cohort_started_at=T0, accepted_at=T1)

    save_accepted_cohort(path, acceptance)

    assert load_accepted_cohort(path) == acceptance
