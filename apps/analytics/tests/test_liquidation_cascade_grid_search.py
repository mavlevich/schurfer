from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.liquidation_cascade_cohort_split import CohortBoundaries, Segment
from schurfer_analytics.liquidation_cascade_episodes import CascadeEpisode
from schurfer_analytics.liquidation_cascade_grid_search import (
    EpisodeReplay,
    MinuteObservation,
    episodes_for_threshold_segment,
    rescore_cells,
    run_grid_search,
    score_grid_cell,
    shortlist,
    to_minute_states,
)

_START = datetime(2026, 8, 17, tzinfo=UTC)
_WIDE_BOUNDARIES = CohortBoundaries(
    discovery_end=_START + timedelta(days=1), validation_end=_START + timedelta(days=2)
)
# mypy strict cannot verify a heterogeneous dict unpacked via ** against a
# specifically-typed function signature -- these are passed as explicit
# kwargs at every call site below instead of `**_SEGMENT_KWARGS`.
_FEATURE_LOOKBACK_MINUTES = 15
_OUTCOME_HORIZON_MINUTES = 60


def _observation(
    minute_offset: int,
    *,
    price_drop_pct: float,
    oi_drop_pct: float,
    symbol: str = "TESTUSDT",
    price_complete: bool = True,
    open_interest_complete: bool = True,
) -> MinuteObservation:
    return MinuteObservation(
        exchange="bybit",
        symbol=symbol,
        bucket_start=_START + timedelta(minutes=minute_offset),
        price_drop_pct=price_drop_pct,
        oi_drop_pct=oi_drop_pct,
        price_complete=price_complete,
        open_interest_complete=open_interest_complete,
    )


def test_to_minute_states_thresholds_independently_per_call() -> None:
    observations = [_observation(0, price_drop_pct=-0.04, oi_drop_pct=-0.12)]
    loose = to_minute_states(observations, price_drop_trigger_pct=-0.03, oi_drop_trigger_pct=-0.10)
    strict = to_minute_states(observations, price_drop_trigger_pct=-0.05, oi_drop_trigger_pct=-0.15)
    assert loose[0].is_qualifying is True
    assert strict[0].is_qualifying is False


def _episode(episode_id: int, *, data_quality_unresolved: bool = False) -> CascadeEpisode:
    return CascadeEpisode(
        episode_id=episode_id,
        exchange="bybit",
        symbol="TESTUSDT",
        trigger_at=_START + timedelta(minutes=episode_id),
        last_trigger_at=_START + timedelta(minutes=episode_id),
        peak_price_drop_pct=-0.06,
        peak_oi_drop_pct=-0.2,
        trigger_minutes=1,
        data_quality_unresolved=data_quality_unresolved,
    )


def test_score_grid_cell_excludes_unresolved_episodes_from_the_mean() -> None:
    resolved = EpisodeReplay(episode=_episode(1), net_return_pct=10.0, unresolved_reason=None)
    unresolved = EpisodeReplay(
        episode=_episode(2, data_quality_unresolved=True),
        net_return_pct=None,
        unresolved_reason="missing_oi",
    )
    cell = score_grid_cell(-0.05, -0.15, (resolved, unresolved))
    assert cell.episodes == 2
    assert cell.resolved_episodes == 1
    assert cell.unresolved_episodes == 1
    assert cell.mean_net_return_pct == pytest.approx(10.0)


def test_score_grid_cell_below_materiality_floor_is_not_formal_sample_ready() -> None:
    replays = tuple(
        EpisodeReplay(episode=_episode(i), net_return_pct=1.0, unresolved_reason=None)
        for i in range(1, 4)
    )
    cell = score_grid_cell(-0.05, -0.15, replays, min_formal_sample_episodes=10)
    assert cell.formal_sample_ready is False


def test_episode_replay_requires_exactly_one_of_return_or_reason() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        EpisodeReplay(episode=_episode(1), net_return_pct=1.0, unresolved_reason="oops")
    with pytest.raises(ValueError, match="exactly one"):
        EpisodeReplay(episode=_episode(1), net_return_pct=None, unresolved_reason=None)


def test_shortlist_excludes_cells_under_the_materiality_floor() -> None:
    observations = [
        _observation(i, price_drop_pct=-0.06, oi_drop_pct=-0.20, symbol=f"SYM{i}USDT")
        for i in range(3)
    ]

    def replay_all_positive(episodes: tuple[CascadeEpisode, ...]) -> tuple[EpisodeReplay, ...]:
        return tuple(
            EpisodeReplay(episode=episode, net_return_pct=5.0, unresolved_reason=None)
            for episode in episodes
        )

    result = run_grid_search(
        observations=observations,
        replay_episodes=replay_all_positive,
        recovery_price_pct=0.02,
        recovery_oi_pct=0.05,
        cooldown_minutes=30,
        price_drop_thresholds_pct=(-0.05,),
        oi_drop_thresholds_pct=(-0.15,),
        min_formal_sample_episodes=10,
        boundaries=_WIDE_BOUNDARIES,
        feature_lookback_minutes=_FEATURE_LOOKBACK_MINUTES,
        outcome_horizon_minutes=_OUTCOME_HORIZON_MINUTES,
    )
    assert shortlist(result) == ()
    assert result.cells[0].episodes == 3
    assert result.cells[0].formal_sample_ready is False


