from __future__ import annotations

from datetime import UTC, datetime, timedelta

from schurfer_analytics.liquidation_cascade_cohort_split import CohortBoundaries, Segment
from schurfer_analytics.liquidation_cascade_episodes import CascadeEpisode
from schurfer_analytics.liquidation_cascade_grid_search import EpisodeReplay, MinuteObservation
from schurfer_analytics.liquidation_cascade_repository import IdentityObservation
from schurfer_analytics.liquidation_cascade_statistics import ShuffledLabelControl
from schurfer_analytics.liquidation_cascade_validation_report import (
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


def test_verdict_is_insufficient_data_when_no_candidate_was_selected() -> None:
    verdict, reasons = _verdict(None, _SIGNIFICANT_CONTROL)
    assert verdict == "insufficient_data"
    assert reasons == ["no_validation_selected_candidate"]


def test_verdict_is_insufficient_data_below_the_sample_floor() -> None:
    verdict, reasons = _verdict(_economics(fillable_episodes=3), _SIGNIFICANT_CONTROL)
    assert verdict == "insufficient_data"
    assert "insufficient_test_sample" in reasons


def test_verdict_is_insufficient_data_below_four_distinct_utc_weeks() -> None:
    verdict, reasons = _verdict(_economics(fillable_distinct_utc_weeks=2), _SIGNIFICANT_CONTROL)
    assert verdict == "insufficient_data"
    assert "fewer_than_four_distinct_utc_weeks" in reasons


def test_verdict_is_insufficient_data_below_min_fillable_assets() -> None:
    verdict, reasons = _verdict(_economics(fillable_distinct_assets=2), _SIGNIFICANT_CONTROL)
    assert verdict == "insufficient_data"
    assert "fewer_than_min_fillable_assets" in reasons


def test_verdict_fails_on_non_positive_net_ev() -> None:
    verdict, reasons = _verdict(_economics(mean_net_return_pct=-0.5), _SIGNIFICANT_CONTROL)
    assert verdict == "FAIL"
    assert reasons == ["test_net_ev_non_positive"]


def test_verdict_fails_when_a_single_week_flips_the_sign() -> None:
    sensitivity = {
        "leave_one_week_out": (("2026-W33", -0.1),),
        "leave_one_asset_out": (("BTCUSDT", 1.0),),
    }
    verdict, reasons = _verdict(_economics(sensitivity=sensitivity), _SIGNIFICANT_CONTROL)
    assert verdict == "FAIL"
    assert reasons == ["fails_leave_one_week_out"]


def test_verdict_fails_when_a_single_asset_flips_the_sign() -> None:
    sensitivity = {
        "leave_one_week_out": (("2026-W33", 1.0),),
        "leave_one_asset_out": (("BTCUSDT", -0.2),),
    }
    verdict, reasons = _verdict(_economics(sensitivity=sensitivity), _SIGNIFICANT_CONTROL)
    assert verdict == "FAIL"
    assert reasons == ["fails_leave_one_asset_out"]


def test_verdict_fails_when_shuffled_label_control_is_not_significant() -> None:
    # Regression (colleague review, 2026-08-21): a PASS used to require only
    # positive test EV and leave-one-out robustness -- ten fillable episodes
    # from a single asset/week would have cleared the (then also weaker)
    # diversity floor. The shuffled-label control result must now also be
    # significant, on top of those checks, not merely reported alongside.
    verdict, reasons = _verdict(_economics(), _NOT_SIGNIFICANT_CONTROL)
    assert verdict == "FAIL"
    assert reasons == ["shuffled_label_control_not_significant"]


def test_verdict_passes_a_clean_positive_diverse_significant_test_segment() -> None:
    verdict, reasons = _verdict(_economics(), _SIGNIFICANT_CONTROL)
    assert verdict == "PASS"
    assert reasons == []


def test_identity_stability_requires_ready_status_and_one_identity_key() -> None:
    observations = (
        IdentityObservation(
            native_market_id="STABLEUSDT",
            identity_status="ready",
            identity_key="key-stable",
            onboarded_at=_START,
            captured_at=_START,
        ),
        IdentityObservation(
            native_market_id="STABLEUSDT",
            identity_status="ready",
            identity_key="key-stable",
            onboarded_at=_START,
            captured_at=_START + timedelta(days=1),
        ),
        IdentityObservation(
            native_market_id="RELISTEDUSDT",
            identity_status="ready",
            identity_key="key-relisted-v1",
            onboarded_at=_START,
            captured_at=_START,
        ),
        # Delisted and relisted under the same native market id -- its OWN
        # onboarded_at (and identity_key) changes, even though nothing
        # about this fixture ever mentions a catalog_version.
        IdentityObservation(
            native_market_id="RELISTEDUSDT",
            identity_status="ready",
            identity_key="key-relisted-v2",
            onboarded_at=_START + timedelta(days=2),
            captured_at=_START + timedelta(days=2),
        ),
        IdentityObservation(
            native_market_id="CONFLICTUSDT",
            identity_status="manual_review_required",
            identity_key=None,
            onboarded_at=None,
            captured_at=_START,
        ),
    )
    stability = _identity_stability(observations)
    assert stability["STABLEUSDT"] is True
    assert stability["RELISTEDUSDT"] is False
    assert stability["CONFLICTUSDT"] is False
    assert "NEVEROBSERVEDUSDT" not in stability


def test_replays_from_cache_marks_identity_unresolved_symbols_unresolved() -> None:
    # Regression (colleague review, 2026-08-21): eligibility must be baked
    # in at this single chokepoint so the grid search and the final
    # reported economics can never disagree about which episodes count --
    # a resolved cache entry is not enough on its own if identity isn't
    # stable for that symbol.
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
    first = build_validation_report(
        observations=(),
        replay_cache={},
        identity_stable={},
        boundaries=_BOUNDARIES,
        since=_START,
        until=_UNTIL,
        generated_at=_UNTIL,
        code_revision="a" * 40,
        working_tree_dirty=False,
    )
    second = build_validation_report(
        observations=(),
        replay_cache={},
        identity_stable={},
        boundaries=_BOUNDARIES,
        since=_START,
        until=_UNTIL,
        generated_at=_UNTIL,
        code_revision="a" * 40,
        working_tree_dirty=False,
    )
    assert first == second
    assert first["verdict"] == "insufficient_data"
    assert first["verdict_reasons"] == ["no_validation_selected_candidate"]
    assert first["grid_search"]["candidate"] is None
    assert first["grid_search"]["candidate_test_economics"] is None


def test_build_validation_report_gates_on_the_candidates_own_test_economics() -> None:
    # Regression (colleague review, 2026-08-21): an earlier draft ran the
    # verdict against the PRODUCTION REFERENCE rule's own test-segment
    # economics regardless of which thresholds validation had actually
    # selected. This constructs data where the reference rule (-0.05/
    # -0.15) never triggers in the test segment at all, while a looser
    # combination (which clears discovery/validation and becomes the
    # candidate) does -- proving the two are now genuinely independent.
    exchange = "bybit"
    discovery_time = _START + timedelta(hours=1)
    validation_time = _DISCOVERY_END + timedelta(hours=1)
    test_time = _VALIDATION_END + timedelta(hours=1)

    observations: list[MinuteObservation] = []
    replay_cache: dict[tuple[str, str, datetime], tuple[float | None, str | None]] = {}
    identity_stable: dict[str, bool] = {}

    def _add(
        symbol: str, bucket_start: datetime, *, price_drop_pct: float, oi_drop_pct: float
    ) -> None:
        observations.append(
            MinuteObservation(
                exchange=exchange,
                symbol=symbol,
                bucket_start=bucket_start,
                price_drop_pct=price_drop_pct,
                oi_drop_pct=oi_drop_pct,
                price_complete=True,
                open_interest_complete=True,
            )
        )
        replay_cache[(exchange, symbol, bucket_start)] = (3.0, None)
        identity_stable[symbol] = True

    # Clears -0.03 price / -0.10 OI (a real default grid threshold) but
    # NOT the production reference rule's -0.05 price / -0.15 OI.
    for i in range(10):
        _add(f"D{i}USDT", discovery_time, price_drop_pct=-0.035, oi_drop_pct=-0.11)
        _add(f"D{i}USDT", validation_time, price_drop_pct=-0.035, oi_drop_pct=-0.11)
    _add("T0USDT", test_time, price_drop_pct=-0.04, oi_drop_pct=-0.12)
    _add("T1USDT", test_time, price_drop_pct=-0.04, oi_drop_pct=-0.12)

    report = build_validation_report(
        observations=tuple(observations),
        replay_cache=replay_cache,
        identity_stable=identity_stable,
        boundaries=_BOUNDARIES,
        since=_START,
        until=_UNTIL,
        generated_at=_UNTIL,
        code_revision="a" * 40,
        working_tree_dirty=False,
    )

    candidate = report["grid_search"]["candidate"]
    assert candidate is not None
    assert (candidate["price_drop_trigger_pct"], candidate["oi_drop_trigger_pct"]) != (
        report["reference_rule"]["price_drop_trigger_pct"],
        report["reference_rule"]["oi_drop_trigger_pct"],
    )
    candidate_test = report["grid_search"]["candidate_test_economics"]
    reference_test = report["reference_rule_segments"]["test"]
    assert candidate_test is not None
    assert candidate_test["episodes"] == 2
    assert reference_test["episodes"] == 0
