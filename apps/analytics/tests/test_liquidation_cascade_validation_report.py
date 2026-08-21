from __future__ import annotations

from datetime import UTC, datetime, timedelta

from schurfer_analytics.liquidation_cascade_cohort_split import CohortBoundaries, Segment
from schurfer_analytics.liquidation_cascade_episodes import CascadeEpisode
from schurfer_analytics.liquidation_cascade_grid_search import (
    MIN_FORMAL_SAMPLE_EPISODES,
    EpisodeReplay,
    GridCell,
)
from schurfer_analytics.liquidation_cascade_repository import IdentityLookup, IdentityObservation
from schurfer_analytics.liquidation_cascade_statistics import ShuffledLabelControl
from schurfer_analytics.liquidation_cascade_validation_report import (
    MIN_DISTINCT_UTC_WEEKS,
    MIN_FILLABLE_DISTINCT_ASSETS,
    PROJECTION_CAVEAT,
    Diagnostics,
    SegmentEconomics,
    _identity_stability,
    _replays_from_cache,
    _segment_economics,
    _verdict,
    build_validation_report,
)

_START = datetime(2026, 8, 10, tzinfo=UTC)
_DISCOVERY_END = _START + timedelta(days=3)
_VALIDATION_END = _DISCOVERY_END + timedelta(days=3)
_UNTIL = _VALIDATION_END + timedelta(days=3)
_BOUNDARIES = CohortBoundaries(discovery_end=_DISCOVERY_END, validation_end=_VALIDATION_END)
_SIGNIFICANT_CONTROL = ShuffledLabelControl(
    observed_best_mean_net_return_pct=1.5,
    iterations=2000,
    shuffled_at_or_above_observed=1,
    empirical_p_value=0.001,
)
_NOT_SIGNIFICANT_CONTROL = ShuffledLabelControl(
    observed_best_mean_net_return_pct=1.5,
    iterations=2000,
    shuffled_at_or_above_observed=900,
    empirical_p_value=0.45,
)


def _episode(trigger_at: datetime, *, episode_id: int, symbol: str = "TESTUSDT") -> CascadeEpisode:
    return CascadeEpisode(
        episode_id=episode_id,
        exchange="bybit",
        symbol=symbol,
        trigger_at=trigger_at,
        last_trigger_at=trigger_at,
        peak_price_drop_pct=-0.06,
        peak_oi_drop_pct=-0.2,
        trigger_minutes=1,
        data_quality_unresolved=False,
    )


def _resolved_replay(
    symbol: str, trigger_at: datetime, *, episode_id: int, value: float = 3.0
) -> EpisodeReplay:
    return EpisodeReplay(
        episode=_episode(trigger_at, episode_id=episode_id, symbol=symbol),
        net_return_pct=value,
        unresolved_reason=None,
    )


def _economics(**overrides: object) -> SegmentEconomics:
    base = {
        "segment": "test",
        "episodes": 20,
        "fillable_episodes": 20,
        "unresolved_episodes": 0,
        "distinct_assets": 8,
        "distinct_utc_weeks": 4,
        "fillable_distinct_assets": 8,
        "fillable_distinct_utc_weeks": 4,
        "window_days": 21.0,
        "opportunities_per_day": 1.0,
        "fillable_opportunities_per_day": 1.0,
        "mean_net_return_pct": 1.5,
        "median_net_return_pct": 1.2,
        "profit_factor": 1.8,
        "max_drawdown_usd_at_position": 10.0,
        "worst_losing_streak": 2,
        "capital_occupancy": {"mean_concurrent_positions": 1.0, "window_days": 21.0},
        "projected_monthly_pnl_usd": {"50": 30.0, "100": 60.0, "250": 150.0},
        "projected_monthly_pnl_caveat": "unmeasured impact above the $50 probe",
        "capacity_above_probe_usd": None,
        "sensitivity": {
            "leave_one_week_out": (("2026-W33", 1.0), ("2026-W34", 1.2)),
            "leave_one_asset_out": (("BTCUSDT", 1.1), ("ETHUSDT", 1.3)),
        },
    }
    base.update(overrides)
    return SegmentEconomics(**base)  # type: ignore[arg-type]


