from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.cex_activity_discovery import (
    OUTCOME_HORIZON_MINUTES,
    ExactPricePath,
    MatchedMovePair,
    OutcomeSignalEpisode,
    PathRequest,
    build_control_requests,
    build_direction_results,
    favorable_move_pct,
    select_forward_candidate,
    select_matched_pairs,
    signal_request,
)
from schurfer_analytics.cex_activity_discovery_report import build_parser
from schurfer_analytics.momentum_flow_bidirectional_burst_report import (
    DEFAULT_EXTREME_THRESHOLD_PCT,
)

BASE = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def test_cex_v1_primary_threshold_cannot_be_overridden_from_the_cli() -> None:
    base_args = [
        "--since",
        "2026-08-18T00:00:00Z",
        "--until",
        "2026-08-27T00:00:00Z",
        "--code-revision",
        "84d9388",
        "--no-working-tree-dirty",
    ]
    parsed = build_parser().parse_args(base_args)
    assert parsed.extreme_threshold_pct == DEFAULT_EXTREME_THRESHOLD_PCT
    with pytest.raises(SystemExit):
        build_parser().parse_args([*base_args, "--extreme-threshold-pct", "9"])


def _episode(
    episode_id: int,
    *,
    symbol: str = "TESTUSDT",
    direction: str = "buy",
    trigger_at: datetime = BASE,
) -> OutcomeSignalEpisode:
    return OutcomeSignalEpisode(
        episode_id=episode_id,
        signal_id=f"test:{episode_id}",
        source="test_signal_v1",
        exchange="bybit",
        symbol=symbol,
        direction=direction,
        trigger_at=trigger_at,
        entry_at=trigger_at + timedelta(minutes=1),
        signal_value=12.0,
    )


def _path(
    request_id: str,
    *,
    symbol: str = "TESTUSDT",
    trigger_at: datetime = BASE,
    up_hit: bool = False,
    down_hit: bool = False,
    observed_minutes: int = OUTCOME_HORIZON_MINUTES,
) -> ExactPricePath:
    entry_at = trigger_at + timedelta(minutes=1)
    return ExactPricePath(
        request_id=request_id,
        symbol=symbol,
        trigger_at=trigger_at,
        entry_at=entry_at,
        entry_price=100.0,
        observed_minutes=observed_minutes,
        max_high=130.0 if up_hit else 110.0,
        min_low=70.0 if down_hit else 95.0,
        first_up_25_at=entry_at + timedelta(hours=3) if up_hit else None,
        first_down_25_at=entry_at + timedelta(hours=4) if down_hit else None,
    )


def test_exact_path_requires_every_minute_in_the_24h_window() -> None:
    assert _path("resolved").resolved
    assert not _path("gap", observed_minutes=OUTCOME_HORIZON_MINUTES - 1).resolved


def test_controls_keep_same_symbol_and_utc_time_and_require_over_24h_quiet() -> None:
    episode = _episode(1)
    controls = build_control_requests(
        (episode,),
        since=BASE - timedelta(days=7),
        until=BASE + timedelta(days=8),
    )[1]
    assert controls
    assert controls[0].trigger_at == BASE - timedelta(days=2)
    assert all(row.symbol == episode.symbol for row in controls)
    assert all(row.trigger_at.time() == episode.trigger_at.time() for row in controls)


def test_control_builder_excludes_other_bursts_for_the_same_instrument() -> None:
    first = _episode(1)
    second = _episode(2, trigger_at=BASE + timedelta(days=2))
    controls = build_control_requests(
        (first, second),
        since=BASE - timedelta(days=7),
        until=BASE + timedelta(days=8),
    )[1]
    assert BASE + timedelta(days=2) not in {row.trigger_at for row in controls}


def test_pair_selection_never_reuses_one_control_path() -> None:
    first = _episode(1, symbol="ONEUSDT")
    second = _episode(2, symbol="ONEUSDT", trigger_at=BASE + timedelta(hours=2))
    shared_at = BASE - timedelta(days=2)
    first_control = PathRequest(
        "control:1:m2", "ONEUSDT", shared_at, shared_at + timedelta(minutes=1)
    )
    second_shared = PathRequest(
        "control:2:m2", "ONEUSDT", shared_at, shared_at + timedelta(minutes=1)
    )
    second_fallback = PathRequest(
        "control:2:m3",
        "ONEUSDT",
        BASE - timedelta(days=3),
        BASE - timedelta(days=3) + timedelta(minutes=1),
    )
    signal_paths = {
        signal_request(first).request_id: _path(
            signal_request(first).request_id, symbol="ONEUSDT", trigger_at=first.trigger_at
        ),
        signal_request(second).request_id: _path(
            signal_request(second).request_id, symbol="ONEUSDT", trigger_at=second.trigger_at
        ),
    }
    control_paths = {
        first_control.request_id: _path(
            first_control.request_id, symbol="ONEUSDT", trigger_at=shared_at
        ),
        second_shared.request_id: _path(
            second_shared.request_id, symbol="ONEUSDT", trigger_at=shared_at
        ),
        second_fallback.request_id: _path(
            second_fallback.request_id,
            symbol="ONEUSDT",
            trigger_at=second_fallback.trigger_at,
        ),
    }
    pairs = select_matched_pairs(
        (first, second),
        signal_paths=signal_paths,
        control_requests={
            1: (first_control,),
            2: (second_shared, second_fallback),
        },
        control_paths=control_paths,
    )
    assert [pair.control_path.request_id for pair in pairs] == [
        "control:1:m2",
        "control:2:m3",
    ]


