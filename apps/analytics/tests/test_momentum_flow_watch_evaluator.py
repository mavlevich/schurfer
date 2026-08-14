from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from schurfer_analytics.momentum_flow_watch_contract import (
    WATCH_CONTRACT_SHA256,
    WatchContract,
)
from schurfer_analytics.momentum_flow_watch_evaluator import (
    CrossSectionThresholds,
    PreparedEvaluation,
    SymbolWatchState,
    WatchBar,
    WatchFeatures,
    build_cross_section_thresholds,
    evaluate_prepared,
    percentile,
    prepare_symbol_evaluation,
)


def _contract(**changes: Any) -> WatchContract:
    return replace(
        WatchContract(),
        min_cross_section_size=2,
        **changes,
    )


def _bars(
    *,
    symbol: str = "TESTUSDT",
    start: datetime = datetime(2026, 8, 14, 0, 0, tzinfo=UTC),
    count: int = 61,
    current_price: float = 102.0,
    current_oi: float = 110.0,
) -> tuple[WatchBar, ...]:
    rows: list[WatchBar] = []
    for index in range(count):
        bucket = start + timedelta(minutes=index)
        price = 100.0 + (current_price - 100.0) * index / max(count - 1, 1)
        oi = 100.0 + (current_oi - 100.0) * index / max(count - 1, 1)
        rows.append(
            WatchBar(
                symbol=symbol,
                universe_version="universe-v1",
                bucket_start=bucket,
                created_at=bucket + timedelta(minutes=1, seconds=2),
                close_price=price,
                buy_total_notional_usd=1_000.0 if index >= 46 else 100.0,
                sell_total_notional_usd=200.0 if index >= 46 else 100.0,
                open_interest=oi,
                open_interest_event_at=bucket + timedelta(seconds=30),
                open_interest_observed_at=bucket + timedelta(seconds=31),
                last_trade_event_at=bucket + timedelta(seconds=55),
                last_trade_received_at=bucket + timedelta(seconds=56),
                last_ticker_event_at=bucket + timedelta(seconds=57),
                last_ticker_received_at=bucket + timedelta(seconds=58),
                unbackfilled_gap_minutes=0,
                complete=True,
            )
        )
    return tuple(rows)


def _prepared(symbol: str, features: WatchFeatures) -> PreparedEvaluation:
    bucket = datetime(2026, 8, 14, 1, 0, tzinfo=UTC)
    return PreparedEvaluation(
        symbol=symbol,
        bucket_start=bucket,
        universe_version="universe-v1",
        source_event_at=bucket + timedelta(seconds=55),
        source_received_at=bucket + timedelta(seconds=56),
        bucket_ready_at=bucket + timedelta(minutes=1, seconds=2),
        quality_reasons=(),
        features=features,
    )


def test_contract_rejects_inconsistent_windows() -> None:
    with pytest.raises(ValueError, match="exactly cover"):
        replace(WatchContract(), flow_baseline_minutes=44)


def test_frozen_contract_hash_is_pre_registered_literal() -> None:
    assert WATCH_CONTRACT_SHA256 == (
        "f112c05005e8eb5c81670df09beedb741351bce55c307019aa347552c5dd6f97"
    )


def _features(
    *,
    oi: float = 10.0,
    imbalance: float = 0.7,
    acceleration: float = 3.0,
) -> WatchFeatures:
    return WatchFeatures(
        price_return_60m_pct=2.0,
        price_return_15m_pct=1.0,
        oi_growth_60m_pct=oi,
        buy_notional_15m_usd=20_000.0,
        sell_notional_15m_usd=2_000.0,
        flow_notional_15m_usd=22_000.0,
        buy_imbalance_15m=imbalance,
        flow_acceleration_15m_vs_prior_45m=acceleration,
    )


def test_percentile_uses_deterministic_nearest_rank() -> None:
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.75) == 3.0
    with pytest.raises(ValueError, match="at least one"):
        percentile([], 0.9)


def test_prepare_symbol_evaluation_calculates_frozen_features() -> None:
    bars = _bars()
    result = prepare_symbol_evaluation(
        symbol="TESTUSDT",
        bucket_start=bars[-1].bucket_start,
        bars=bars,
        evaluator_started_at=bars[-1].bucket_start + timedelta(minutes=1, seconds=10),
        contract=_contract(),
    )

    assert result.quality_ready is True
    assert result.features is not None
    assert result.features.price_return_60m_pct == pytest.approx(2.0)
    assert result.features.oi_growth_60m_pct == pytest.approx(10.0)
    assert result.features.buy_notional_15m_usd == 15_000.0
    assert result.features.sell_notional_15m_usd == 3_000.0
    assert result.features.buy_imbalance_15m == pytest.approx(2.0 / 3.0)
    assert result.features.flow_acceleration_15m_vs_prior_45m == pytest.approx(6.0)