def _cell(mean_net_return_pct: float | None, *, formal_sample_ready: bool = True) -> GridCell:
    return GridCell(
        price_drop_trigger_pct=-0.03,
        oi_drop_trigger_pct=-0.10,
        episodes=20,
        resolved_episodes=20,
        unresolved_episodes=0,
        distinct_assets=8,
        mean_net_return_pct=mean_net_return_pct,
        profit_factor=1.8,
        formal_sample_ready=formal_sample_ready,
    )


_POSITIVE_VALIDATION_CELL = _cell(1.5)
_NEGATIVE_VALIDATION_CELL = _cell(-2.29)


def test_verdict_is_insufficient_data_when_no_candidate_was_selected() -> None:
    verdict, reasons = _verdict(
        best_validation_cell=None,
        candidate_test_economics=None,
        shuffled_control=_SIGNIFICANT_CONTROL,
    )
    assert verdict == "insufficient_data"
    assert reasons == ["no_validation_selected_candidate"]


def test_verdict_adds_the_shuffle_reason_even_when_no_candidate_was_selected() -> None:
    verdict, reasons = _verdict(
        best_validation_cell=None,
        candidate_test_economics=None,
        shuffled_control=_NOT_SIGNIFICANT_CONTROL,
    )
    assert verdict == "insufficient_data"
    assert reasons == ["no_validation_selected_candidate", "shuffled_label_control_not_significant"]


def test_verdict_rejects_a_negative_validation_cell_regardless_of_its_test_economics() -> None:
    # Regression (colleague review, 2026-08-21): a real smoke run's
    # validation-selected cell was negative (mean -2.29%, PF 0.12); its own
    # untouched test segment happened to look positive on four hours of
    # data, but that must never rescue a candidate validation already
    # rejected.
    verdict, reasons = _verdict(
        best_validation_cell=_NEGATIVE_VALIDATION_CELL,
        candidate_test_economics=_economics(mean_net_return_pct=4.0),
        shuffled_control=_SIGNIFICANT_CONTROL,
    )
    assert verdict == "FAIL"
    assert reasons == ["validation_net_ev_non_positive"]


def test_verdict_shows_validation_and_shuffle_failure_reasons_together() -> None:
    verdict, reasons = _verdict(
        best_validation_cell=_NEGATIVE_VALIDATION_CELL,
        candidate_test_economics=None,
        shuffled_control=_NOT_SIGNIFICANT_CONTROL,
    )
    assert verdict == "FAIL"
    assert reasons == ["validation_net_ev_non_positive", "shuffled_label_control_not_significant"]


def test_verdict_is_insufficient_data_below_the_sample_floor() -> None:
    verdict, reasons = _verdict(
        best_validation_cell=_POSITIVE_VALIDATION_CELL,
        candidate_test_economics=_economics(fillable_episodes=3),
        shuffled_control=_SIGNIFICANT_CONTROL,
    )
    assert verdict == "insufficient_data"
    assert "insufficient_test_sample" in reasons


def test_verdict_is_insufficient_data_below_four_distinct_utc_weeks() -> None:
    verdict, reasons = _verdict(
        best_validation_cell=_POSITIVE_VALIDATION_CELL,
        candidate_test_economics=_economics(fillable_distinct_utc_weeks=2),
        shuffled_control=_SIGNIFICANT_CONTROL,
    )
    assert verdict == "insufficient_data"
    assert "fewer_than_four_distinct_utc_weeks" in reasons