def test_pair_selection_maximizes_pairs_instead_of_greedy_first_available() -> None:
    # Colleague review, 2026-09-01: the exact counter-example that shows a
    # greedy "first available" pick is a real bias, not just a style
    # preference. E1 (earlier trigger_at) can use control C1 OR C2; E2
    # (later) can ONLY use C1. Greedy processes E1 first and takes C1 (its
    # own first candidate), leaving E2 with nothing -- 1 pair total, and a
    # systematic loss of later episodes whenever an earlier one shares a
    # control candidate. The correct maximum-cardinality matching is
    # E1->C2, E2->C1 -- 2 pairs, using every resolved control available.
    first = _episode(1, symbol="MATCHUSDT")
    second = _episode(2, symbol="MATCHUSDT", trigger_at=BASE + timedelta(hours=2))
    c1_at = BASE - timedelta(days=2)
    c2_at = BASE - timedelta(days=3)
    first_c1 = PathRequest("control:1:c1", "MATCHUSDT", c1_at, c1_at + timedelta(minutes=1))
    first_c2 = PathRequest("control:1:c2", "MATCHUSDT", c2_at, c2_at + timedelta(minutes=1))
    second_c1 = PathRequest("control:2:c1", "MATCHUSDT", c1_at, c1_at + timedelta(minutes=1))
    signal_paths = {
        signal_request(first).request_id: _path(
            signal_request(first).request_id, symbol="MATCHUSDT", trigger_at=first.trigger_at
        ),
        signal_request(second).request_id: _path(
            signal_request(second).request_id, symbol="MATCHUSDT", trigger_at=second.trigger_at
        ),
    }
    control_paths = {
        first_c1.request_id: _path(first_c1.request_id, symbol="MATCHUSDT", trigger_at=c1_at),
        first_c2.request_id: _path(first_c2.request_id, symbol="MATCHUSDT", trigger_at=c2_at),
        second_c1.request_id: _path(second_c1.request_id, symbol="MATCHUSDT", trigger_at=c1_at),
    }
    pairs = select_matched_pairs(
        (first, second),
        signal_paths=signal_paths,
        control_requests={
            1: (first_c1, first_c2),  # E1: C1 or C2
            2: (second_c1,),  # E2: only C1
        },
        control_paths=control_paths,
    )
    assert len(pairs) == 2  # not 1, the greedy result
    control_at_by_episode = {
        pair.episode.episode_id: pair.control_path.trigger_at for pair in pairs
    }
    assert control_at_by_episode == {1: c2_at, 2: c1_at}


def test_favorable_move_respects_signal_direction() -> None:
    assert favorable_move_pct(_path("up", up_hit=True), "buy") == pytest.approx(30.0)
    assert favorable_move_pct(_path("down", down_hit=True), "sell") == pytest.approx(30.0)


def test_joint_family_can_nominate_only_one_forward_direction() -> None:
    episodes: list[OutcomeSignalEpisode] = []
    pairs: list[MatchedMovePair] = []
    signal_paths: dict[str, ExactPricePath] = {}
    for index in range(100):
        symbol = f"T{index % 20:02d}USDT"
        trigger_at = BASE + timedelta(days=7 if index >= 50 else 0, minutes=index)
        episode = _episode(index + 1, symbol=symbol, trigger_at=trigger_at)
        signal = _path(
            signal_request(episode).request_id,
            symbol=symbol,
            trigger_at=trigger_at,
            up_hit=True,
        )
        control = _path(
            f"control:{episode.episode_id}:m2",
            symbol=symbol,
            trigger_at=trigger_at - timedelta(days=2),
        )
        episodes.append(episode)
        signal_paths[signal.request_id] = signal
        pairs.append(MatchedMovePair(episode, signal, control))

    results = build_direction_results(
        tuple(episodes),
        tuple(pairs),
        signal_paths,
        bootstrap_iterations=100,
    )
    buy = next(row for row in results if row.direction == "buy")
    assert buy.readiness == "discovery_ready"
    assert buy.verdict == "forward_candidate"
    assert buy.signal_hit_rate_pct == 100.0
    assert buy.control_hit_rate_pct == 0.0
    assert select_forward_candidate(results) == "buy"


def test_single_registered_radar_direction_is_not_penalized_as_a_two_way_screen() -> None:
    episodes: list[OutcomeSignalEpisode] = []
    pairs: list[MatchedMovePair] = []
    signal_paths: dict[str, ExactPricePath] = {}
    for index in range(100):
        symbol = f"R{index % 20:02d}USDT"
        trigger_at = BASE + timedelta(days=7 if index >= 50 else 0, minutes=index)
        episode = _episode(index + 1, symbol=symbol, trigger_at=trigger_at)
        signal = _path(
            signal_request(episode).request_id,
            symbol=symbol,
            trigger_at=trigger_at,
            up_hit=True,
        )
        control = _path(
            f"control:{episode.episode_id}:m2",
            symbol=symbol,
            trigger_at=trigger_at - timedelta(days=2),
        )
        episodes.append(episode)
        signal_paths[signal.request_id] = signal
        pairs.append(MatchedMovePair(episode, signal, control))

    (result,) = build_direction_results(
        tuple(episodes),
        tuple(pairs),
        signal_paths,
        bootstrap_iterations=100,
        registered_directions=("buy",),
    )
    assert result.verdict == "forward_candidate"
    assert result.holm_adjusted_p_value == result.raw_p_value