def test_prepare_fails_closed_on_incomplete_gap_and_carried_oi() -> None:
    bars = list(_bars())
    bars[20] = replace(bars[20], complete=False, unbackfilled_gap_minutes=1)
    bars[-1] = replace(
        bars[-1],
        open_interest_event_at=bars[-2].bucket_start + timedelta(seconds=30),
    )
    result = prepare_symbol_evaluation(
        symbol="TESTUSDT",
        bucket_start=bars[-1].bucket_start,
        bars=tuple(bars),
        evaluator_started_at=bars[-1].bucket_start + timedelta(minutes=1, seconds=10),
        contract=_contract(),
    )

    assert result.features is None
    assert result.quality_reasons == (
        "incomplete_bar",
        "feed_gap",
        "missing_fresh_oi",
    )


def test_prepare_rejects_mixed_symbol_or_universe_identity() -> None:
    bars = list(_bars())
    bars[10] = replace(bars[10], symbol="OTHERUSDT")
    bars[20] = replace(bars[20], universe_version="universe-v2")

    result = prepare_symbol_evaluation(
        symbol="TESTUSDT",
        bucket_start=bars[-1].bucket_start,
        bars=tuple(bars),
        evaluator_started_at=bars[-1].bucket_start + timedelta(minutes=1, seconds=10),
        contract=_contract(),
    )

    assert "identity_unresolved" in result.quality_reasons


def test_prepare_rejects_symbol_outside_dominant_bucket_universe() -> None:
    bars = _bars()

    result = prepare_symbol_evaluation(
        symbol="TESTUSDT",
        bucket_start=bars[-1].bucket_start,
        bars=bars,
        evaluator_started_at=bars[-1].bucket_start + timedelta(minutes=1, seconds=10),
        expected_universe_version="universe-v2",
        contract=_contract(),
    )

    assert "identity_unresolved" in result.quality_reasons


def test_prepare_rejects_delayed_evaluation_as_stale_quote() -> None:
    bars = _bars()
    result = prepare_symbol_evaluation(
        symbol="TESTUSDT",
        bucket_start=bars[-1].bucket_start,
        bars=bars,
        evaluator_started_at=bars[-1].bucket_start + timedelta(minutes=4),
        contract=_contract(),
    )
    assert "stale_quote" in result.quality_reasons


def test_prepare_rejects_undefined_zero_flow_baseline() -> None:
    bars = tuple(
        replace(
            bar,
            buy_total_notional_usd=(bar.buy_total_notional_usd if index >= 46 else 0.0),
            sell_total_notional_usd=(bar.sell_total_notional_usd if index >= 46 else 0.0),
        )
        for index, bar in enumerate(_bars())
    )

    result = prepare_symbol_evaluation(
        symbol="TESTUSDT",
        bucket_start=bars[-1].bucket_start,
        bars=bars,
        evaluator_started_at=bars[-1].bucket_start + timedelta(minutes=1, seconds=10),
        contract=_contract(),
    )

    assert result.quality_reasons == ("insufficient_flow_baseline",)


def test_prepare_rejects_future_or_non_finite_inputs() -> None:
    bars = list(_bars())
    decision_at = bars[-1].bucket_start + timedelta(minutes=1, seconds=10)
    bars[10] = replace(bars[10], created_at=decision_at + timedelta(seconds=1))
    bars[20] = replace(bars[20], buy_total_notional_usd=float("nan"))

    result = prepare_symbol_evaluation(
        symbol="TESTUSDT",
        bucket_start=bars[-1].bucket_start,
        bars=tuple(bars),
        evaluator_started_at=decision_at,
        contract=_contract(),
    )

    assert "input_not_available_at_decision" in result.quality_reasons
    assert "invalid_numeric_input" in result.quality_reasons


def test_prepare_rejects_non_finite_derived_features() -> None:
    bars = list(_bars())
    bars[0] = replace(bars[0], close_price=5e-324)
    bars[-1] = replace(bars[-1], close_price=1e308)

    result = prepare_symbol_evaluation(
        symbol="TESTUSDT",
        bucket_start=bars[-1].bucket_start,
        bars=tuple(bars),
        evaluator_started_at=bars[-1].bucket_start + timedelta(minutes=1, seconds=10),
        contract=_contract(),
    )

    assert result.quality_reasons == ("invalid_numeric_input",)


def test_source_latency_pair_comes_from_the_same_latest_event() -> None:
    bars = list(_bars())
    current = bars[-1]
    bars[-1] = replace(
        current,
        last_trade_event_at=current.bucket_start + timedelta(seconds=58),
        last_trade_received_at=current.bucket_start + timedelta(seconds=59),
        last_ticker_event_at=current.bucket_start + timedelta(seconds=57),
        last_ticker_received_at=current.bucket_start + timedelta(minutes=1),
    )

    result = prepare_symbol_evaluation(
        symbol="TESTUSDT",
        bucket_start=bars[-1].bucket_start,
        bars=tuple(bars),
        evaluator_started_at=bars[-1].bucket_start + timedelta(minutes=1, seconds=10),
        contract=_contract(),
    )

    assert result.source_event_at == bars[-1].last_trade_event_at
    assert result.source_received_at == bars[-1].last_trade_received_at


