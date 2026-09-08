from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.ohlcv import TIMEFRAME_MS, Candle
from schurfer_analytics.replay import ReplayDecision, ReplayEpisode
from schurfer_analytics.virtual_strategy import (
    BASELINE_EXIT_MECHANICS,
    BASELINE_EXIT_POLICY,
    BREAKEVEN_EXIT_POLICY,
    COMBINED_EXIT_POLICY,
    EXIT_POLICIES,
    FIXED_240_ONLY_EXIT_MECHANICS,
    INITIAL_SL_MAX_HOLD_EXIT_MECHANICS,
    MAX_HOLD_ONLY_EXIT_MECHANICS,
    NO_PROGRESS_EXIT_POLICY,
    PRODUCTION_EXIT_POLICY,
    RECENT_PROGRESS_EXTENSION_EXIT_POLICY,
    SCALED_P50_EXIT_POLICY,
    CostParameters,
    ExitMechanics,
    ExitParameters,
    ExitPolicy,
    MarketPath,
    VirtualTrade,
    economics_path_bounds,
    exit_parameters,
    exit_policy_family_path_bounds,
    exit_policy_family_path_is_complete,
    expected_path_bounds,
    market_path_fingerprint,
    select_episode_decision,
    simulate_decision,
    simulate_episode,
    simulate_episode_at_entry,
)
from schurfer_performance.exit_policy import exit_params as shared_exit_params


def _decision(
    *,
    action: str = "skipped",
    minutes: int = 1,
    pump_pct: float = 40.0,
) -> ReplayDecision:
    ts = datetime(2026, 7, 26, 12, 0, tzinfo=UTC) + timedelta(minutes=minutes)
    return ReplayDecision(
        row_id=1,
        decision_id="00000000-0000-0000-0000-000000000001",
        pump_event_id=42,
        event_base="ERA",
        event_first_seen_at=ts - timedelta(minutes=1),
        event_closed_at=ts + timedelta(hours=8),
        ts=ts,
        base="ERA",
        exchange="binance",
        action=action,
        reason="score 5 < threshold 6" if action == "skipped" else "dry_run",
        score=5,
        pump_pct=pump_pct,
        price=99.0,
        strategy_version="pump_short_v1_market_quality",
        features={
            "signal": {"computed_at": ts.timestamp()},
            "config": {"signal_position_usd": 50.0},
        },
        liquidity={
            "status": "sampled",
            "bid_impact_bps": {"100": 3.0},
            "ask_impact_bps": {"100": 4.0},
            "quality": {"depth_target_usd": 100.0},
        },
        outcomes=(),
    )


def _episode(*decisions: ReplayDecision) -> ReplayEpisode:
    return ReplayEpisode(
        pump_event_id=42,
        base="ERA",
        cluster_key="base:ERA",
        decisions=decisions or (_decision(),),
        exclusion_reasons=(),
    )


def _candles(
    decision: ReplayDecision,
    *,
    first: tuple[float, float, float, float] = (100.0, 100.0, 100.0, 100.0),
    close: float = 90.0,
    exit_policy: ExitPolicy = BASELINE_EXIT_POLICY,
    exit_mechanics: ExitMechanics | None = None,
) -> tuple[Candle, ...]:
    start_ms, end_ms = expected_path_bounds(
        decision,
        exit_policy=exit_policy,
        exit_mechanics=exit_mechanics or BASELINE_EXIT_MECHANICS,
    )
    count = (end_ms - start_ms) // TIMEFRAME_MS
    rows = [Candle(start_ms, *first, 1.0)]
    rows.extend(
        Candle(
            start_ms + index * TIMEFRAME_MS,
            close,
            close,
            close,
            close,
            1.0,
        )
        for index in range(1, count)
    )
    return tuple(rows)


def _path(decision: ReplayDecision, candles: tuple[Candle, ...] | None = None) -> MarketPath:
    return MarketPath(
        pump_event_id=42,
        exchange="binance",
        base="ERA",
        status="complete",
        candles=candles if candles is not None else _candles(decision),
    )


