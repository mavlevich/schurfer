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