def test_verdict_shows_data_and_shuffle_reasons_together_even_when_weeks_are_insufficient() -> None:
    # The exact phrasing this test checks (colleague review, 2026-08-21):
    # the shuffle-control reason must appear ALONGSIDE a data-insufficiency
    # reason, not be silently dropped just because weeks are also short.
    verdict, reasons = _verdict(
        best_validation_cell=_POSITIVE_VALIDATION_CELL,
        candidate_test_economics=_economics(fillable_distinct_utc_weeks=2),
        shuffled_control=_NOT_SIGNIFICANT_CONTROL,
    )
    assert verdict == "insufficient_data"
    assert reasons == [
        "fewer_than_four_distinct_utc_weeks",
        "shuffled_label_control_not_significant",
    ]


def test_verdict_is_insufficient_data_below_min_fillable_assets() -> None:
    verdict, reasons = _verdict(
        best_validation_cell=_POSITIVE_VALIDATION_CELL,
        candidate_test_economics=_economics(fillable_distinct_assets=2),
        shuffled_control=_SIGNIFICANT_CONTROL,
    )
    assert verdict == "insufficient_data"
    assert "fewer_than_min_fillable_assets" in reasons


def test_verdict_fails_on_non_positive_net_ev() -> None:
    verdict, reasons = _verdict(
        best_validation_cell=_POSITIVE_VALIDATION_CELL,
        candidate_test_economics=_economics(mean_net_return_pct=-0.5),
        shuffled_control=_SIGNIFICANT_CONTROL,
    )
    assert verdict == "FAIL"
    assert reasons == ["test_net_ev_non_positive"]


def test_verdict_fails_when_a_single_week_flips_the_sign() -> None:
    sensitivity = {
        "leave_one_week_out": (("2026-W33", -0.1),),
        "leave_one_asset_out": (("BTCUSDT", 1.0),),
    }
    verdict, reasons = _verdict(
        best_validation_cell=_POSITIVE_VALIDATION_CELL,
        candidate_test_economics=_economics(sensitivity=sensitivity),
        shuffled_control=_SIGNIFICANT_CONTROL,
    )
    assert verdict == "FAIL"
    assert reasons == ["fails_leave_one_week_out"]


def test_verdict_fails_when_a_single_asset_flips_the_sign() -> None:
    sensitivity = {
        "leave_one_week_out": (("2026-W33", 1.0),),
        "leave_one_asset_out": (("BTCUSDT", -0.2),),
    }
    verdict, reasons = _verdict(
        best_validation_cell=_POSITIVE_VALIDATION_CELL,
        candidate_test_economics=_economics(sensitivity=sensitivity),
        shuffled_control=_SIGNIFICANT_CONTROL,
    )
    assert verdict == "FAIL"
    assert reasons == ["fails_leave_one_asset_out"]


def test_verdict_fails_when_shuffled_label_control_is_not_significant() -> None:
    verdict, reasons = _verdict(
        best_validation_cell=_POSITIVE_VALIDATION_CELL,
        candidate_test_economics=_economics(),
        shuffled_control=_NOT_SIGNIFICANT_CONTROL,
    )
    assert verdict == "FAIL"
    assert reasons == ["shuffled_label_control_not_significant"]


def test_verdict_passes_a_clean_positive_diverse_significant_test_segment() -> None:
    verdict, reasons = _verdict(
        best_validation_cell=_POSITIVE_VALIDATION_CELL,
        candidate_test_economics=_economics(),
        shuffled_control=_SIGNIFICANT_CONTROL,
    )
    assert verdict == "PASS"
    assert reasons == []


# --- Identity stability: baseline-snapshot-plus-changes scheme ---
# See liquidation_cascade_repository.py's own module doc for why a bare
# `captured_at < until` range scan is wrong (colleague review, 2026-08-21,
# confirmed against real data: a 2026-08-15 baseline snapshot was invisible
# to a window starting 2026-08-20 under the old query).

_SINCE = datetime(2026, 8, 20, tzinfo=UTC)
_BASELINE_AT = datetime(2026, 8, 15, tzinfo=UTC)  # before _SINCE
_IN_WINDOW_AT = datetime(2026, 8, 22, tzinfo=UTC)  # inside [_SINCE, until)


