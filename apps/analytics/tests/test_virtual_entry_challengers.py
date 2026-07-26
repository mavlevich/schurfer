from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.ohlcv import TIMEFRAME_MS, Candle, ceil_to_timeframe
from schurfer_analytics.replay import ReplayDecision
from schurfer_analytics.virtual_entry_challengers import (
    ENTRY_LOOKBACK_BARS,
    ENTRY_VARIANTS,
    EntryVariant,
    challenger_path_bounds,
    evaluate_entry_confirmation,
)
from schurfer_analytics.virtual_strategy import exit_parameters


def _decision() -> ReplayDecision:
    ts = datetime(2026, 7, 27, 12, 1, tzinfo=UTC)
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
        action="skipped",
        reason="score 5 < threshold 6",
        score=5,
        pump_pct=40,
        price=100,
        strategy_version="pump_short_v1_market_quality",
        features={"config": {"signal_position_usd": 50}},
        liquidity={"status": "sampled"},
        outcomes=(),
    )


def _candles(decision: ReplayDecision, *, candidate_count: int = 13) -> tuple[Candle, ...]:
    baseline_entry = ceil_to_timeframe(int(decision.ts.timestamp() * 1000))
    start = baseline_entry - (ENTRY_LOOKBACK_BARS + 1) * TIMEFRAME_MS
    count = ENTRY_LOOKBACK_BARS + candidate_count
    return tuple(
        Candle(
            start + index * TIMEFRAME_MS,
            100,
            100,
            99,
            100,
            1,
        )
        for index in range(count)
    )


def _variant(
    *,
    red: bool = False,
    retrace: float = 0,
    max_wait: int = 60,
    execution_gap: int = 1,
) -> EntryVariant:
    return EntryVariant(
        key="test",
        version="test_v1",
        require_red_candle=red,
        min_retrace_pct=retrace,
        max_wait_minutes=max_wait,
        execution_gap_bars=execution_gap,
    )


def test_registered_entry_family_is_fully_pinned() -> None:
    expected = (
        EntryVariant("red_candle", "entry_red_candle_v1", True, 0.0, 6, 60, 1),
        EntryVariant("retrace_1_5", "entry_retrace_1_5_v1", False, 1.5, 6, 60, 1),
        EntryVariant(
            "red_candle_retrace_1_5",
            "entry_red_candle_retrace_1_5_v1",
            True,
            1.5,
            6,
            60,
            1,
        ),
    )
    assert expected == ENTRY_VARIANTS


def test_challenger_path_includes_predecision_context_wait_and_full_exit() -> None:
    decision = _decision()
    baseline_entry = ceil_to_timeframe(int(decision.ts.timestamp() * 1000))

    start, end = challenger_path_bounds(decision)

    assert start == baseline_entry - (ENTRY_LOOKBACK_BARS + 1) * TIMEFRAME_MS
    assert end == baseline_entry + (60 + exit_parameters(40).max_hold_min) * 60 * 1000


def test_red_confirmation_uses_only_candle_closed_before_entry_bar() -> None:
    decision = _decision()
    candles = list(_candles(decision))
    baseline_entry = ceil_to_timeframe(int(decision.ts.timestamp() * 1000))
    future_index = next(
        index
        for index, candle in enumerate(candles)
        if candle.ts_ms == baseline_entry - TIMEFRAME_MS
    )
    candles[future_index] = replace(candles[future_index], open=100, close=90, low=90)

    confirmation = evaluate_entry_confirmation(
        decision,
        tuple(candles),
        _variant(red=True),
    )

    assert confirmation.status == "confirmed"
    assert confirmation.entry_at_ms == baseline_entry + TIMEFRAME_MS
    assert confirmation.wait_minutes == 5
    assert confirmation.signal_at == datetime.fromtimestamp(baseline_entry / 1000, tz=UTC)


