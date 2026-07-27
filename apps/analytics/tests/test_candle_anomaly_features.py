from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.candle_anomaly_features import (
    ATR_BARS,
    BLOW_OFF_MIN_BULL_BODY_ATR,
    BLOW_OFF_TOP2_SHARE_PCT,
    CANDLE_ANOMALY_FEATURE_VERSION,
    FORMATION_BARS,
    STRONG_REVERSAL_MIN_BEAR_BODY_ATR,
    STRONG_REVERSAL_MIN_RETURNED_SHARE_PCT,
    VOLUME_ZSCORE_BARS,
    WARMUP_BARS,
    derive_candle_anomaly_features,
    feature_window_bounds,
)
from schurfer_analytics.ohlcv import TIMEFRAME_MS, Candle
from schurfer_analytics.replay import ReplayDecision


def _decision() -> ReplayDecision:
    ts = datetime(2026, 7, 29, 12, 3, tzinfo=UTC)
    return ReplayDecision(
        row_id=1,
        decision_id="00000000-0000-0000-0000-000000000001",
        pump_event_id=42,
        event_base="ERA",
        event_first_seen_at=ts - timedelta(hours=1),
        event_closed_at=ts + timedelta(hours=1),
        ts=ts,
        base="ERA",
        exchange="binance",
        action="skipped",
        reason="score",
        score=7,
        pump_pct=40,
        price=100,
        strategy_version="pump_short_v1_market_quality",
        features={"config": {"signal_position_usd": 50}},
        liquidity={"status": "sampled"},
        outcomes=(),
    )


def _candles(
    decision: ReplayDecision,
    *,
    missing_volume: bool = False,
) -> tuple[Candle, ...]:
    start_ms, end_ms = feature_window_bounds(decision)
    closes = [100.0] * (WARMUP_BARS + FORMATION_BARS)
    first_jump = WARMUP_BARS + 100
    second_jump = first_jump + 1
    closes[first_jump] = 110
    closes[second_jump:] = [120] * (len(closes) - second_jump)
    closes[-1] = 110
    rows: list[Candle] = []
    previous_close = 100.0
    for index, close in enumerate(closes):
        open_ = previous_close
        high = max(open_, close) + 0.5
        low = min(open_, close) - 0.5
        volume = None if missing_volume else 1.0
        rows.append(
            Candle(
                start_ms + index * TIMEFRAME_MS,
                open_,
                high,
                low,
                close,
                volume,
            )
        )
        previous_close = close
    assert rows[-1].ts_ms + TIMEFRAME_MS == end_ms
    return tuple(rows)


def test_feature_contract_constants_are_fully_pinned() -> None:
    assert CANDLE_ANOMALY_FEATURE_VERSION == "candle_anomaly_features_v1"
    assert (FORMATION_BARS, WARMUP_BARS, ATR_BARS, VOLUME_ZSCORE_BARS) == (
        288,
        48,
        14,
        48,
    )
    assert (
        BLOW_OFF_TOP2_SHARE_PCT,
        BLOW_OFF_MIN_BULL_BODY_ATR,
        STRONG_REVERSAL_MIN_BEAR_BODY_ATR,
        STRONG_REVERSAL_MIN_RETURNED_SHARE_PCT,
    ) == (60, 3, 1, 35)


def test_feature_window_uses_only_candles_closed_by_decision() -> None:
    decision = _decision()

    start_ms, end_ms = feature_window_bounds(decision)

    assert end_ms == int(datetime(2026, 7, 29, 12, 0, tzinfo=UTC).timestamp() * 1000)
    assert end_ms - start_ms == (FORMATION_BARS + WARMUP_BARS) * TIMEFRAME_MS


def test_two_expansion_candles_and_last_reversal_form_registered_bucket() -> None:
    decision = _decision()

    features = derive_candle_anomaly_features(decision, _candles(decision))

    assert features.status == "complete"
    assert features.top_2_positive_move_share_pct == pytest.approx(100)
    assert features.max_bull_body_atr == pytest.approx(10)
    assert features.strongest_bull_upper_wick_share_pct == pytest.approx(0.5 / 11 * 100)
    assert features.last_bear_body_atr == pytest.approx(10)
    assert features.returned_pump_share_pct == pytest.approx(10.5 / 20.5 * 100)
    assert features.blow_off is True
    assert features.strong_reversal is True
    assert features.bucket == "blow_off__strong_reversal"
    assert features.volume_status == "complete"
    assert features.volume_zscore_samples == FORMATION_BARS
    assert features.max_volume_zscore == 0


def test_future_candles_do_not_change_point_in_time_features() -> None:
    decision = _decision()
    candles = _candles(decision)
    _, end_ms = feature_window_bounds(decision)
    future = Candle(end_ms, 110, 1000, 1, 999, 1_000_000)

    baseline = derive_candle_anomaly_features(decision, candles)
    with_future = derive_candle_anomaly_features(decision, (*candles, future))

    assert with_future == baseline


def test_missing_volume_is_partial_without_erasing_price_classification() -> None:
    features = derive_candle_anomaly_features(
        _decision(),
        _candles(_decision(), missing_volume=True),
    )

    assert features.status == "partial_volume"
    assert features.volume_status == "unavailable"
    assert features.max_volume_zscore is None
    assert features.bucket == "blow_off__strong_reversal"


def test_missing_duplicate_and_invalid_price_candles_fail_closed() -> None:
    decision = _decision()
    candles = _candles(decision)

    missing = derive_candle_anomaly_features(decision, candles[1:])
    duplicate = derive_candle_anomaly_features(decision, (*candles, candles[0]))
    invalid = derive_candle_anomaly_features(
        decision,
        (replace(candles[0], high=99), *candles[1:]),
    )

    assert missing.status == "unresolved"
    assert "missing required candle" in (missing.error or "")
    assert duplicate.status == "unresolved"
    assert "duplicate required candle" in (duplicate.error or "")
    assert invalid.status == "unresolved"
    assert "invalid required candle" in (invalid.error or "")


def test_no_price_runup_remains_unclassified_instead_of_becoming_grind() -> None:
    decision = _decision()
    start_ms, _ = feature_window_bounds(decision)
    flat = tuple(
        Candle(start_ms + index * TIMEFRAME_MS, 100, 100.5, 99.5, 100, 1)
        for index in range(WARMUP_BARS + FORMATION_BARS)
    )

    features = derive_candle_anomaly_features(decision, flat)

    assert features.status == "unclassified"
    assert features.top_2_positive_move_share_pct is None
    assert features.returned_pump_share_pct == pytest.approx(100)
    assert features.bucket is None
