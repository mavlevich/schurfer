from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.maker_entry import (
    choose_maker_path,
    evaluate_maker_entry,
    potential_fill,
)
from schurfer_analytics.ohlcv import ONE_MINUTE_MS, TIMEFRAME_MS, Candle
from schurfer_analytics.replay import ReplayDecision, ReplayEpisode
from schurfer_analytics.virtual_market import (
    MAKER_FILL_TIMEOUT_MINUTES,
    MakerDecisionPaths,
    maker_path_bounds,
)
from schurfer_analytics.virtual_strategy import MarketPath


def _decision() -> ReplayDecision:
    ts = datetime(2026, 7, 22, 12, 1, 20, tzinfo=UTC)
    return ReplayDecision(
        row_id=1,
        decision_id="00000000-0000-0000-0000-000000000001",
        pump_event_id=42,
        event_base="ERA",
        event_first_seen_at=ts - timedelta(hours=1),
        event_closed_at=ts + timedelta(hours=8),
        ts=ts,
        base="ERA",
        exchange="binance",
        action="skipped",
        reason="score",
        score=5,
        pump_pct=40,
        price=100,
        strategy_version="pump_short_v1_market_quality",
        features={
            "signal": {"computed_at": ts.timestamp()},
            "config": {
                "require_market_quality": True,
                "signal_position_usd": 50,
            },
        },
        liquidity={
            "status": "sampled",
            "best_bid": 99.9,
            "best_ask": 100.1,
            "bid_impact_bps": {"100": 3},
            "ask_impact_bps": {"100": 4},
            "quality": {
                "allowed": True,
                "depth_target_usd": 100,
            },
        },
        outcomes=(),
    )


def _episode(decision: ReplayDecision | None = None) -> ReplayEpisode:
    selected = decision or _decision()
    return ReplayEpisode(42, "ERA", "base:ERA", (selected,), ())


def _complete_path(
    decision: ReplayDecision,
    timeframe_ms: int,
    *,
    fill: bool,
) -> MarketPath:
    start_ms, end_ms = maker_path_bounds(decision, timeframe_ms)
    candles = []
    for index, timestamp in enumerate(range(start_ms, end_ms, timeframe_ms)):
        if index == 0 and fill:
            candles.append(Candle(timestamp, 100, 120, 50, 100, 1))
        elif fill:
            candles.append(Candle(timestamp, 90, 90, 90, 90, 1))
        else:
            candles.append(Candle(timestamp, 100, 100, 99, 100, 1))
    return MarketPath(42, "binance", "ERA", "complete", tuple(candles))


def _paths(decision: ReplayDecision, *, fill: bool) -> MakerDecisionPaths:
    return MakerDecisionPaths(
        decision.decision_id or "",
        _complete_path(decision, ONE_MINUTE_MS, fill=fill),
        _complete_path(decision, TIMEFRAME_MS, fill=fill),
    )


def test_potential_fill_never_uses_decision_candle_or_timeout_boundary() -> None:
    decision = _decision()
    decision_floor = int(decision.ts.timestamp() * 1000) // ONE_MINUTE_MS * ONE_MINUTE_MS
    start_ms, _ = maker_path_bounds(decision, ONE_MINUTE_MS)
    timeout_end = start_ms + MAKER_FILL_TIMEOUT_MINUTES * 60 * 1000
    candles = (
        Candle(decision_floor, 100, 200, 50, 100, 1),
        Candle(timeout_end, 100, 200, 50, 100, 1),
    )

    fill, evidence, fill_bar_index = potential_fill(
        candles,
        decision,
        limit_price=100.1,
        timeframe_ms=ONE_MINUTE_MS,
    )

    assert fill is None
    assert evidence is None
    assert fill_bar_index is None


