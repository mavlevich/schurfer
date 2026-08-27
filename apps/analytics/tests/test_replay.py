from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import schurfer_analytics.replay as replay_module
from schurfer_analytics.replay import (
    ReplayDecision,
    ReplayFilters,
    ReplayOutcome,
    _fingerprint,
    build_replay_dataset,
    decision_exclusion_reasons,
)


def _at(minutes: int) -> datetime:
    return datetime(2026, 7, 26, tzinfo=UTC) + timedelta(minutes=minutes)


def _filters(**kwargs: Any) -> ReplayFilters:
    values: dict[str, Any] = {
        "since": _at(0),
        "until": _at(600),
        "required_horizons": (480,),
    }
    values.update(kwargs)
    return ReplayFilters(**values)


def _outcome(*, status: str = "complete", horizon: int = 480) -> ReplayOutcome:
    return ReplayOutcome(
        horizon_minutes=horizon,
        status=status,
        anchor_exchange="binance",
        source_exchange="binance",
        entry_price=100.0,
        forward_price=90.0,
        mfe_pct=12.0,
        mae_pct=3.0,
        short_return_pct=10.0,
        coverage_ratio=1.0,
    )


def _decision(
    row_id: int,
    *,
    event_id: int | None = 1,
    minutes: int = 0,
    base: str = "ERA",
    decision_id: str | None = None,
    outcomes: tuple[ReplayOutcome, ...] | None = None,
) -> ReplayDecision:
    ts = _at(minutes)
    return ReplayDecision(
        row_id=row_id,
        decision_id=decision_id or f"00000000-0000-0000-0000-{row_id:012d}",
        pump_event_id=event_id,
        event_base=base if event_id is not None else None,
        event_first_seen_at=_at(0) if event_id is not None else None,
        event_closed_at=_at(500) if event_id is not None else None,
        ts=ts,
        base=base,
        exchange="binance",
        action="skipped",
        reason="score 5 < threshold 6",
        score=5,
        pump_pct=40.0,
        price=100.0,
        strategy_version="pump_short_v1_market_quality",
        features={
            "signal": {"computed_at": ts.timestamp() - 1},
            "config": {"score_threshold": 6},
        },
        liquidity={"status": "sampled", "quality": {"allowed": True}},
        outcomes=outcomes if outcomes is not None else (_outcome(),),
    )


