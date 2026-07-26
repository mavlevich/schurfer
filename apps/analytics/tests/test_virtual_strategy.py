from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.ohlcv import TIMEFRAME_MS, Candle
from schurfer_analytics.replay import ReplayDecision, ReplayEpisode
from schurfer_analytics.virtual_strategy import (
    CostParameters,
    ExitParameters,
    MarketPath,
    exit_parameters,
    market_path_fingerprint,
    select_episode_decision,
    simulate_episode,
    simulate_episode_at_entry,
)


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
) -> tuple[Candle, ...]:
    params = exit_parameters(decision.pump_pct)
    decision_ms = int(decision.ts.timestamp() * 1000)
    start_ms = ((decision_ms + TIMEFRAME_MS - 1) // TIMEFRAME_MS) * TIMEFRAME_MS
    count = params.max_hold_min // 5
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
        (0.0, ExitParameters(8.0, 8.0, 12.0, 8.0, 90, 180)),
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