def test_baseline_before_window_with_no_in_window_snapshots_is_stable() -> None:
    lookup = IdentityLookup(
        observations=(
            IdentityObservation(
                native_market_id="AUSDT",
                identity_status="ready",
                identity_key="key-a",
                onboarded_at=_BASELINE_AT,
                captured_at=_BASELINE_AT,
            ),
        ),
        relevant_snapshot_timestamps=(_BASELINE_AT,),
    )
    stability = _identity_stability(lookup, since=_SINCE)
    assert stability["AUSDT"] is True


def test_identity_key_changing_across_relevant_snapshots_is_unresolved() -> None:
    lookup = IdentityLookup(
        observations=(
            IdentityObservation(
                native_market_id="AUSDT",
                identity_status="ready",
                identity_key="key-a-v1",
                onboarded_at=_BASELINE_AT,
                captured_at=_BASELINE_AT,
            ),
            IdentityObservation(
                native_market_id="AUSDT",
                identity_status="ready",
                identity_key="key-a-v2",
                onboarded_at=_IN_WINDOW_AT,
                captured_at=_IN_WINDOW_AT,
            ),
        ),
        relevant_snapshot_timestamps=(_BASELINE_AT, _IN_WINDOW_AT),
    )
    stability = _identity_stability(lookup, since=_SINCE)
    assert stability["AUSDT"] is False


def test_onboarded_at_changing_across_relevant_snapshots_is_unresolved() -> None:
    # A delisted-and-relisted ticker under the same native market id --
    # identity_key can even stay the same, onboarded_at is what moves.
    lookup = IdentityLookup(
        observations=(
            IdentityObservation(
                native_market_id="AUSDT",
                identity_status="ready",
                identity_key="key-a",
                onboarded_at=_BASELINE_AT,
                captured_at=_BASELINE_AT,
            ),
            IdentityObservation(
                native_market_id="AUSDT",
                identity_status="ready",
                identity_key="key-a",
                onboarded_at=_IN_WINDOW_AT,
                captured_at=_IN_WINDOW_AT,
            ),
        ),
        relevant_snapshot_timestamps=(_BASELINE_AT, _IN_WINDOW_AT),
    )
    stability = _identity_stability(lookup, since=_SINCE)
    assert stability["AUSDT"] is False


def test_symbol_disappearing_from_an_in_window_snapshot_is_unresolved() -> None:
    # AUSDT has a baseline row but no row at the in-window relevant
    # snapshot -- it was absent from that catalog fetch (a temporary
    # delisting), which a naive "just check the rows we got" comparison
    # would miss entirely.
    lookup = IdentityLookup(
        observations=(
            IdentityObservation(
                native_market_id="AUSDT",
                identity_status="ready",
                identity_key="key-a",
                onboarded_at=_BASELINE_AT,
                captured_at=_BASELINE_AT,
            ),
        ),
        relevant_snapshot_timestamps=(_BASELINE_AT, _IN_WINDOW_AT),
    )
    stability = _identity_stability(lookup, since=_SINCE)
    assert stability["AUSDT"] is False


def test_missing_baseline_before_window_is_unresolved() -> None:
    # Only an in-window observation exists; there is no evidence of what
    # this symbol's identity was before the window started.
    lookup = IdentityLookup(
        observations=(
            IdentityObservation(
                native_market_id="AUSDT",
                identity_status="ready",
                identity_key="key-a",
                onboarded_at=_IN_WINDOW_AT,
                captured_at=_IN_WINDOW_AT,
            ),
        ),
        relevant_snapshot_timestamps=(_IN_WINDOW_AT,),
    )
    stability = _identity_stability(lookup, since=_SINCE)
    assert stability["AUSDT"] is False


