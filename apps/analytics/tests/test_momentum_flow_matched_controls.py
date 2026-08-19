from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.momentum_flow_matched_controls import (
    candidate_control_instants,
    evaluate_control_balance,
)

CAPTURE_START = datetime(2026, 8, 14, 12, 4, 47, tzinfo=UTC)


def test_candidates_prefer_nearest_calendar_distance_in_either_direction() -> None:
    trigger_at = CAPTURE_START + timedelta(days=10)
    until = trigger_at + timedelta(days=20)

    candidates = candidate_control_instants(
        trigger_at=trigger_at,
        other_trigger_instants_same_instrument=(),
        capture_epoch_started_at=CAPTURE_START,
        until=until,
    )

    # +-1 day is exactly the 24h exclusion boundary against the trigger's own
    # instant (self-exclusion -- see test_candidates_self_exclude_the_trigger_
    # instants_own_24h_neighbors below), so the nearest surviving candidates
    # are +-2 days.
    assert candidates[0].candidate_at == trigger_at - timedelta(days=2)
    assert candidates[1].candidate_at == trigger_at + timedelta(days=2)
    assert candidates[2].candidate_at == trigger_at - timedelta(days=3)
    # Exact UTC time-of-day is preserved by construction (whole-day shifts).
    assert all(candidate.candidate_at.time() == trigger_at.time() for candidate in candidates)


def test_candidates_self_exclude_the_trigger_instants_own_24h_neighbors() -> None:
    """A +-1 day shift is exactly 24h from the trigger itself -- the same
    pump this control is meant to compare against -- so it is excluded by
    construction for every event, not only when another pump happens to be
    nearby."""
    trigger_at = CAPTURE_START + timedelta(days=10)
    until = trigger_at + timedelta(days=20)

    candidates = candidate_control_instants(
        trigger_at=trigger_at,
        other_trigger_instants_same_instrument=(),
        capture_epoch_started_at=CAPTURE_START,
        until=until,
    )

    offsets = {candidate.offset_days for candidate in candidates}
    assert -1 not in offsets
    assert 1 not in offsets


def test_candidates_exclude_other_pump_within_24h_of_this_instrument() -> None:
    trigger_at = CAPTURE_START + timedelta(days=10)
    until = trigger_at + timedelta(days=20)
    # A same-instrument pump exactly 1 day before the trigger additionally
    # removes the -2 day candidate (exactly 24h from that other trigger), on
    # top of the trigger's own +-1 day self-exclusion.
    other_trigger = trigger_at - timedelta(days=1)

    candidates = candidate_control_instants(
        trigger_at=trigger_at,
        other_trigger_instants_same_instrument=(other_trigger,),
        capture_epoch_started_at=CAPTURE_START,
        until=until,
    )

    offsets = {c.offset_days for c in candidates}
    assert -1 not in offsets
    assert 1 not in offsets
    assert -2 not in offsets
    assert candidates[0].candidate_at == trigger_at + timedelta(days=2)


def test_candidates_never_reach_outside_capture_epoch_or_before_until() -> None:
    # Only 3 days of capture history exist before the trigger, and only 2
    # days of runway remain before `until` -- both bounds must be respected,
    # not just the earlier one.
    trigger_at = CAPTURE_START + timedelta(days=3)
    until = trigger_at + timedelta(days=2)

    candidates = candidate_control_instants(
        trigger_at=trigger_at,
        other_trigger_instants_same_instrument=(),
        capture_epoch_started_at=CAPTURE_START,
        until=until,
        max_search_days=28,
    )

    for candidate in candidates:
        assert candidate.candidate_at - timedelta(hours=24) >= CAPTURE_START
        assert candidate.candidate_at + timedelta(hours=4) <= until