def test_potential_fill_labels_touch_and_cross_without_claiming_execution() -> None:
    decision = _decision()
    start_ms, _ = maker_path_bounds(decision, ONE_MINUTE_MS)

    touched, touch_evidence, touch_index = potential_fill(
        (Candle(start_ms, 100, 100.1, 99, 100, 1),),
        decision,
        limit_price=100.1,
        timeframe_ms=ONE_MINUTE_MS,
    )
    crossed, cross_evidence, cross_index = potential_fill(
        (Candle(start_ms, 100, 100.2, 99, 100, 1),),
        decision,
        limit_price=100.1,
        timeframe_ms=ONE_MINUTE_MS,
    )
    marketable, marketable_evidence, marketable_index = potential_fill(
        (Candle(start_ms, 100.2, 100.3, 99, 100, 1),),
        decision,
        limit_price=100.1,
        timeframe_ms=ONE_MINUTE_MS,
    )

    assert touched is not None
    assert touch_evidence == "touched_only"
    assert touch_index == 0
    assert crossed is not None
    assert cross_evidence == "crossed_intrabar"
    assert cross_index == 0
    assert marketable is not None
    assert marketable_evidence == "marketable_on_activation"
    assert marketable_index == 0


def test_later_gap_is_not_labeled_as_activation_rejection_risk() -> None:
    decision = _decision()
    start_ms, _ = maker_path_bounds(decision, ONE_MINUTE_MS)

    fill, evidence, fill_bar_index = potential_fill(
        (
            Candle(start_ms, 100, 100, 99, 100, 1),
            Candle(start_ms + ONE_MINUTE_MS, 100.2, 100.3, 100.1, 100.2, 1),
        ),
        decision,
        limit_price=100.1,
        timeframe_ms=ONE_MINUTE_MS,
    )

    assert fill is not None
    assert evidence == "crossed_between_bars"
    assert fill_bar_index == 1


def test_maker_fill_starts_exposure_after_ambiguous_fill_bar() -> None:
    decision = _decision()
    result = evaluate_maker_entry(_episode(decision), _paths(decision, fill=True))

    assert result.status == "complete"
    assert result.path_timeframe == "1m_primary"
    assert result.maker_trade is not None
    assert result.fill_bar_at_ms is not None
    assert result.maker_trade.entry_at is not None
    assert int(result.maker_trade.entry_at.timestamp() * 1000) == (
        result.fill_bar_at_ms + ONE_MINUTE_MS
    )
    assert result.maker_trade.exit_reason != "initial_sl"
    assert result.maker_trade.entry_price == pytest.approx(100.1)
    assert result.maker_trade.fee_cost_bps == pytest.approx(10)
    assert result.maker_trade.slippage_cost_bps == pytest.approx(4)
    assert result.fill_bar_index == 0
    assert result.fill_delay_minutes == 0
    assert result.taker_1m_trade is not None
    assert result.taker_1m_trade.status == "complete"
    assert result.taker_1m_trade.entry_price == pytest.approx(100)
    assert result.taker_1m_trade.fee_cost_bps == pytest.approx(20)
    assert result.taker_1m_trade.slippage_cost_bps == pytest.approx(7)


def test_unfilled_maker_order_is_cash_and_counts_missed_baseline_winner() -> None:
    decision = _decision()
    paths = _paths(decision, fill=False)
    five = replace(
        paths.five_minute,
        candles=tuple(
            replace(candle, open=100, high=100, low=90, close=90)
            for candle in paths.five_minute.candles
        ),
    )

    result = evaluate_maker_entry(
        _episode(decision),
        replace(paths, five_minute=five),
    )

    assert result.status == "cash_unfilled"
    assert result.episode_net_return_pct == 0
    assert result.missed_baseline_winner is True


def test_five_minute_fallback_is_separate_when_one_minute_path_is_incomplete() -> None:
    decision = _decision()
    paths = _paths(decision, fill=True)
    incomplete_one = replace(
        paths.one_minute,
        candles=paths.one_minute.candles[:-1],
    )

    selected, timeframe, timeframe_ms, error = choose_maker_path(
        replace(paths, one_minute=incomplete_one),
        decision,
    )

    assert selected is paths.five_minute
    assert timeframe == "5m_fallback"
    assert timeframe_ms == TIMEFRAME_MS
    assert error is None


def test_missing_recorded_best_ask_fails_closed() -> None:
    decision = _decision()
    liquidity = dict(decision.liquidity or {})
    liquidity.pop("best_ask")
    decision = replace(decision, liquidity=liquidity)

    result = evaluate_maker_entry(_episode(decision), _paths(decision, fill=True))

    assert result.status == "input_unavailable"
    assert result.error == "recorded best ask is unavailable"