def test_unrelated_listing_does_not_break_another_symbols_identity() -> None:
    lookup = IdentityLookup(
        observations=(
            IdentityObservation(
                native_market_id="STABLEUSDT",
                identity_status="ready",
                identity_key="key-stable",
                onboarded_at=_BASELINE_AT,
                captured_at=_BASELINE_AT,
            ),
            IdentityObservation(
                native_market_id="STABLEUSDT",
                identity_status="ready",
                identity_key="key-stable",
                onboarded_at=_BASELINE_AT,
                captured_at=_IN_WINDOW_AT,
            ),
            # A brand-new listing with no baseline of its own -- correctly
            # unresolved ON ITS OWN, but must not affect STABLEUSDT.
            IdentityObservation(
                native_market_id="NEWLISTEDUSDT",
                identity_status="ready",
                identity_key="key-new",
                onboarded_at=_IN_WINDOW_AT,
                captured_at=_IN_WINDOW_AT,
            ),
        ),
        relevant_snapshot_timestamps=(_BASELINE_AT, _IN_WINDOW_AT),
    )
    stability = _identity_stability(lookup, since=_SINCE)
    assert stability["STABLEUSDT"] is True
    assert stability["NEWLISTEDUSDT"] is False


def test_manual_review_required_status_is_unresolved() -> None:
    lookup = IdentityLookup(
        observations=(
            IdentityObservation(
                native_market_id="CONFLICTUSDT",
                identity_status="manual_review_required",
                identity_key=None,
                onboarded_at=None,
                captured_at=_BASELINE_AT,
            ),
        ),
        relevant_snapshot_timestamps=(_BASELINE_AT,),
    )
    stability = _identity_stability(lookup, since=_SINCE)
    assert stability["CONFLICTUSDT"] is False


def test_never_observed_symbol_is_absent_not_stable() -> None:
    lookup = IdentityLookup(observations=(), relevant_snapshot_timestamps=(_BASELINE_AT,))
    stability = _identity_stability(lookup, since=_SINCE)
    assert "NEVEROBSERVEDUSDT" not in stability


def test_replays_from_cache_marks_identity_unresolved_symbols_unresolved() -> None:
    stable_episode = _episode(_START, episode_id=1, symbol="STABLEUSDT")
    unstable_episode = _episode(_START, episode_id=2, symbol="UNSTABLEUSDT")
    cache = {
        ("bybit", "STABLEUSDT", _START): (2.0, None),
        ("bybit", "UNSTABLEUSDT", _START): (2.0, None),
    }
    replays = _replays_from_cache(
        (stable_episode, unstable_episode),
        cache,
        identity_stable={"STABLEUSDT": True, "UNSTABLEUSDT": False},
    )
    by_symbol = {r.episode.symbol: r for r in replays}
    assert by_symbol["STABLEUSDT"].net_return_pct == 2.0
    assert by_symbol["UNSTABLEUSDT"].net_return_pct is None
    assert by_symbol["UNSTABLEUSDT"].unresolved_reason == "identity_unresolved"


def test_replays_from_cache_marks_data_quality_unresolved_episodes_unresolved() -> None:
    episode = CascadeEpisode(
        episode_id=1,
        exchange="bybit",
        symbol="TESTUSDT",
        trigger_at=_START,
        last_trigger_at=_START,
        peak_price_drop_pct=-0.06,
        peak_oi_drop_pct=-0.2,
        trigger_minutes=1,
        data_quality_unresolved=True,
    )
    cache = {("bybit", "TESTUSDT", _START): (2.0, None)}
    (replay,) = _replays_from_cache((episode,), cache, identity_stable={"TESTUSDT": True})
    assert replay.net_return_pct is None
    assert replay.unresolved_reason == "data_quality_unresolved"


def test_segment_economics_only_counts_a_resolved_return_as_fillable() -> None:
    resolved = EpisodeReplay(
        episode=_episode(_START, episode_id=1, symbol="AUSDT"),
        net_return_pct=2.0,
        unresolved_reason=None,
    )
    unresolved = EpisodeReplay(
        episode=_episode(_START, episode_id=2, symbol="BUSDT"),
        net_return_pct=None,
        unresolved_reason="identity_unresolved",
    )
    economics = _segment_economics(
        Segment.TEST, (resolved, unresolved), since=_START, until=_START + timedelta(days=1)
    )
    assert economics.episodes == 2
    assert economics.fillable_episodes == 1
    assert economics.unresolved_episodes == 1


