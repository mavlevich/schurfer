from __future__ import annotations

import pytest
from schurfer_analytics.entry_challenger_inference import (
    ENTRY_INFERENCE_VERSION,
    InferenceEpisode,
    InferenceSettings,
    build_entry_challenger_inference,
)

VARIANTS = ("red", "retrace", "combined")
TEST_SETTINGS = InferenceSettings(iterations=500, seed=42)


def _episodes(
    count: int,
    clusters: int,
    *,
    baseline: float | None = 1,
    challengers: tuple[float | None, ...] = (2, 3, 4),
) -> tuple[InferenceEpisode, ...]:
    return tuple(
        InferenceEpisode(
            pump_event_id=index + 1,
            cluster_key=f"base:TOKEN{index % clusters}",
            baseline_return_pct=baseline,
            challenger_returns_pct=tuple(zip(VARIANTS, challengers, strict=True)),
        )
        for index in range(count)
    )


def test_formal_sample_is_locked_to_first_100_episodes() -> None:
    initial = _episodes(100, 40)
    later = tuple(
        InferenceEpisode(
            pump_event_id=101 + index,
            cluster_key=f"base:LATE{index}",
            baseline_return_pct=-100,
            challenger_returns_pct=tuple(zip(VARIANTS, (-100, -100, -100), strict=True)),
        )
        for index in range(20)
    )

    report = build_entry_challenger_inference(
        (*initial, *later),
        VARIANTS,
        settings=TEST_SETTINGS,
    )

    assert report.readiness.status == "formal_sample_ready"
    assert report.inference_version == ENTRY_INFERENCE_VERSION
    assert report.formal_sample_event_ids == tuple(range(1, 101))
    assert report.baseline is not None
    assert report.baseline.estimate.point_estimate == 1
    assert all(row.verdict == "shadow_candidate" for row in report.challengers)
    assert all(
        row.paired.familywise_confidence_level == pytest.approx(1 - 0.05 / 3)
        for row in report.challengers
    )
    for row in report.challengers:
        assert row.paired.raw_p_value <= row.paired.holm_adjusted_p_value
    assert all(row.paired.holm_rejected for row in report.challengers)


def _episodes_with_triggers(
    count: int,
    clusters: int,
    *,
    triggered_challenger_count: int,
) -> tuple[InferenceEpisode, ...]:
    """Baseline always triggers; only the first triggered_challenger_count episodes
    have a real 'rare' challenger trade — the rest are zero_return_cash_episode."""
    episodes = []
    for index in range(count):
        rare_triggered = index < triggered_challenger_count
        episodes.append(
            InferenceEpisode(
                pump_event_id=index + 1,
                cluster_key=f"base:TOKEN{index % clusters}",
                baseline_return_pct=1,
                baseline_triggered=True,
                challenger_returns_pct=(("common", 2), ("rare", 3 if rare_triggered else 0)),
                challenger_triggered=(("common", True), ("rare", rare_triggered)),
            )
        )
    return tuple(episodes)


def test_low_trigger_rate_challenger_is_not_formal_sample_ready() -> None:
    """Regression (2026-08-04 entry-floor finding): a rare challenger can reach every
    other formal-sample gate almost entirely on zero_return_cash_episode rows while
    the informative, actually-triggered sample stays tiny. minimum_triggered_episodes
    must catch this instead of reporting formal_sample_ready."""
    report = build_entry_challenger_inference(
        _episodes_with_triggers(100, 40, triggered_challenger_count=3),
        ("common", "rare"),
        settings=TEST_SETTINGS,
        minimum_triggered_episodes=20,
    )

    assert report.readiness.status == "insufficient_triggers"
    assert report.readiness.least_triggered_variant == "rare"
    assert report.readiness.least_triggered_count == 3
    assert report.baseline is None
    assert report.challengers == ()


def test_trigger_gate_passes_once_every_strategy_clears_the_floor() -> None:
    report = build_entry_challenger_inference(
        _episodes_with_triggers(100, 40, triggered_challenger_count=25),
        ("common", "rare"),
        settings=TEST_SETTINGS,
        minimum_triggered_episodes=20,
    )

    assert report.readiness.status == "formal_sample_ready"
    assert report.readiness.least_triggered_variant == "rare"
    assert report.readiness.least_triggered_count == 25
    assert report.baseline is not None


