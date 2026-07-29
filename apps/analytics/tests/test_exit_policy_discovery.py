from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.exit_policy_discovery import (
    ATR_STOP_EXIT_DISCOVERY_VARIANT,
    BASELINE_EXIT_DISCOVERY_VARIANT,
    EXIT_DISCOVERY_ATR_BARS,
    EXIT_DISCOVERY_VARIANTS,
    WIDER_STOP_EXIT_DISCOVERY_VARIANT,
    build_exit_discovery_results,
    effective_initial_stop_pct,
    exit_discovery_path_bounds,
    prior_atr_pct,
)
from schurfer_analytics.ohlcv import TIMEFRAME_MS, Candle
from schurfer_analytics.replay import (
    ReplayDecision,
    ReplayFilters,
    ReplayOutcome,
    build_replay_dataset,
)
from schurfer_analytics.virtual_market import DecisionMarketPath
from schurfer_analytics.virtual_strategy import MarketPath, simulate_decision

COHORT_START = datetime(2026, 7, 22, tzinfo=UTC)


def _decision() -> ReplayDecision:
    decision_at = COHORT_START + timedelta(hours=2, minutes=1)
    return ReplayDecision(
        row_id=1,
        decision_id="00000000-0000-0000-0000-000000000001",
        pump_event_id=42,
        event_base="ERA",
        event_first_seen_at=decision_at - timedelta(hours=1),
        event_closed_at=decision_at + timedelta(hours=8),
        ts=decision_at,
        base="ERA",
        exchange="binance",
        action="skipped",
        reason="score 5 < threshold 6",
        score=5,
        pump_pct=40,
        price=99,
        strategy_version="pump_short_v1_market_quality",
        features={
            "signal": {"computed_at": decision_at.timestamp()},
            "config": {
                "require_market_quality": True,
                "signal_position_usd": 50,
            },
        },
        liquidity={
            "status": "sampled",
            "bid_impact_bps": {"100": 3},
            "ask_impact_bps": {"100": 4},
            "quality": {
                "allowed": True,
                "reason": "ok",
                "depth_target_usd": 100,
            },
        },
        outcomes=(
            ReplayOutcome(
                horizon_minutes=480,
                status="complete",
                anchor_exchange="binance",
                source_exchange="binance",
                entry_price=100,
                forward_price=90,
                mfe_pct=10,
                mae_pct=10,
                short_return_pct=10,
                coverage_ratio=1,
            ),
        ),
    )


def _filters() -> ReplayFilters:
    return ReplayFilters(
        since=COHORT_START,
        until=COHORT_START + timedelta(days=2),
        strategy_versions=("pump_short_v1_market_quality",),
        required_horizons=(480,),
    )


def _candles(
    decision: ReplayDecision,
    *,
    prior_range: float = 2,
    first: tuple[float, float, float, float] = (100, 109, 100, 108),
) -> tuple[Candle, ...]:
    start_ms, end_ms = exit_discovery_path_bounds(decision)
    entry_ms = start_ms + (EXIT_DISCOVERY_ATR_BARS + 1) * TIMEFRAME_MS
    rows: list[Candle] = []
    for timestamp in range(start_ms, entry_ms, TIMEFRAME_MS):
        rows.append(
            Candle(
                timestamp,
                100,
                100 + prior_range / 2,
                100 - prior_range / 2,
                100,
                1,
            )
        )
    rows.append(Candle(entry_ms, *first, 1))
    rows.extend(
        Candle(timestamp, 90, 90, 90, 90, 1)
        for timestamp in range(entry_ms + TIMEFRAME_MS, end_ms, TIMEFRAME_MS)
    )
    return tuple(rows)


def _path(
    decision: ReplayDecision,
    candles: tuple[Candle, ...] | None = None,
) -> DecisionMarketPath:
    return DecisionMarketPath(
        decision.decision_id or "",
        MarketPath(
            pump_event_id=42,
            exchange="binance",
            base="ERA",
            status="complete",
            candles=candles if candles is not None else _candles(decision),
        ),
    )


