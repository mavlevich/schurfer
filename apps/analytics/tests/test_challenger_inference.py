from __future__ import annotations

import pytest
from schurfer_analytics.challenger_inference import (
    DEFAULT_INFERENCE_SETTINGS,
    ChallengerEpisode,
    InferenceSettings,
    build_challenger_inference,
)
from schurfer_analytics.replay import DIRECTIONAL_EPISODES, FORMAL_EPISODES, MIN_FORMAL_CLUSTERS

_FAST_SETTINGS = InferenceSettings(
    iterations=100,
    seed=DEFAULT_INFERENCE_SETTINGS.seed,
    confidence_level=DEFAULT_INFERENCE_SETTINGS.confidence_level,
    family_alpha=DEFAULT_INFERENCE_SETTINGS.family_alpha,
)


def _episodes(count: int, *, distinct_clusters: int) -> tuple[ChallengerEpisode, ...]:
    return tuple(
        ChallengerEpisode(
            pump_event_id=i + 1,
            cluster_key=f"base:COIN{i % distinct_clusters}",
            baseline_return_pct=-1.0,
            challenger_returns_pct=(("candidate_a", 2.0),),
        )
        for i in range(count)
    )


# --- backward compatibility: defaults match the shared replay.py constants ---


def test_default_floors_match_the_shared_replay_constants_directional() -> None:
    # One below the shared DIRECTIONAL_EPISODES floor -> "collecting", exactly
    # as before this module accepted overrides.
    episodes = _episodes(DIRECTIONAL_EPISODES - 1, distinct_clusters=MIN_FORMAL_CLUSTERS)
    inference = build_challenger_inference(episodes, ("candidate_a",), settings=_FAST_SETTINGS)
    assert inference.readiness.status == "collecting"


def test_default_floors_match_the_shared_replay_constants_directional_only() -> None:
    episodes = _episodes(DIRECTIONAL_EPISODES, distinct_clusters=MIN_FORMAL_CLUSTERS)
    inference = build_challenger_inference(episodes, ("candidate_a",), settings=_FAST_SETTINGS)
    assert inference.readiness.status == "directional_only"


def test_default_floors_match_the_shared_replay_constants_formal_ready() -> None:
    episodes = _episodes(FORMAL_EPISODES, distinct_clusters=MIN_FORMAL_CLUSTERS)
    inference = build_challenger_inference(episodes, ("candidate_a",), settings=_FAST_SETTINGS)
    assert inference.readiness.status == "formal_sample_ready"


def test_default_insufficient_diversity_unchanged() -> None:
    episodes = _episodes(FORMAL_EPISODES, distinct_clusters=MIN_FORMAL_CLUSTERS - 1)
    inference = build_challenger_inference(episodes, ("candidate_a",), settings=_FAST_SETTINGS)
    assert inference.readiness.status == "insufficient_diversity"


# --- per-family override: a family may register its own, different floor ---


def test_custom_lower_floor_reaches_formal_sample_ready_below_the_shared_default() -> None:
    # 60 episodes / 20 clusters -- well below the shared FORMAL_EPISODES=100 /
    # MIN_FORMAL_CLUSTERS=30 default, but exactly what a family with its own
    # frozen, looser discovery-vs-confirmation bar (token_behavior_discovery_
    # report.py) registers explicitly.
    episodes = _episodes(60, distinct_clusters=20)
    inference = build_challenger_inference(
        episodes,
        ("candidate_a",),
        settings=_FAST_SETTINGS,
        directional_episodes=30,
        formal_episodes=60,
        min_formal_clusters=20,
    )
    assert inference.readiness.status == "formal_sample_ready"
    assert inference.readiness.formal_sample_episodes == 60
    assert len(inference.challengers) == 1


def test_custom_floor_still_gates_below_its_own_threshold() -> None:
    episodes = _episodes(59, distinct_clusters=20)
    inference = build_challenger_inference(
        episodes,
        ("candidate_a",),
        settings=_FAST_SETTINGS,
        directional_episodes=30,
        formal_episodes=60,
        min_formal_clusters=20,
    )
    assert inference.readiness.status == "directional_only"


def test_custom_floor_caps_the_formal_sample_at_its_own_size_not_the_shared_default() -> None:
    # 150 raw episodes but formal_episodes=60 registered -- the formal
    # sample must be capped at 60 (first-chronological, i.e. insertion
    # order here), never silently widened to the shared FORMAL_EPISODES.
    episodes = _episodes(150, distinct_clusters=20)
    inference = build_challenger_inference(
        episodes,
        ("candidate_a",),
        settings=_FAST_SETTINGS,
        directional_episodes=30,
        formal_episodes=60,
        min_formal_clusters=20,
    )
    assert inference.readiness.formal_sample_episodes == 60
    assert len(inference.formal_sample_event_ids) == 60


# --- validation ---


def test_rejects_non_positive_formal_episodes() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        build_challenger_inference(
            _episodes(10, distinct_clusters=5),
            ("candidate_a",),
            formal_episodes=0,
        )


def test_rejects_non_positive_min_formal_clusters() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        build_challenger_inference(
            _episodes(10, distinct_clusters=5),
            ("candidate_a",),
            min_formal_clusters=0,
        )


def test_rejects_directional_episodes_above_formal_episodes() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        build_challenger_inference(
            _episodes(10, distinct_clusters=5),
            ("candidate_a",),
            directional_episodes=61,
            formal_episodes=60,
        )