def test_candidates_require_a_full_24h_quiet_period_before_until() -> None:
    """Regression for the second colleague review: a candidate whose own
    +4h feature window fits before `until` used to pass, even though the
    matched-control exclusion rule is a claim that no pump occurred for
    this instrument within the FOLLOWING 24h -- a claim this report's own
    dataset cannot yet verify for a period past `until`. The +4h
    feature-window check alone is not enough."""
    trigger_at = CAPTURE_START + timedelta(days=30)
    candidate_at = trigger_at + timedelta(days=2)
    # +4h feature window fits before `until`; +24h quiet period does not.
    until = candidate_at + timedelta(hours=10)
    assert candidate_at + timedelta(hours=4) <= until
    assert candidate_at + timedelta(hours=24) > until

    candidates = candidate_control_instants(
        trigger_at=trigger_at,
        other_trigger_instants_same_instrument=(),
        capture_epoch_started_at=CAPTURE_START,
        until=until,
        max_search_days=5,
    )

    assert all(candidate.candidate_at != candidate_at for candidate in candidates)


def test_candidates_reject_a_quiet_period_ending_exactly_at_until() -> None:
    """Regression for the third colleague review: a candidate whose quiet
    period ends EXACTLY at `until` used to pass (the old check only
    rejected `> until`), but the contamination data this check relies on
    (momentum_flow_event_repository.bybit_source_instants_statement) loads
    pump sources with `first_seen_at < until`, EXCLUSIVE -- a pump landing
    exactly at `until` would never appear in that loaded set, so this
    candidate's own exclusion could not actually be verified against that
    instant. `until` is an exclusive cutoff everywhere else in this report;
    this check must match that convention exactly, with no off-by-one."""
    trigger_at = CAPTURE_START + timedelta(days=30)
    candidate_at = trigger_at + timedelta(days=2)
    until_exact_boundary = candidate_at + timedelta(hours=24)  # quiet period ends EXACTLY at until

    candidates = candidate_control_instants(
        trigger_at=trigger_at,
        other_trigger_instants_same_instrument=(),
        capture_epoch_started_at=CAPTURE_START,
        until=until_exact_boundary,
        max_search_days=5,
    )

    assert all(candidate.candidate_at != candidate_at for candidate in candidates)

    # One second earlier, the quiet period genuinely ends strictly BEFORE
    # `until` and the candidate must be accepted.
    until_just_after = until_exact_boundary + timedelta(seconds=1)
    candidates_just_after = candidate_control_instants(
        trigger_at=trigger_at,
        other_trigger_instants_same_instrument=(),
        capture_epoch_started_at=CAPTURE_START,
        until=until_just_after,
        max_search_days=5,
    )
    assert any(candidate.candidate_at == candidate_at for candidate in candidates_just_after)


def test_candidates_may_be_empty_when_nothing_qualifies() -> None:
    trigger_at = CAPTURE_START + timedelta(hours=1)
    until = trigger_at + timedelta(hours=2)

    candidates = candidate_control_instants(
        trigger_at=trigger_at,
        other_trigger_instants_same_instrument=(),
        capture_epoch_started_at=CAPTURE_START,
        until=until,
    )

    assert candidates == ()


def test_candidates_reject_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        candidate_control_instants(
            trigger_at=datetime(2026, 8, 20),
            other_trigger_instants_same_instrument=(),
            capture_epoch_started_at=CAPTURE_START,
            until=CAPTURE_START + timedelta(days=30),
        )


def test_control_balance_flags_missing_reading_as_unresolved() -> None:
    balance = evaluate_control_balance(
        event_flow_notional_usd=1000.0,
        control_flow_notional_usd=None,
    )
    assert balance.balanced is False
    assert balance.reason == "missing_flow_reading"


def test_control_balance_flags_large_imbalance() -> None:
    balance = evaluate_control_balance(
        event_flow_notional_usd=1000.0,
        control_flow_notional_usd=10_000.0,
    )
    assert balance.balanced is False
    assert balance.reason == "flow_notional_imbalance"
    assert balance.flow_ratio == pytest.approx(10.0)


def test_control_balance_accepts_within_ratio() -> None:
    balance = evaluate_control_balance(
        event_flow_notional_usd=1000.0,
        control_flow_notional_usd=3000.0,
    )
    assert balance.balanced is True
    assert balance.reason is None