@pytest.mark.parametrize(
    ("pump_pct", "expected"),
    [
        # Production reads a 0.0 pump as "magnitude unknown" -- its band
        # selection is `pump_pct or 50.0`, and 0.0 is falsy -- so it lands in
        # the 50-100% band, exactly where None lands. The replay used to
        # disagree here and put 0.0 in the sub-50% band. Matching production is
        # what a replay is for; whether production's reading of 0.0 is the
        # right one is a separate question about production.
        (0.0, ExitParameters(10.0, 12.0, 15.0, 10.0, 120, 240)),
        (49.999, ExitParameters(8.0, 8.0, 12.0, 8.0, 90, 180)),
        (50.0, ExitParameters(10.0, 12.0, 15.0, 10.0, 120, 240)),
        (99.999, ExitParameters(10.0, 12.0, 15.0, 10.0, 120, 240)),
        (100.0, ExitParameters(12.0, 15.0, 20.0, 12.0, 180, 360)),
        (None, ExitParameters(10.0, 12.0, 15.0, 10.0, 120, 240)),
    ],
)
def test_exit_parameters_match_all_production_bands(
    pump_pct: float | None,
    expected: ExitParameters,
) -> None:
    assert exit_parameters(pump_pct) == expected


# Everything the shared production policy returns that this replay does not
# model. `no_progress_min` is here because production has closed stalled
# positions at 60 minutes since 2026-08-18 while the replay's baseline holds
# them to max_hold; that gap is deliberate and documented on
# BASELINE_EXIT_POLICY, but any *new* production parameter must not join it
# silently.
_UNMODELLED_PRODUCTION_PARAMS = frozenset({"no_progress_min"})


@pytest.mark.parametrize("pump_pct", [0.0, 49.999, 50.0, 99.999, 100.0, None])
def test_replay_models_every_production_exit_parameter(pump_pct: float | None) -> None:
    """The literal-value test above restates the numbers, so it agrees with a
    stale copy. This one compares against the shared policy itself: it is the
    check that would have failed when production gained the no-progress exit.
    """
    shared = shared_exit_params(pump_pct)
    modelled = {field.name for field in fields(ExitParameters)}
    assert set(shared) - modelled == _UNMODELLED_PRODUCTION_PARAMS
    replayed = exit_parameters(pump_pct)
    for name in modelled:
        assert getattr(replayed, name) == shared[name], name