def test_run_grid_search_is_deterministic() -> None:
    observations = [
        _observation(i, price_drop_pct=-0.06, oi_drop_pct=-0.20, symbol=f"SYM{i}USDT")
        for i in range(5)
    ]

    def replay(episodes: tuple[CascadeEpisode, ...]) -> tuple[EpisodeReplay, ...]:
        return tuple(
            EpisodeReplay(
                episode=episode,
                net_return_pct=float(episode.episode_id),
                unresolved_reason=None,
            )
            for episode in episodes
        )

    first = run_grid_search(
        observations=observations,
        replay_episodes=replay,
        recovery_price_pct=0.02,
        recovery_oi_pct=0.05,
        cooldown_minutes=30,
        price_drop_thresholds_pct=(-0.03, -0.05),
        oi_drop_thresholds_pct=(-0.10, -0.15),
        boundaries=_WIDE_BOUNDARIES,
        feature_lookback_minutes=_FEATURE_LOOKBACK_MINUTES,
        outcome_horizon_minutes=_OUTCOME_HORIZON_MINUTES,
    )
    second = run_grid_search(
        observations=observations,
        replay_episodes=replay,
        recovery_price_pct=0.02,
        recovery_oi_pct=0.05,
        cooldown_minutes=30,
        price_drop_thresholds_pct=(-0.03, -0.05),
        oi_drop_thresholds_pct=(-0.10, -0.15),
        boundaries=_WIDE_BOUNDARIES,
        feature_lookback_minutes=_FEATURE_LOOKBACK_MINUTES,
        outcome_horizon_minutes=_OUTCOME_HORIZON_MINUTES,
    )
    assert first.cells == second.cells
    assert first.cell_membership == second.cell_membership
    assert first.episode_returns == second.episode_returns


def test_ranking_never_puts_a_losing_cell_above_a_flat_zero_cell() -> None:
    # Regression (colleague review, 2026-08-21): `mean_net_return_pct or
    # float("-inf")` treats a genuinely 0.0 mean as falsy, so a losing cell
    # at -1.0 would previously outrank a flat 0.0 cell in the leaderboard.
    from schurfer_analytics.liquidation_cascade_grid_search import _ranking_key

    zero_cell = score_grid_cell(
        -0.03,
        0.0,
        (EpisodeReplay(episode=_episode(1), net_return_pct=0.0, unresolved_reason=None),),
        min_formal_sample_episodes=1,
    )
    loss_cell = score_grid_cell(
        -0.05,
        -0.15,
        (EpisodeReplay(episode=_episode(2), net_return_pct=-1.0, unresolved_reason=None),),
        min_formal_sample_episodes=1,
    )
    assert sorted((loss_cell, zero_cell), key=_ranking_key) == [zero_cell, loss_cell]


def test_episodes_for_threshold_segment_excludes_footprints_that_straddle_the_boundary() -> None:
    # An episode whose trigger sits 10 minutes before discovery_end, with a
    # 60-minute outcome horizon, has a footprint that crosses into what
    # would be the validation segment -- it must not be scored as a clean
    # discovery observation (colleague review, 2026-08-21: an earlier draft
    # pre-sliced raw bucket_start before declustering and missed this).
    boundaries = CohortBoundaries(
        discovery_end=_START + timedelta(minutes=20), validation_end=_START + timedelta(days=1)
    )
    straddling = _observation(10, price_drop_pct=-0.06, oi_drop_pct=-0.20, symbol="STRADDLEUSDT")
    clean = _observation(-100, price_drop_pct=-0.06, oi_drop_pct=-0.20, symbol="CLEANUSDT")

    discovery_episodes = episodes_for_threshold_segment(
        [straddling, clean],
        price_drop_trigger_pct=-0.05,
        oi_drop_trigger_pct=-0.15,
        boundaries=boundaries,
        target_segment=Segment.DISCOVERY,
        feature_lookback_minutes=15,
        outcome_horizon_minutes=60,
        recovery_price_pct=0.02,
        recovery_oi_pct=0.05,
        cooldown_minutes=30,
    )
    assert {episode.symbol for episode in discovery_episodes} == {"CLEANUSDT"}

    purge_episodes = episodes_for_threshold_segment(
        [straddling, clean],
        price_drop_trigger_pct=-0.05,
        oi_drop_trigger_pct=-0.15,
        boundaries=boundaries,
        target_segment=Segment.EXCLUDED_PURGE,
        feature_lookback_minutes=15,
        outcome_horizon_minutes=60,
        recovery_price_pct=0.02,
        recovery_oi_pct=0.05,
        cooldown_minutes=30,
    )
    assert {episode.symbol for episode in purge_episodes} == {"STRADDLEUSDT"}


def test_rescore_cells_applies_the_same_purge_rule_on_an_explicit_cell_list() -> None:
    boundaries = CohortBoundaries(
        discovery_end=_START + timedelta(minutes=20), validation_end=_START + timedelta(days=1)
    )
    straddling = _observation(10, price_drop_pct=-0.06, oi_drop_pct=-0.20, symbol="STRADDLEUSDT")

    def replay(episodes: tuple[CascadeEpisode, ...]) -> tuple[EpisodeReplay, ...]:
        return tuple(
            EpisodeReplay(episode=episode, net_return_pct=1.0, unresolved_reason=None)
            for episode in episodes
        )

    discovery_cells = rescore_cells(
        observations=[straddling],
        cells=[(-0.05, -0.15)],
        replay_episodes=replay,
        boundaries=boundaries,
        target_segment=Segment.DISCOVERY,
        feature_lookback_minutes=15,
        outcome_horizon_minutes=60,
        recovery_price_pct=0.02,
        recovery_oi_pct=0.05,
        cooldown_minutes=30,
    )
    assert discovery_cells[0].episodes == 0