def test_red_confirmation_can_enter_on_first_safe_bar() -> None:
    decision = _decision()
    candles = list(_candles(decision))
    baseline_entry = ceil_to_timeframe(int(decision.ts.timestamp() * 1000))
    last_safe = baseline_entry - 2 * TIMEFRAME_MS
    index = next(index for index, candle in enumerate(candles) if candle.ts_ms == last_safe)
    candles[index] = replace(candles[index], open=100, close=99, low=99)

    confirmation = evaluate_entry_confirmation(
        decision,
        tuple(candles),
        _variant(red=True),
    )

    assert confirmation.status == "confirmed"
    assert confirmation.entry_at_ms == baseline_entry
    assert confirmation.wait_minutes == 0
    assert confirmation.closed_red is True


def test_retrace_exactly_at_registered_boundary_passes() -> None:
    decision = _decision()
    candles = list(_candles(decision))
    baseline_entry = ceil_to_timeframe(int(decision.ts.timestamp() * 1000))
    last_safe = baseline_entry - 2 * TIMEFRAME_MS
    index = next(index for index, candle in enumerate(candles) if candle.ts_ms == last_safe)
    candles[index] = replace(candles[index], open=98, close=98.5, low=98)

    confirmation = evaluate_entry_confirmation(
        decision,
        tuple(candles),
        _variant(retrace=1.5),
    )

    assert confirmation.status == "confirmed"
    assert confirmation.entry_at_ms == baseline_entry
    assert confirmation.retrace_pct == pytest.approx(1.5)
    assert confirmation.closed_red is False


def test_combined_variant_requires_red_and_retrace_on_same_closed_bar() -> None:
    decision = _decision()
    candles = list(_candles(decision))
    baseline_entry = ceil_to_timeframe(int(decision.ts.timestamp() * 1000))
    first_safe = baseline_entry - 2 * TIMEFRAME_MS
    first_index = next(index for index, candle in enumerate(candles) if candle.ts_ms == first_safe)
    candles[first_index] = replace(candles[first_index], open=98, close=98.5, low=98)
    next_safe = baseline_entry - TIMEFRAME_MS
    next_index = next(index for index, candle in enumerate(candles) if candle.ts_ms == next_safe)
    candles[next_index] = replace(candles[next_index], open=100, close=98, low=98)

    confirmation = evaluate_entry_confirmation(
        decision,
        tuple(candles),
        _variant(red=True, retrace=1.5),
    )

    assert confirmation.status == "confirmed"
    assert confirmation.entry_at_ms == baseline_entry + TIMEFRAME_MS
    assert confirmation.closed_red is True
    assert confirmation.retrace_pct == pytest.approx(2)


def test_no_confirmation_is_valid_zero_trade_result() -> None:
    confirmation = evaluate_entry_confirmation(
        _decision(),
        _candles(_decision()),
        _variant(red=True),
    )

    assert confirmation.status == "not_confirmed"
    assert confirmation.entry_at_ms is None
    assert confirmation.wait_minutes == 60
    assert confirmation.closed_red is False


def test_missing_candle_fails_closed_instead_of_skipping_possible_signal() -> None:
    decision = _decision()
    candles = _candles(decision)

    confirmation = evaluate_entry_confirmation(
        decision,
        candles[1:],
        _variant(red=True),
    )

    assert confirmation.status == "unresolved"
    assert confirmation.error is not None
    assert "missing closed entry candle" in confirmation.error


def test_duplicate_candle_fails_closed() -> None:
    decision = _decision()
    candles = _candles(decision)

    confirmation = evaluate_entry_confirmation(
        decision,
        (*candles, candles[0]),
        _variant(red=True),
    )

    assert confirmation.status == "unresolved"
    assert confirmation.error is not None
    assert "duplicate candle" in confirmation.error


@pytest.mark.parametrize(
    "kwargs",
    [
        {"red": False, "retrace": 0},
        {"red": True, "max_wait": 7},
        {"red": True, "retrace": float("nan")},
        {"red": True, "execution_gap": 0},
    ],
)
def test_invalid_variant_contract_is_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _variant(**kwargs)  # type: ignore[arg-type]