def test_discovery_path_adds_exact_prior_atr_window() -> None:
    decision = _decision()
    start_ms, end_ms = exit_discovery_path_bounds(decision)
    entry_ms = ((int(decision.ts.timestamp() * 1000) + TIMEFRAME_MS - 1) // TIMEFRAME_MS) * (
        TIMEFRAME_MS
    )

    assert entry_ms - start_ms == (EXIT_DISCOVERY_ATR_BARS + 1) * TIMEFRAME_MS
    assert end_ms > entry_ms


def test_prior_atr_uses_only_complete_pre_entry_bars() -> None:
    decision = _decision()
    path = _path(decision)

    before, error = prior_atr_pct(decision, path.path)
    changed_future = replace(
        path.path,
        candles=tuple(
            replace(candle, high=1_000, close=1_000)
            if candle.ts_ms >= path.path.candles[EXIT_DISCOVERY_ATR_BARS + 1].ts_ms
            else candle
            for candle in path.path.candles
        ),
    )
    after, changed_error = prior_atr_pct(decision, changed_future)

    assert error is None
    assert changed_error is None
    assert before == pytest.approx(2)
    assert after == before


def test_prior_atr_stop_is_clamped_and_preserves_baseline_dollar_risk() -> None:
    decision = _decision()

    effective, scale = effective_initial_stop_pct(
        decision,
        ATR_STOP_EXIT_DISCOVERY_VARIANT,
        prior_atr_value_pct=6,
    )

    assert effective == 16
    assert scale == 0.5


def test_matched_discovery_rescues_baseline_stop_with_scaled_notional() -> None:
    decision = _decision()
    dataset = build_replay_dataset([decision], _filters())

    results = build_exit_discovery_results(dataset, (_path(decision),))

    assert len(results) == len(EXIT_DISCOVERY_VARIANTS)
    by_variant = {result.variant_key: result for result in results}
    baseline = by_variant[BASELINE_EXIT_DISCOVERY_VARIANT.key]
    wider = by_variant[WIDER_STOP_EXIT_DISCOVERY_VARIANT.key]
    assert baseline.trade is not None
    assert baseline.trade.exit_reason == "initial_sl"
    assert wider.trade is not None
    assert wider.trade.exit_reason != "initial_sl"
    assert wider.effective_initial_sl_pct == 12
    assert wider.position_scale == pytest.approx(2 / 3)
    assert wider.trade.position_usd == pytest.approx(50 * 2 / 3)
    assert wider.risk_normalized_net_return_pct == pytest.approx(
        (wider.trade.net_return_pct or 0) * 2 / 3
    )


def test_discovery_baseline_reconciles_to_the_unchanged_replay_engine() -> None:
    decision = _decision()
    dataset = build_replay_dataset([decision], _filters())
    path = _path(decision)

    results = build_exit_discovery_results(dataset, (path,))
    baseline = next(
        result for result in results if result.variant_key == BASELINE_EXIT_DISCOVERY_VARIANT.key
    )
    direct = simulate_decision(
        dataset.eligible_episodes[0],
        path.path,
        decision,
        selection_reason="direct_baseline_reconciliation",
    )

    assert baseline.trade is not None
    assert baseline.trade == replace(
        direct,
        selection_reason=baseline.trade.selection_reason,
    )
    assert baseline.position_scale == 1
    assert baseline.risk_normalized_net_return_pct == baseline.trade.net_return_pct


def test_missing_prior_bar_fails_the_entire_family_closed() -> None:
    decision = _decision()
    dataset = build_replay_dataset([decision], _filters())
    incomplete = _path(decision, _candles(decision)[1:])

    results = build_exit_discovery_results(dataset, (incomplete,))

    assert {result.status for result in results} == {"path_unavailable"}
    assert {result.error for result in results} == {
        "missing one or more bars in the exit-discovery path"
    }
