from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.cex_activity_discovery import (
    CONTROL_BOUNDARY_POLICY_VERSION,
    DISCOVERY_SINCE,
    DISCOVERY_UNTIL,
    HYPOTHESIS_ID,
    MISSING_PATH_RESULT_REASON,
    OUTCOME_HORIZON_MINUTES,
    UNRESOLVED_REASONS,
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
from schurfer_analytics.cex_activity_discovery_report import (
    build_parser,
    check_path_request_count,
    freeze_dataset,
)
from schurfer_analytics.momentum_flow_bidirectional_burst_report import (
    DEFAULT_EXTREME_THRESHOLD_PCT,
)

BASE = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def test_check_path_request_count_rejects_over_limit() -> None:
    with pytest.raises(ValueError, match="over --max-path-requests"):
        check_path_request_count(11, 10)
    check_path_request_count(10, 10)  # exactly at the cap does not raise


def test_cex_v1_primary_threshold_cannot_be_overridden_from_the_cli() -> None:
    base_args = [
        "--since",
        "2026-08-18T00:00:00Z",
        "--until",
        "2026-08-27T00:00:00Z",
        "--code-revision",
        "84d9388",
        "--no-working-tree-dirty",
        "--freeze-artifact",
    ]
    parsed = build_parser().parse_args(base_args)
    assert parsed.extreme_threshold_pct == DEFAULT_EXTREME_THRESHOLD_PCT
    with pytest.raises(SystemExit):
        build_parser().parse_args([*base_args, "--extreme-threshold-pct", "9"])


# --- freeze/evaluate CLI split (colleague review, 2026-09-03) -------------


def test_freeze_artifact_and_from_artifact_are_mutually_exclusive_and_required() -> None:
    required_args = ["--code-revision", "deadbeef", "--no-working-tree-dirty"]
    with pytest.raises(SystemExit):
        build_parser().parse_args(required_args)  # neither given
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [*required_args, "--freeze-artifact", "--from-artifact", "abc123"]
        )  # both given
    parsed = build_parser().parse_args([*required_args, "--freeze-artifact"])
    assert parsed.freeze_artifact is True
    assert parsed.from_artifact is None
    parsed = build_parser().parse_args([*required_args, "--from-artifact", "abc123"])
    assert parsed.freeze_artifact is False
    assert parsed.from_artifact == "abc123"


# --- HYP-016's own frozen window (colleague review, 2026-09-03) -----------


def test_discovery_window_matches_the_registered_ledger_row() -> None:
    """Copied verbatim from docs/research/discovery-ledger.md's own
    HYP-016 row -- a change here means either the ledger or this contract
    drifted from the other, both of which must be caught loudly."""
    assert HYPOTHESIS_ID == "HYP-016"
    assert datetime(2026, 8, 18, tzinfo=UTC) == DISCOVERY_SINCE
    assert datetime(2026, 8, 27, tzinfo=UTC) == DISCOVERY_UNTIL


def test_build_parser_defaults_since_and_until_to_the_frozen_window() -> None:
    args = build_parser().parse_args(
        ["--code-revision", "deadbeef", "--no-working-tree-dirty", "--freeze-artifact"]
    )
    assert args.since == DISCOVERY_SINCE
    assert args.until == DISCOVERY_UNTIL


async def test_freeze_dataset_rejects_a_since_other_than_the_frozen_window() -> None:
    args = build_parser().parse_args(
        [
            "--since",
            "2026-01-01T00:00:00Z",
            "--code-revision",
            "deadbeef",
            "--no-working-tree-dirty",
            "--freeze-artifact",
        ]
    )
    with pytest.raises(ValueError, match="must equal the frozen"):
        await freeze_dataset(args)


async def test_freeze_dataset_rejects_an_until_other_than_the_frozen_window() -> None:
    args = build_parser().parse_args(
        [
            "--until",
            "2026-12-31T00:00:00Z",
            "--code-revision",
            "deadbeef",
            "--no-working-tree-dirty",
            "--freeze-artifact",
        ]
    )
    with pytest.raises(ValueError, match="must equal the frozen"):
        await freeze_dataset(args)


async def test_freeze_dataset_rejects_a_dirty_working_tree() -> None:
    """--freeze-artifact requires --no-working-tree-dirty: a formal freeze
    that becomes a permanent record must not be produced from an
    uncommitted tree. Checked before the since/until check, so a dirty
    tree is caught regardless of what window was requested."""
    args = build_parser().parse_args(
        ["--code-revision", "deadbeef", "--working-tree-dirty", "--freeze-artifact"]
    )
    with pytest.raises(ValueError, match="requires --no-working-tree-dirty"):
        await freeze_dataset(args)


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


# --- ExactPricePath.unresolved_reason (colleague review, 2026-09-03) ------


def test_unresolved_reason_is_none_for_a_resolved_path() -> None:
    assert _path("resolved").unresolved_reason is None