def test_cross_section_uses_only_quality_ready_rows() -> None:
    good_a = _prepared("AUSDT", _features(oi=1.0, imbalance=0.2, acceleration=2.0))
    good_b = _prepared("BUSDT", _features(oi=9.0, imbalance=0.8, acceleration=4.0))
    rejected = replace(good_b, symbol="CUSDT", quality_reasons=("feed_gap",), features=None)

    thresholds = build_cross_section_thresholds((good_a, good_b, rejected), contract=_contract())

    assert thresholds.sample_size == 2
    assert thresholds.oi_growth_60m_pct == 9.0
    assert thresholds.buy_imbalance_15m == 0.8
    assert thresholds.flow_acceleration_15m_vs_prior_45m == 4.0


def test_watch_state_machine_emits_once_then_rearms_after_clear_streak() -> None:
    contract = _contract(rearm_clear_minutes=2, watch_cooldown_minutes=1)
    strong = _prepared("TESTUSDT", _features())
    thresholds = CrossSectionThresholds(200, 5.0, 0.5, 2.0)
    at = strong.bucket_start + timedelta(minutes=1)

    first, state = evaluate_prepared(
        strong, thresholds=thresholds, state=SymbolWatchState(), decision_at=at, contract=contract
    )
    second, state = evaluate_prepared(
        strong,
        thresholds=thresholds,
        state=state,
        decision_at=at + timedelta(minutes=1),
        contract=contract,
    )
    weak = replace(strong, features=_features(oi=-1.0))
    _, state = evaluate_prepared(
        weak,
        thresholds=thresholds,
        state=state,
        decision_at=at + timedelta(minutes=2),
        contract=contract,
    )
    _, state = evaluate_prepared(
        weak,
        thresholds=thresholds,
        state=state,
        decision_at=at + timedelta(minutes=3),
        contract=contract,
    )
    third, state = evaluate_prepared(
        replace(strong, bucket_start=strong.bucket_start + timedelta(minutes=4)),
        thresholds=thresholds,
        state=state,
        decision_at=at + timedelta(minutes=4),
        contract=contract,
    )

    assert first.decision_status == "watch"
    assert first.watch_id is not None
    assert state.last_watch_at == strong.bucket_start + timedelta(minutes=4)
    assert second.decision_status == "suppressed_active_episode"
    assert third.decision_status == "watch"
    assert third.watch_id != first.watch_id


def test_quality_failure_does_not_rearm_active_episode() -> None:
    strong = _prepared("TESTUSDT", _features())
    thresholds = CrossSectionThresholds(200, 5.0, 0.5, 2.0)
    state = SymbolWatchState(active_episode=True, clear_streak=4)
    quality_failure = replace(strong, quality_reasons=("feed_gap",), features=None)

    result, next_state = evaluate_prepared(
        quality_failure,
        thresholds=thresholds,
        state=state,
        decision_at=strong.bucket_start + timedelta(minutes=1),
    )

    assert result.decision_status == "rejected_quality"
    assert next_state == state


def test_small_cross_section_does_not_rearm_active_episode() -> None:
    strong = _prepared("TESTUSDT", _features())
    thresholds = CrossSectionThresholds(10, None, None, None)
    state = SymbolWatchState(active_episode=True, clear_streak=4)

    result, next_state = evaluate_prepared(
        strong,
        thresholds=thresholds,
        state=state,
        decision_at=strong.bucket_start + timedelta(minutes=1),
    )

    assert result.reason_codes == ("cross_section_too_small",)
    assert next_state == state


def test_rejected_signal_does_not_accumulate_clear_state_while_inactive() -> None:
    weak = _prepared("TESTUSDT", _features(oi=-1.0))
    thresholds = CrossSectionThresholds(200, 5.0, 0.5, 2.0)
    state = SymbolWatchState(clear_streak=4)

    result, next_state = evaluate_prepared(
        weak,
        thresholds=thresholds,
        state=state,
        decision_at=weak.bucket_start + timedelta(minutes=1),
    )

    assert result.decision_status == "rejected_signal"
    assert next_state == SymbolWatchState()


def test_cooldown_suppresses_new_episode_after_rearm() -> None:
    strong = _prepared("TESTUSDT", _features())
    thresholds = CrossSectionThresholds(200, 5.0, 0.5, 2.0)
    last_watch = strong.bucket_start - timedelta(minutes=10)
    state = SymbolWatchState(active_episode=False, last_watch_at=last_watch)

    result, next_state = evaluate_prepared(
        strong,
        thresholds=thresholds,
        state=state,
        decision_at=strong.bucket_start + timedelta(minutes=1),
    )

    assert result.decision_status == "suppressed_cooldown"
    assert next_state.active_episode is True
    assert next_state.last_watch_at == last_watch


def test_watch_cooldown_uses_bucket_time_not_runtime_time() -> None:
    strong = _prepared("TESTUSDT", _features())
    thresholds = CrossSectionThresholds(200, 5.0, 0.5, 2.0)
    delayed_runtime = strong.bucket_start + timedelta(hours=4)

    result, next_state = evaluate_prepared(
        strong,
        thresholds=thresholds,
        state=SymbolWatchState(),
        decision_at=delayed_runtime,
    )

    assert result.decision_status == "watch"
    assert next_state.last_watch_at == strong.bucket_start