def test_trigger_gate_is_off_by_default() -> None:
    """Existing callers that never populate challenger_triggered must see
    identical behavior to before this gate existed."""
    report = build_entry_challenger_inference(
        _episodes(100, 40),
        VARIANTS,
        settings=TEST_SETTINGS,
    )

    assert report.readiness.status == "formal_sample_ready"
    assert report.readiness.minimum_triggered_episodes is None
    assert report.readiness.least_triggered_variant is None
    assert report.readiness.least_triggered_count is None


def test_trigger_gate_requires_trigger_data_on_every_formal_episode() -> None:
    episodes = _episodes(100, 40)  # challenger_triggered left at its empty default

    with pytest.raises(ValueError, match="challenger_triggered"):
        build_entry_challenger_inference(
            episodes,
            VARIANTS,
            settings=TEST_SETTINGS,
            minimum_triggered_episodes=20,
        )


def test_challenger_triggered_keys_must_match_returns_keys() -> None:
    with pytest.raises(ValueError, match="challenger_triggered"):
        InferenceEpisode(
            pump_event_id=1,
            cluster_key="base:ERA",
            baseline_return_pct=1,
            challenger_returns_pct=(("red", 2), ("retrace", 3), ("combined", 4)),
            challenger_triggered=(("red", True),),
        )


def test_formal_inference_requires_cluster_diversity() -> None:
    report = build_entry_challenger_inference(
        _episodes(100, 10),
        VARIANTS,
        settings=TEST_SETTINGS,
    )

    assert report.readiness.status == "insufficient_diversity"
    assert report.baseline is None
    assert report.challengers == ()


def test_directional_sample_does_not_emit_formal_intervals() -> None:
    report = build_entry_challenger_inference(
        _episodes(50, 40),
        VARIANTS,
        settings=TEST_SETTINGS,
    )

    assert report.readiness.status == "directional_only"
    assert report.baseline is None


def test_unresolved_member_of_first_100_is_not_replaced_by_later_episode() -> None:
    episodes = list(_episodes(101, 40))
    episodes[0] = InferenceEpisode(
        pump_event_id=1,
        cluster_key="base:TOKEN0",
        baseline_return_pct=1,
        challenger_returns_pct=(("red", None), ("retrace", 3), ("combined", 4)),
    )

    report = build_entry_challenger_inference(
        tuple(episodes),
        VARIANTS,
        settings=TEST_SETTINGS,
    )

    assert report.readiness.status == "insufficient_resolution"
    assert report.readiness.completely_paired_episodes == 99
    assert report.formal_sample_event_ids[-1] == 100
    assert report.baseline is None


def test_negative_expectancy_is_no_go() -> None:
    report = build_entry_challenger_inference(
        _episodes(100, 40, baseline=-1, challengers=(-2, -3, -4)),
        VARIANTS,
        settings=TEST_SETTINGS,
    )

    assert report.baseline is not None
    assert report.baseline.verdict == "no_go"
    assert all(row.verdict == "no_go" for row in report.challengers)


def test_no_paired_improvement_remains_inconclusive() -> None:
    report = build_entry_challenger_inference(
        _episodes(100, 40, baseline=1, challengers=(1, 1, 1)),
        VARIANTS,
        settings=TEST_SETTINGS,
    )

    assert report.baseline is not None
    assert report.baseline.verdict == "evidence_of_edge"
    assert all(row.strategy.verdict == "evidence_of_edge" for row in report.challengers)
    assert all(row.paired.holm_adjusted_p_value == 1 for row in report.challengers)
    assert all(row.paired.holm_rejected is False for row in report.challengers)
    assert all(row.verdict == "inconclusive" for row in report.challengers)


def test_duplicate_event_ids_are_rejected() -> None:
    duplicate = _episodes(2, 2)
    duplicate = (
        duplicate[0],
        InferenceEpisode(
            pump_event_id=duplicate[0].pump_event_id,
            cluster_key=duplicate[1].cluster_key,
            baseline_return_pct=duplicate[1].baseline_return_pct,
            challenger_returns_pct=duplicate[1].challenger_returns_pct,
        ),
    )

    with pytest.raises(ValueError, match="unique event ids"):
        build_entry_challenger_inference(
            duplicate,
            VARIANTS,
            settings=TEST_SETTINGS,
        )


def test_registered_family_must_match_every_episode() -> None:
    malformed = InferenceEpisode(
        pump_event_id=1,
        cluster_key="base:ERA",
        baseline_return_pct=1,
        challenger_returns_pct=(("red", 2),),
    )

    with pytest.raises(ValueError, match="family"):
        build_entry_challenger_inference(
            (malformed,),
            VARIANTS,
            settings=TEST_SETTINGS,
        )