def test_unresolved_reason_missing_entry_bar() -> None:
    path = ExactPricePath(
        request_id="r",
        symbol="TESTUSDT",
        trigger_at=BASE,
        entry_at=BASE + timedelta(minutes=1),
        entry_price=None,
        observed_minutes=0,
        max_high=None,
        min_low=None,
        first_up_25_at=None,
        first_down_25_at=None,
    )
    assert path.unresolved_reason == "missing_entry_bar"
    assert not path.resolved


def test_unresolved_reason_invalid_entry_price() -> None:
    for bad_price in (0.0, -1.0, float("nan"), float("inf")):
        path = ExactPricePath(
            request_id="r",
            symbol="TESTUSDT",
            trigger_at=BASE,
            entry_at=BASE + timedelta(minutes=1),
            entry_price=bad_price,
            observed_minutes=OUTCOME_HORIZON_MINUTES,
            max_high=110.0,
            min_low=95.0,
            first_up_25_at=None,
            first_down_25_at=None,
        )
        assert path.unresolved_reason == "invalid_entry_price", bad_price


def test_unresolved_reason_incomplete_24h_path() -> None:
    path = _path("gap", observed_minutes=OUTCOME_HORIZON_MINUTES - 1)
    assert path.unresolved_reason == "incomplete_24h_path"


def test_unresolved_reason_missing_extrema() -> None:
    path = ExactPricePath(
        request_id="r",
        symbol="TESTUSDT",
        trigger_at=BASE,
        entry_at=BASE + timedelta(minutes=1),
        entry_price=100.0,
        observed_minutes=OUTCOME_HORIZON_MINUTES,
        max_high=None,
        min_low=95.0,
        first_up_25_at=None,
        first_down_25_at=None,
    )
    assert path.unresolved_reason == "missing_extrema"


def test_unresolved_reason_invalid_extrema() -> None:
    path = ExactPricePath(
        request_id="r",
        symbol="TESTUSDT",
        trigger_at=BASE,
        entry_at=BASE + timedelta(minutes=1),
        entry_price=100.0,
        observed_minutes=OUTCOME_HORIZON_MINUTES,
        max_high=-5.0,
        min_low=95.0,
        first_up_25_at=None,
        first_down_25_at=None,
    )
    assert path.unresolved_reason == "invalid_extrema"


def test_unresolved_reasons_cover_every_reason_this_property_can_return() -> None:
    """Every string unresolved_reason can actually return (plus the one
    reason -- missing_path_result -- it structurally cannot, classified by
    the caller instead) must be a member of UNRESOLVED_REASONS, so a
    report's own funnel can never see a reason string outside this frozen
    set."""
    assert MISSING_PATH_RESULT_REASON in UNRESOLVED_REASONS
    for reason in (
        "missing_entry_bar",
        "invalid_entry_price",
        "incomplete_24h_path",
        "missing_extrema",
        "invalid_extrema",
    ):
        assert reason in UNRESOLVED_REASONS


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


# --- CONTROL_BOUNDARY_POLICY_VERSION: within-window only, never outside --
# (colleague review, 2026-09-03, research/cex-activity-discovery-
# completion-v1 planning: this behavior was already correct, but
# unversioned and untested at exactly the two edges that matter.)


def test_control_boundary_policy_version_is_frozen() -> None:
    assert CONTROL_BOUNDARY_POLICY_VERSION == "within_discovery_window_v1"


def test_control_builder_near_since_only_offers_forward_candidates() -> None:
    """An episode right at the discovery window's own start: every
    backward-looking control offset would land before `since`, outside the
    window -- only forward (future) candidates may ever be offered."""
    since = BASE
    until = BASE + timedelta(days=20)
    episode = _episode(1, trigger_at=since)
    controls = build_control_requests((episode,), since=since, until=until)[1]
    assert controls
    assert all(row.trigger_at >= since for row in controls)
    assert all(row.trigger_at > episode.trigger_at for row in controls)


def test_control_builder_near_until_only_offers_backward_candidates() -> None:
    """An episode right at the discovery window's own end (until is
    exclusive): every forward-looking control offset would land at or past
    `until`, outside the window -- only backward (past) candidates may
    ever be offered."""
    since = BASE - timedelta(days=20)
    until = BASE + timedelta(minutes=1)
    episode = _episode(1, trigger_at=BASE)
    controls = build_control_requests((episode,), since=since, until=until)[1]
    assert controls
    assert all(row.trigger_at < until for row in controls)
    assert all(row.trigger_at < episode.trigger_at for row in controls)


def test_control_builder_returns_no_candidates_when_the_window_is_too_narrow() -> None:
    """An episode with no room on either side (a window barely wider than
    the episode's own trigger instant): zero candidates, not an error --
    the episode becomes unmatched, classified explicitly by the report
    layer's own funnel rather than silently disappearing."""
    since = BASE
    until = BASE + timedelta(minutes=1)
    episode = _episode(1, trigger_at=BASE)
    controls = build_control_requests((episode,), since=since, until=until)[1]
    assert controls == ()


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