def test_filters_normalize_horizons_strategies_and_status_policy() -> None:
    filters = _filters(
        strategy_versions=(" pump_short_v1 ", "pump_short_v1", ""),
        required_horizons=(480, 60, 480),
        allow_fallback=True,
    )

    assert filters.strategy_versions == ("pump_short_v1",)
    assert filters.required_horizons == (60, 480)
    assert filters.accepted_outcome_statuses == (
        "complete",
        "complete_fallback",
        "complete_fallback_unsupported",
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"until": datetime(2026, 7, 26)}, "timezone-aware"),
        ({"since": _at(10), "until": _at(10)}, "earlier"),
        ({"strategy_versions": (" ",)}, "strategy"),
        ({"required_horizons": ()}, "horizons"),
        ({"required_horizons": (0,)}, "positive"),
        ({"resolver_version": " "}, "resolver"),
    ],
)
def test_filters_reject_invalid_reproducibility_scope(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _filters(**changes)


def test_dataset_groups_decisions_in_stable_chronological_order() -> None:
    dataset = build_replay_dataset(
        [
            _decision(3, event_id=2, minutes=30, base="BANK"),
            _decision(2, event_id=1, minutes=10),
            _decision(1, event_id=1, minutes=10),
        ],
        _filters(),
    )

    assert [episode.pump_event_id for episode in dataset.episodes] == [1, 2]
    assert [decision.row_id for decision in dataset.episodes[0].decisions] == [1, 2]
    assert len(dataset.eligible_episodes) == 2
    assert dataset.episodes[0].cluster_key == "base:ERA"


def test_one_bad_decision_excludes_the_whole_episode() -> None:
    invalid = replace(_decision(2, minutes=10), features=None)
    dataset = build_replay_dataset([_decision(1), invalid], _filters())

    assert dataset.eligible_episodes == ()
    assert dataset.excluded_episodes[0].exclusion_reasons == ("missing_features",)
    assert len(dataset.excluded_episodes[0].decisions) == 2


def test_unattributed_decision_is_reported_without_synthetic_episode() -> None:
    decision = _decision(1, event_id=None)
    dataset = build_replay_dataset([decision], _filters())

    assert dataset.episodes == ()
    assert dataset.unassigned_decisions == (decision,)
    assert dict(dataset.unassigned_reasons)[1] == (
        "missing_pump_event",
        "missing_pump_event_id",
    )


def test_duplicate_decision_id_excludes_every_affected_episode() -> None:
    duplicate = "00000000-0000-0000-0000-000000000001"
    dataset = build_replay_dataset(
        [
            _decision(1, event_id=1, decision_id=duplicate),
            _decision(2, event_id=2, decision_id=duplicate, base="BANK"),
        ],
        _filters(),
    )

    assert len(dataset.excluded_episodes) == 2
    assert all(
        "duplicate_decision_id" in episode.exclusion_reasons
        for episode in dataset.excluded_episodes
    )


def test_mixed_strategy_episode_keeps_full_path_and_fails_closed() -> None:
    other_version = replace(_decision(2, minutes=10), strategy_version="pump_short_v2")
    dataset = build_replay_dataset([_decision(1), other_version], _filters())

    assert len(dataset.episodes[0].decisions) == 2
    assert dataset.episodes[0].exclusion_reasons == ("mixed_strategy_episode",)


def test_explicit_measurement_only_decision_does_not_contaminate_target_episode() -> None:
    measurement = replace(
        _decision(2, minutes=10),
        strategy_version="pump_short_measurement_v1",
        features={
            "config": {},
            "signal": {"computed_at": _at(10).timestamp()},
            "measurement_only": True,
        },
    )
    target = _decision(1)

    dataset = build_replay_dataset([target, measurement], _filters())

    assert dataset.decisions == (target,)
    assert dataset.eligible_episodes[0].decisions == (target,)


@pytest.mark.parametrize(
    "measurement",
    [
        replace(
            _decision(2, minutes=10),
            strategy_version="pump_short_measurement_v1",
            features={
                "config": {},
                "signal": {"computed_at": _at(10).timestamp()},
                "measurement_only": False,
            },
        ),
        replace(
            _decision(2, minutes=10),
            strategy_version="unexpected_measurement_v2",
            features={
                "config": {},
                "signal": {"computed_at": _at(10).timestamp()},
                "measurement_only": True,
            },
        ),
        replace(
            _decision(2, minutes=10),
            strategy_version="pump_short_measurement_v1",
            features={
                "config": {},
                "signal": {"computed_at": _at(10).timestamp()},
                "measurement_only": "true",
            },
        ),
    ],
)
def test_measurement_only_scope_exception_fails_closed(measurement: ReplayDecision) -> None:
    dataset = build_replay_dataset([_decision(1), measurement], _filters())

    assert dataset.episodes[0].exclusion_reasons == ("mixed_strategy_episode",)


@pytest.mark.parametrize(
    ("boundary", "reason"),
    [
        ("started_before_scope", "left_censored_episode"),
        ("still_open", "right_censored_episode"),
        ("closed_at_cutoff", "right_censored_episode"),
    ],
)
def test_boundary_episode_is_not_treated_as_a_complete_path(
    boundary: str,
    reason: str,
) -> None:
    decision = _decision(1)
    if boundary == "started_before_scope":
        decision = replace(decision, event_first_seen_at=_at(-1))
    elif boundary == "still_open":
        decision = replace(decision, event_closed_at=None)
    else:
        decision = replace(decision, event_closed_at=_at(600))
    dataset = build_replay_dataset([decision], _filters())

    assert reason in dataset.excluded_episodes[0].exclusion_reasons


def test_signal_after_decision_is_rejected_as_lookahead() -> None:
    decision = _decision(1)
    assert decision.features is not None
    invalid = replace(
        decision,
        features={
            **decision.features,
            "signal": {"computed_at": decision.ts.timestamp() + 10},
        },
    )

    assert "signal_after_decision" in decision_exclusion_reasons(invalid, _filters())


def test_missing_exchange_excludes_exact_anchor_replay() -> None:
    decision = replace(_decision(1), exchange="")

    assert "missing_exchange" in decision_exclusion_reasons(decision, _filters())


@pytest.mark.parametrize(
    "status",
    ("complete_fallback", "complete_fallback_unsupported"),
)
def test_fallback_outcome_requires_explicit_sensitivity_policy(status: str) -> None:
    decision = _decision(1, outcomes=(_outcome(status=status),))

    exact_dataset = build_replay_dataset([decision], _filters())
    fallback_dataset = build_replay_dataset(
        [decision],
        _filters(allow_fallback=True),
    )

    assert exact_dataset.excluded_episodes[0].exclusion_reasons == (f"outcome_status:480:{status}",)
    assert len(fallback_dataset.eligible_episodes) == 1


def test_required_horizons_fail_closed_on_missing_and_partial_outcomes() -> None:
    decision = _decision(1, outcomes=(_outcome(status="partial", horizon=60),))

    reasons = decision_exclusion_reasons(
        decision,
        _filters(required_horizons=(60, 480)),
    )

    assert reasons == ("missing_outcome:480", "outcome_status:60:partial")


def test_input_fingerprint_is_order_independent_but_content_sensitive() -> None:
    first = _decision(1)
    second = _decision(2, event_id=2, minutes=10, base="BANK")

    forward = build_replay_dataset([first, second], _filters())
    reverse = build_replay_dataset([second, first], _filters())
    changed = build_replay_dataset([replace(first, score=6), second], _filters())

    assert forward.input_fingerprint == reverse.input_fingerprint
    assert forward.input_fingerprint != changed.input_fingerprint


def test_streaming_fingerprint_matches_legacy_canonical_bytes() -> None:
    decisions = (
        replace(_decision(1), reason="измерение 🚀"),
        _decision(2, event_id=2, minutes=10, base="BANK"),
    )
    legacy_payload = []
    for decision in decisions:
        row = asdict(decision)
        row["ts"] = decision.ts.isoformat()
        legacy_payload.append(row)
    legacy_bytes = json.dumps(
        legacy_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()

    assert _fingerprint(decisions) == hashlib.sha256(legacy_bytes).hexdigest()


def test_fingerprint_serializes_only_one_decision_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serialized_row_ids: list[int] = []
    real_serialize = replay_module._canonical_fingerprint_row

    def tracked_serialize(decision: ReplayDecision) -> dict[str, Any]:
        serialized_row_ids.append(decision.row_id)
        return real_serialize(decision)

    monkeypatch.setattr(replay_module, "_canonical_fingerprint_row", tracked_serialize)

    _fingerprint(tuple(_decision(index, event_id=index) for index in range(1, 101)))

    assert serialized_row_ids == list(range(1, 101))