def test_segment_economics_hides_the_monthly_projection_below_the_evidence_floor() -> None:
    # Regression (colleague review, 2026-08-21): a real smoke run's 4-hour/
    # 2-episode/1-week test sample projected to "$1,776/month" -- a number
    # with no real portfolio constraints behind it, published as if it
    # meant something. Below the same sample/diversity floor the verdict
    # gate itself requires, the projection must be explicitly unavailable,
    # never a number computed from too little.
    replays = (
        _resolved_replay("AUSDT", _START, episode_id=1, value=50.0),
        _resolved_replay("BUSDT", _START + timedelta(minutes=1), episode_id=2, value=50.0),
    )
    economics = _segment_economics(
        Segment.TEST, replays, since=_START, until=_START + timedelta(hours=4)
    )
    assert economics.projected_monthly_pnl_usd == {"50": None, "100": None, "250": None}
    assert "unavailable" in economics.projected_monthly_pnl_caveat


def test_segment_economics_publishes_the_monthly_projection_above_the_evidence_floor() -> None:
    # 12 episodes, 6 distinct assets, spread across 4 distinct UTC weeks.
    replays = tuple(
        _resolved_replay(
            f"SYM{i % 6}USDT",
            _START + timedelta(weeks=i % 4, days=i // 4),
            episode_id=i,
            value=1.0,
        )
        for i in range(12)
    )
    economics = _segment_economics(
        Segment.TEST, replays, since=_START, until=_START + timedelta(days=28)
    )
    assert economics.fillable_episodes >= MIN_FORMAL_SAMPLE_EPISODES
    assert economics.fillable_distinct_assets >= MIN_FILLABLE_DISTINCT_ASSETS
    assert economics.fillable_distinct_utc_weeks >= MIN_DISTINCT_UTC_WEEKS
    assert economics.projected_monthly_pnl_usd["50"] is not None
    assert economics.projected_monthly_pnl_caveat == PROJECTION_CAVEAT


def test_segment_economics_computes_a_max_drawdown_and_losing_streak() -> None:
    replays = tuple(
        EpisodeReplay(
            episode=_episode(_START + timedelta(hours=i), episode_id=i, symbol="TESTUSDT"),
            net_return_pct=value,
            unresolved_reason=None,
        )
        for i, value in enumerate([2.0, -1.0, -1.0, -1.0, 3.0], start=1)
    )
    economics = _segment_economics(
        Segment.TEST, replays, since=_START, until=_START + timedelta(days=1)
    )
    assert economics.worst_losing_streak == 3
    assert economics.max_drawdown_usd_at_position is not None
    assert economics.max_drawdown_usd_at_position > 0


def test_build_validation_report_is_deterministic_on_a_thin_fixture() -> None:
    kwargs = dict(
        reference_replays={},
        grid_replays={},
        diagnostics=Diagnostics(),
        boundaries=_BOUNDARIES,
        since=_START,
        until=_UNTIL,
        generated_at=_UNTIL,
        code_revision="a" * 40,
        working_tree_dirty=False,
    )
    first = build_validation_report(**kwargs)  # type: ignore[arg-type]
    second = build_validation_report(**kwargs)  # type: ignore[arg-type]
    assert first == second
    assert first["verdict"] == "insufficient_data"
    assert first["verdict_reasons"] == [
        "no_validation_selected_candidate",
        "shuffled_label_control_not_significant",
    ]
    assert first["grid_search"]["candidate"] is None
    assert first["grid_search"]["candidate_promoted"] is False
    assert first["grid_search"]["candidate_test_economics"] is None


def test_build_validation_report_never_promotes_a_negative_validation_cell() -> None:
    # Regression (colleague review, 2026-08-21): a real smoke run's
    # validation-selected cell was negative; it must never become
    # `candidate`, even though it is still shown as `best_validation_cell`
    # for transparency, and its own test segment must never be evaluated
    # or reported as `candidate_test_economics`.
    losing_cell = (-0.03, -0.10)
    discovery_replays = tuple(
        _resolved_replay(f"D{i}USDT", _START + timedelta(hours=1), episode_id=i, value=1.0)
        for i in range(10)
    )
    # Negative validation mean: half win small, half lose big.
    validation_replays = tuple(
        _resolved_replay(
            f"D{i}USDT",
            _DISCOVERY_END + timedelta(hours=1),
            episode_id=100 + i,
            value=(1.0 if i % 2 == 0 else -10.0),
        )
        for i in range(10)
    )
    # A tempting-looking positive test result that must never be reached.
    test_replays = (
        _resolved_replay("T0USDT", _VALIDATION_END + timedelta(hours=1), episode_id=200, value=4.0),
    )

    report = build_validation_report(
        reference_replays={},
        grid_replays={
            losing_cell: {
                Segment.DISCOVERY: discovery_replays,
                Segment.VALIDATION: validation_replays,
                Segment.TEST: test_replays,
            }
        },
        diagnostics=Diagnostics(),
        boundaries=_BOUNDARIES,
        since=_START,
        until=_UNTIL,
        generated_at=_UNTIL,
        code_revision="a" * 40,
        working_tree_dirty=False,
    )

    assert report["grid_search"]["best_validation_cell"] is not None
    assert report["grid_search"]["best_validation_cell"]["mean_net_return_pct"] < 0
    assert report["grid_search"]["candidate"] is None
    assert report["grid_search"]["candidate_promoted"] is False
    assert report["grid_search"]["candidate_test_economics"] is None
    assert report["verdict"] == "FAIL"
    assert "validation_net_ev_non_positive" in report["verdict_reasons"]


def test_build_validation_report_gates_on_the_candidates_own_test_economics() -> None:
    # Regression (colleague review, 2026-08-21): an earlier draft ran the
    # verdict against the PRODUCTION REFERENCE rule's own test-segment
    # economics regardless of which thresholds validation had actually
    # selected. Here the (-0.03, -0.10) cell has real discovery/validation
    # sample and 2 test-segment episodes; the reference rule's own test
    # segment is left empty entirely -- proving the two are genuinely
    # independent inputs to the report.
    candidate_cell = (-0.03, -0.10)
    discovery_replays = tuple(
        _resolved_replay(f"D{i}USDT", _START + timedelta(hours=1), episode_id=i) for i in range(10)
    )
    validation_replays = tuple(
        _resolved_replay(f"D{i}USDT", _DISCOVERY_END + timedelta(hours=1), episode_id=100 + i)
        for i in range(10)
    )
    test_replays = (
        _resolved_replay("T0USDT", _VALIDATION_END + timedelta(hours=1), episode_id=200, value=4.0),
        _resolved_replay("T1USDT", _VALIDATION_END + timedelta(hours=1), episode_id=201, value=4.0),
    )

    report = build_validation_report(
        reference_replays={Segment.TEST: ()},
        grid_replays={
            candidate_cell: {
                Segment.DISCOVERY: discovery_replays,
                Segment.VALIDATION: validation_replays,
                Segment.TEST: test_replays,
            }
        },
        diagnostics=Diagnostics(),
        boundaries=_BOUNDARIES,
        since=_START,
        until=_UNTIL,
        generated_at=_UNTIL,
        code_revision="a" * 40,
        working_tree_dirty=False,
    )

    candidate = report["grid_search"]["candidate"]
    assert candidate is not None
    assert (candidate["price_drop_trigger_pct"], candidate["oi_drop_trigger_pct"]) == candidate_cell
    assert (candidate["price_drop_trigger_pct"], candidate["oi_drop_trigger_pct"]) != (
        report["reference_rule"]["price_drop_trigger_pct"],
        report["reference_rule"]["oi_drop_trigger_pct"],
    )
    candidate_test = report["grid_search"]["candidate_test_economics"]
    reference_test = report["reference_rule_segments"]["test"]
    assert candidate_test is not None
    assert candidate_test["episodes"] == 2
    assert reference_test["episodes"] == 0