@pytest.mark.parametrize(
    "kwargs",
    [
        {"taker_fee_bps_per_side": -1},
        {"taker_fee_bps_per_side": float("nan")},
        {"funding_cost_bps_per_8h": -1},
        {"funding_cost_bps_per_8h": float("inf")},
    ],
)
def test_cost_parameters_reject_invalid_values(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        CostParameters(**kwargs)


def test_exit_policy_family_is_versioned_unique_and_bounded() -> None:
    keys = [policy.key for policy in EXIT_POLICIES]
    versions = [policy.version for policy in EXIT_POLICIES]

    assert keys[0] == "baseline"
    assert len(keys) == len(set(keys))
    assert len(versions) == len(set(versions))
    assert max(policy.maximum_hold_minutes(exit_parameters(40)) for policy in EXIT_POLICIES) == 300


def test_exit_policy_family_path_uses_longest_registered_window() -> None:
    decision = _decision(pump_pct=40)
    start_ms, end_ms = exit_policy_family_path_bounds(decision)
    candles = _candles(decision, exit_policy=NO_PROGRESS_EXIT_POLICY)

    assert end_ms - start_ms == 300 * 60 * 1000
    assert exit_policy_family_path_is_complete(decision, candles) is True
    assert exit_policy_family_path_is_complete(decision, candles[:-1]) is False


def test_economics_path_bounds_cover_fixed_240_and_dynamic_hold() -> None:
    low = _decision(pump_pct=40)
    high = _decision(pump_pct=120)

    low_start, low_end = economics_path_bounds(low)
    high_start, high_end = economics_path_bounds(high)

    assert low_end - low_start == 240 * 60 * 1000
    assert high_end - high_start == 360 * 60 * 1000


@pytest.mark.parametrize(
    "kwargs",
    [
        {"key": "", "version": "v1"},
        {"key": "test", "version": ""},
        {"key": "test", "version": "v1", "no_progress_minutes": 60},
        {"key": "test", "version": "v1", "max_extension_minutes": -5},
        {
            "key": "test",
            "version": "v1",
            "no_progress_minutes": 60,
            "max_extension_minutes": 60,
        },
        {
            "key": "test",
            "version": "v1",
            "max_extension_minutes": 60,
            "minimum_progress_pct": 0.5,
            "recent_progress_lookback_minutes": 30,
        },
    ],
)
def test_exit_policy_rejects_incomplete_or_unbounded_configuration(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        ExitPolicy(**kwargs)  # type: ignore[arg-type]


def test_selection_prefers_first_recorded_open_and_never_future_outcome() -> None:
    skipped = _decision(minutes=1)
    opened = replace(
        _decision(action="opened_dry_run", minutes=2),
        row_id=2,
        decision_id="00000000-0000-0000-0000-000000000002",
    )

    selection = select_episode_decision(_episode(skipped, opened))

    assert selection.decision is opened
    assert selection.taken is True
    assert selection.selection_reason == "first_recorded_open"


def test_initial_stop_wins_ambiguous_activation_bar_conservatively() -> None:
    decision = _decision(action="opened_dry_run")
    candles = _candles(decision, first=(100.0, 109.0, 90.0, 95.0))

    trade = simulate_episode(_episode(decision), _path(decision, candles))

    assert trade.status == "complete"
    assert trade.exit_reason == "initial_sl"
    assert trade.exit_price == pytest.approx(108.0)
    assert trade.ambiguity_resolution == "conservative_stop_first"
    assert trade.classification == "taken_lost"


def test_stop_override_and_position_scale_preserve_fixed_dollar_risk() -> None:
    decision = _decision()
    candles = _candles(decision, first=(100.0, 110.0, 100.0, 109.0))
    episode = _episode(decision)
    path = _path(decision, candles)

    baseline = simulate_decision(
        episode,
        path,
        decision,
        selection_reason="discovery:baseline",
    )
    wider = simulate_decision(
        episode,
        path,
        decision,
        selection_reason="discovery:wider",
        initial_sl_pct_override=12.0,
        position_usd_scale=8.0 / 12.0,
    )

    assert baseline.exit_reason == "initial_sl"
    assert wider.exit_reason != "initial_sl"
    assert wider.position_usd == pytest.approx(50.0 * 8.0 / 12.0)
    assert wider.net_return_pct is not None
    assert wider.position_usd is not None
    assert wider.net_pnl_usd == pytest.approx(wider.position_usd * wider.net_return_pct / 100)


@pytest.mark.parametrize(
    ("initial_sl_pct_override", "position_usd_scale"),
    [(0.0, 1.0), (float("nan"), 1.0), (8.0, 0.0), (8.0, 1.01)],
)
def test_stop_discovery_overrides_reject_invalid_configuration(
    initial_sl_pct_override: float,
    position_usd_scale: float,
) -> None:
    decision = _decision()

    with pytest.raises(ValueError):
        simulate_decision(
            _episode(decision),
            _path(decision),
            decision,
            selection_reason="discovery:invalid",
            initial_sl_pct_override=initial_sl_pct_override,
            position_usd_scale=position_usd_scale,
        )


def test_same_bar_trailing_ambiguity_is_resolved_against_short() -> None:
    decision = _decision()
    candles = _candles(decision, first=(100.0, 101.0, 90.0, 95.0))

    trade = simulate_episode(_episode(decision), _path(decision, candles))

    assert trade.exit_reason == "trailing_stop"
    assert trade.exit_price == pytest.approx(100.8)
    assert trade.ambiguity_resolution == "conservative_stop_first"
    assert trade.classification == "skipped_correctly_avoided"


def test_trail_tightens_at_registered_elapsed_time() -> None:
    decision = _decision()
    candles = list(_candles(decision, close=85.0))
    for index in range(2, 18):
        candles[index] = replace(candles[index], open=85.0, high=85.0, low=80.0, close=85.0)
    candles[18] = replace(candles[18], open=85.0, high=87.0, low=80.0, close=85.0)

    trade = simulate_episode(_episode(decision), _path(decision, tuple(candles)))

    assert trade.exit_reason == "trailing_stop"
    assert trade.exit_price == pytest.approx(86.4)
    assert trade.duration_minutes == 95


def test_max_hold_includes_fees_funding_and_liquidity_costs() -> None:
    decision = _decision()

    trade = simulate_episode(
        _episode(decision),
        _path(decision),
        costs=CostParameters(taker_fee_bps_per_side=10, funding_cost_bps_per_8h=5),
    )

    assert trade.exit_reason == "max_hold"
    assert trade.duration_minutes == 180
    assert trade.gross_return_pct == pytest.approx(10.0)
    assert trade.fee_cost_bps == pytest.approx(20.0)
    assert trade.funding_cost_bps == pytest.approx(1.875)
    assert trade.slippage_cost_bps == pytest.approx(7.0)
    assert trade.net_return_pct == pytest.approx(9.71125)
    assert trade.net_pnl_usd == pytest.approx(4.855625)
    assert trade.classification == "skipped_would_have_won"


def test_exit_mechanics_ablate_stop_trailing_and_clock_on_the_same_path() -> None:
    decision = _decision(pump_pct=40)
    start_ms, end_ms = economics_path_bounds(decision)
    candles = [
        Candle(start_ms, 100, 109, 100, 108, 1),
        *(
            Candle(timestamp, 90, 90, 90, 90, 1)
            for timestamp in range(start_ms + TIMEFRAME_MS, end_ms, TIMEFRAME_MS)
        ),
    ]
    episode = _episode(decision)
    path = _path(decision, tuple(candles))

    baseline = simulate_decision(
        episode,
        path,
        decision,
        selection_reason="economics:full_v1",
    )
    initial_stop_only = simulate_decision(
        episode,
        path,
        decision,
        selection_reason="economics:initial_sl_max_hold",
        exit_mechanics=INITIAL_SL_MAX_HOLD_EXIT_MECHANICS,
    )
    clock_only = simulate_decision(
        episode,
        path,
        decision,
        selection_reason="economics:max_hold_only",
        exit_mechanics=MAX_HOLD_ONLY_EXIT_MECHANICS,
    )
    fixed_240 = simulate_decision(
        episode,
        path,
        decision,
        selection_reason="economics:fixed_240_only",
        exit_mechanics=FIXED_240_ONLY_EXIT_MECHANICS,
    )

    assert baseline.exit_reason == "initial_sl"
    assert initial_stop_only.exit_reason == "initial_sl"
    assert clock_only.exit_reason == "max_hold"
    assert clock_only.duration_minutes == 180
    assert fixed_240.exit_reason == "max_hold"
    assert fixed_240.duration_minutes == 240
    assert baseline.net_return_pct is not None and baseline.net_return_pct < 0
    assert fixed_240.net_return_pct is not None and fixed_240.net_return_pct > 0


def test_baseline_policy_matches_locked_golden_trade() -> None:
    decision = _decision()
    episode = _episode(decision)
    path = _path(decision)
    costs = CostParameters(taker_fee_bps_per_side=10, funding_cost_bps_per_8h=5)

    implicit = simulate_episode(episode, path, costs=costs)
    explicit = simulate_episode(
        episode,
        path,
        costs=costs,
        exit_policy=BASELINE_EXIT_POLICY,
    )

    assert implicit == explicit
    assert implicit.status == "complete"
    assert implicit.selection_reason == "first_decision_counterfactual"
    assert implicit.exit_reason == "max_hold"
    assert implicit.entry_price == 100.0
    assert implicit.exit_price == 90.0
    assert implicit.entry_delay_seconds == 240.0
    assert implicit.duration_minutes == 180.0
    assert implicit.gross_return_pct == 10.0
    assert implicit.net_return_pct == pytest.approx(9.71125)
    assert implicit.mfe_pct == 10.0
    assert implicit.mae_pct == 0.0
    assert implicit.captured_move_pct == 100.0
    assert implicit.classification == "skipped_would_have_won"


def test_breakeven_policy_protects_cost_adjusted_zero_after_activation() -> None:
    decision = _decision()
    candles = list(
        _candles(
            decision,
            first=(100.0, 100.0, 91.0, 92.0),
            close=95.0,
            exit_policy=BREAKEVEN_EXIT_POLICY,
        )
    )
    candles[1] = replace(candles[1], open=99.0, high=100.0, low=95.0, close=99.0)

    trade = simulate_episode(
        _episode(decision),
        _path(decision, tuple(candles)),
        costs=CostParameters(taker_fee_bps_per_side=10, funding_cost_bps_per_8h=5),
        exit_policy=BREAKEVEN_EXIT_POLICY,
    )

    assert trade.exit_reason == "protected_stop"
    assert trade.duration_minutes == 5
    assert trade.net_return_pct == pytest.approx(0.0, abs=1e-12)


def test_no_progress_policy_exits_early_without_favorable_extreme() -> None:
    decision = _decision()
    candles = _candles(
        decision,
        close=100.0,
        exit_policy=NO_PROGRESS_EXIT_POLICY,
    )

    trade = simulate_episode(
        _episode(decision),
        _path(decision, candles),
        exit_policy=NO_PROGRESS_EXIT_POLICY,
    )

    assert trade.exit_reason == "no_progress"
    assert trade.duration_minutes == 60


def test_no_progress_policy_remains_bounded_while_price_keeps_improving() -> None:
    decision = _decision()
    candles = list(
        _candles(
            decision,
            close=99.0,
            exit_policy=NO_PROGRESS_EXIT_POLICY,
        )
    )
    for index, candle in enumerate(candles):
        price = 100.0 - index * 0.3
        candles[index] = replace(
            candle,
            open=price,
            high=price,
            low=price,
            close=price,
        )

    trade = simulate_episode(
        _episode(decision),
        _path(decision, tuple(candles)),
        exit_policy=NO_PROGRESS_EXIT_POLICY,
    )

    assert trade.exit_reason == "absolute_max_hold"
    assert trade.duration_minutes == 300


def test_subthreshold_noise_does_not_reset_no_progress_clock() -> None:
    decision = _decision()
    candles = list(
        _candles(
            decision,
            close=100.0,
            exit_policy=NO_PROGRESS_EXIT_POLICY,
        )
    )
    for index in range(12):
        price = 100.0 - index * 0.03
        candles[index] = replace(
            candles[index],
            open=price,
            high=price,
            low=price,
            close=price,
        )

    trade = simulate_episode(
        _episode(decision),
        _path(decision, tuple(candles)),
        exit_policy=NO_PROGRESS_EXIT_POLICY,
    )

    assert trade.exit_reason == "no_progress"
    assert trade.duration_minutes == 60


def test_recent_progress_policy_extends_once_and_tightens_trail() -> None:
    decision = _decision()
    candles = list(
        _candles(
            decision,
            close=99.0,
            exit_policy=RECENT_PROGRESS_EXTENSION_EXIT_POLICY,
        )
    )
    for index, candle in enumerate(candles):
        price = 100.0 - index * 0.3
        candles[index] = replace(
            candle,
            open=price,
            high=price,
            low=price,
            close=price,
        )

    trade = simulate_episode(
        _episode(decision),
        _path(decision, tuple(candles)),
        exit_policy=RECENT_PROGRESS_EXTENSION_EXIT_POLICY,
    )

    assert trade.exit_reason == "absolute_max_hold"
    assert trade.duration_minutes == 240


def test_recent_progress_policy_closes_at_baseline_boundary_when_stale() -> None:
    decision = _decision()
    candles = list(
        _candles(
            decision,
            close=95.0,
            exit_policy=RECENT_PROGRESS_EXTENSION_EXIT_POLICY,
        )
    )
    candles[0] = replace(candles[0], open=100, high=100, low=99, close=99)

    trade = simulate_episode(
        _episode(decision),
        _path(decision, tuple(candles)),
        exit_policy=RECENT_PROGRESS_EXTENSION_EXIT_POLICY,
    )

    assert trade.exit_reason == "max_hold_no_recent_progress"
    assert trade.duration_minutes == 180


def test_combined_policy_is_bounded_and_protects_breakeven() -> None:
    assert COMBINED_EXIT_POLICY.protect_breakeven_after_activation is True
    assert COMBINED_EXIT_POLICY.no_progress_minutes == 60
    assert COMBINED_EXIT_POLICY.max_extension_minutes == 120
    assert COMBINED_EXIT_POLICY.minimum_progress_pct == 0.5


def test_missing_bar_fails_closed_instead_of_shortening_hold() -> None:
    decision = _decision()
    incomplete = _candles(decision)[:-1]

    trade = simulate_episode(_episode(decision), _path(decision, incomplete))

    assert trade.status == "incomplete_market_path"
    assert trade.classification == "unresolved"
    assert trade.net_return_pct is None


def test_malformed_candle_fails_closed() -> None:
    decision = _decision()
    candles = _candles(decision)
    malformed = (replace(candles[0], high=99.0), *candles[1:])

    trade = simulate_episode(_episode(decision), _path(decision, malformed))

    assert trade.status == "incomplete_market_path"
    assert trade.net_return_pct is None


def test_missing_liquidity_cost_inputs_do_not_become_zero_costs() -> None:
    decision = replace(_decision(), liquidity={"status": "fetch_failed"})

    trade = simulate_episode(_episode(decision), _path(decision))

    assert trade.status == "cost_inputs_unavailable"
    assert trade.net_return_pct is None


def test_market_path_must_match_selected_anchor() -> None:
    decision = _decision()
    mismatched = replace(_path(decision), exchange="bybit")

    trade = simulate_episode(_episode(decision), mismatched)

    assert trade.status == "market_path_mismatch"
    assert trade.classification == "unresolved"


def test_explicit_decision_reuses_exit_engine_for_its_own_venue_and_time() -> None:
    first = _decision(minutes=1)
    selected = replace(
        _decision(minutes=6),
        row_id=2,
        decision_id="00000000-0000-0000-0000-000000000002",
        exchange="bybit",
    )
    path = replace(
        _path(selected),
        exchange="bybit",
        candles=_candles(selected),
    )

    trade = simulate_decision(
        _episode(first, selected),
        path,
        selected,
        selection_reason="threshold:35",
    )

    assert trade.status == "complete"
    assert trade.decision_id == selected.decision_id
    assert trade.exchange == "bybit"
    assert trade.decision_at == selected.ts
    assert trade.selection_reason == "threshold:35"


def test_explicit_decision_rejects_foreign_episode_decision() -> None:
    decision = replace(_decision(), row_id=99)

    with pytest.raises(ValueError, match="does not belong"):
        simulate_decision(
            _episode(),
            _path(decision),
            decision,
            selection_reason="threshold:30",
        )


def test_explicit_delayed_entry_uses_requested_bar_and_full_exit_window() -> None:
    decision = _decision()
    candles = list(_candles(decision))
    delayed_entry_ms = candles[1].ts_ms
    candles[1] = replace(candles[1], open=110, high=110, low=90, close=90)
    candles.append(
        Candle(
            candles[-1].ts_ms + TIMEFRAME_MS,
            90,
            90,
            90,
            90,
            1,
        )
    )

    trade = simulate_episode_at_entry(
        _episode(decision),
        _path(decision, tuple(candles)),
        entry_at_ms=delayed_entry_ms,
        selection_reason="challenger:test_v1",
    )

    assert trade.status == "complete"
    assert trade.entry_at == datetime.fromtimestamp(delayed_entry_ms / 1000, tz=UTC)
    assert trade.entry_price == 110
    assert trade.entry_delay_seconds == pytest.approx(
        delayed_entry_ms / 1000 - decision.ts.timestamp()
    )
    assert trade.selection_reason == "challenger:test_v1"


def test_explicit_entry_rejects_unaligned_or_early_bar() -> None:
    decision = _decision()
    baseline_entry_ms = _candles(decision)[0].ts_ms

    trade = simulate_episode_at_entry(
        _episode(decision),
        _path(decision),
        entry_at_ms=baseline_entry_ms - 1,
        selection_reason="challenger:test_v1",
    )

    assert trade.status == "invalid_virtual_entry"
    assert trade.net_return_pct is None


@pytest.mark.parametrize(
    "costs",
    [
        CostParameters(taker_fee_bps_per_side=0, funding_cost_bps_per_8h=0),
        CostParameters(taker_fee_bps_per_side=20, funding_cost_bps_per_8h=10),
    ],
)
def test_explicit_cost_sensitivity_is_deterministic(costs: CostParameters) -> None:
    decision = _decision()

    first = simulate_episode(_episode(decision), _path(decision), costs=costs)
    second = simulate_episode(_episode(decision), _path(decision), costs=costs)

    assert first == second


def test_market_path_fingerprint_is_order_independent_and_content_sensitive() -> None:
    first_decision = _decision()
    first = _path(first_decision)
    second = replace(first, pump_event_id=43, base="BANK")
    changed = replace(first, candles=(replace(first.candles[0], close=99.0), *first.candles[1:]))

    assert market_path_fingerprint((first, second)) == market_path_fingerprint((second, first))
    assert market_path_fingerprint((first, second)) != market_path_fingerprint((changed, second))


def _flat_price_trade(price: float, policy: ExitPolicy) -> VirtualTrade:
    """A short whose price sits flat at `price` for the whole window: it never
    reaches the initial stop and never triggers a trail, so the only thing that
    can close it is a time-based rule."""
    decision = _decision(pump_pct=40.0)
    candles = _candles(decision, close=price, exit_policy=policy)
    return simulate_episode(_episode(decision), _path(decision, candles), exit_policy=policy)


def test_production_policy_closes_at_60_minutes_when_trailing_never_activated() -> None:
    """The sub-50% band activates trailing at 8%. A short sitting at 96 is up
    4% -- not enough -- so production closes it at 60 minutes, while the policy
    still named `production_max_hold_v1` holds it to max_hold at 180.
    """
    entry_ms = expected_path_bounds(_decision(pump_pct=40.0), exit_policy=PRODUCTION_EXIT_POLICY)[0]
    entry_at = datetime.fromtimestamp(entry_ms / 1000, tz=UTC)

    production = _flat_price_trade(96.0, PRODUCTION_EXIT_POLICY)
    assert production.exit_reason == "not_activated"
    assert production.exit_at == entry_at + timedelta(minutes=60)

    baseline = _flat_price_trade(96.0, BASELINE_EXIT_POLICY)
    assert baseline.exit_reason == "max_hold"
    assert baseline.exit_at == entry_at + timedelta(minutes=180)


def test_production_policy_leaves_an_activated_position_alone_at_60_minutes() -> None:
    """Mirrors evaluate_exit's pre-activation branch: once trailing activates,
    the 60-minute cut stops applying entirely. A short down at 90 is up 10%,
    past the 8% activation, and must survive to max_hold.
    """
    trade = _flat_price_trade(90.0, PRODUCTION_EXIT_POLICY)
    assert trade.exit_reason == "max_hold"


def test_production_policy_still_stops_out_before_the_60_minute_cut() -> None:
    """The cut is not a replacement for the initial stop: an adverse move past
    8% must close as `initial_sl`, not wait for minute 60."""
    trade = _flat_price_trade(109.0, PRODUCTION_EXIT_POLICY)
    assert trade.exit_reason == "initial_sl"


def test_scaled_policy_activates_where_production_cannot(tmp_path: object = None) -> None:
    """The point of HYP-022 in one case. A short that reaches 5% in the first
    hour is past scaled_p50's 3.99% activation and nowhere near production's
    8%, so one of them trails the move and the other closes at minute 60
    without ever having started."""
    decision = _decision(pump_pct=40.0)
    # Opens at 100, dips to 95 (5% in our favour) and comes back to 99.
    start_ms, end_ms = expected_path_bounds(decision, exit_policy=SCALED_P50_EXIT_POLICY)
    count = (end_ms - start_ms) // TIMEFRAME_MS
    rows = [Candle(start_ms, 100.0, 100.0, 100.0, 100.0, 1.0)]
    rows.append(Candle(start_ms + TIMEFRAME_MS, 99.0, 99.0, 95.0, 95.0, 1.0))
    rows.extend(
        Candle(start_ms + index * TIMEFRAME_MS, 99.0, 99.0, 99.0, 99.0, 1.0)
        for index in range(2, count)
    )
    path = _path(decision, tuple(rows))

    scaled = simulate_episode(_episode(decision), path, exit_policy=SCALED_P50_EXIT_POLICY)
    production = simulate_episode(_episode(decision), path, exit_policy=PRODUCTION_EXIT_POLICY)

    assert scaled.exit_reason == "trailing_stop"
    assert production.exit_reason == "not_activated"


def test_scale_overrides_must_be_set_together() -> None:
    """A trail sized for one activation threshold says nothing when paired with
    another, so half an override is a configuration error rather than a
    default."""
    with pytest.raises(ValueError, match="together"):
        ExitPolicy(key="half", version="half_v1", activation_pct_override=4.0)
    with pytest.raises(ValueError, match="together"):
        ExitPolicy(key="half", version="half_v1", trail_pct_override=2.0)


def test_scaled_trail_does_not_tighten() -> None:
    """Tightening after tighten_after_min belongs to the round-number bands. A
    trail already sized to the observed excursion has nothing to tighten to that
    would not be a second, unregistered parameter."""
    assert SCALED_P50_EXIT_POLICY.trail_pct_override == 2.00
    decision = _decision(pump_pct=40.0)
    # Best price 95, then a slow drift back. With a fixed 2% trail the exit is
    # at 96.9 whether it happens at minute 20 or minute 120.
    start_ms, end_ms = expected_path_bounds(decision, exit_policy=SCALED_P50_EXIT_POLICY)
    count = (end_ms - start_ms) // TIMEFRAME_MS
    rows = [Candle(start_ms, 100.0, 100.0, 100.0, 100.0, 1.0)]
    rows.append(Candle(start_ms + TIMEFRAME_MS, 99.0, 99.0, 95.0, 95.0, 1.0))
    rows.extend(
        Candle(start_ms + index * TIMEFRAME_MS, 96.0, 96.0, 96.0, 96.0, 1.0)
        for index in range(2, count)
    )
    trade = simulate_episode(
        _episode(decision), _path(decision, tuple(rows)), exit_policy=SCALED_P50_EXIT_POLICY
    )
    assert trade.exit_reason == "trailing_stop"
    assert trade.exit_price == pytest.approx(95.0 * 1.02)
