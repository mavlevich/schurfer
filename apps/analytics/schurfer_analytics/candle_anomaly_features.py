"""Look-ahead-safe candle anomaly features for HYP-005."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import fmean, pstdev
from typing import TYPE_CHECKING, Literal

from .ohlcv import TIMEFRAME_MS, Candle
from .virtual_strategy import expected_path_bounds

if TYPE_CHECKING:
    from .replay import ReplayDecision

CANDLE_ANOMALY_FEATURE_VERSION = "candle_anomaly_features_v1"
CANDLE_ANOMALY_COHORT_START = datetime(2026, 7, 29, tzinfo=UTC)
FORMATION_BARS = 288
WARMUP_BARS = 48
ATR_BARS = 14
VOLUME_ZSCORE_BARS = 48
BLOW_OFF_TOP2_SHARE_PCT = 60.0
BLOW_OFF_MIN_BULL_BODY_ATR = 3.0
STRONG_REVERSAL_MIN_BEAR_BODY_ATR = 1.0
STRONG_REVERSAL_MIN_RETURNED_SHARE_PCT = 35.0

FeatureStatus = Literal["complete", "partial_volume", "unclassified", "unresolved"]
VolumeStatus = Literal["complete", "partial", "unavailable"]


@dataclass(frozen=True)
class CandleAnomalyFeatures:
    version: str
    status: FeatureStatus
    feature_cutoff_at: datetime
    formation_start_at: datetime
    formation_end_at: datetime
    formation_bars: int
    warmup_bars: int
    formation_return_pct: float | None
    formation_peak_return_pct: float | None
    positive_move_count: int | None
    top_1_positive_move_share_pct: float | None
    top_2_positive_move_share_pct: float | None
    max_bull_body_atr: float | None
    max_range_atr: float | None
    strongest_bull_upper_wick_share_pct: float | None
    max_volume_zscore: float | None
    volume_zscore_samples: int
    volume_status: VolumeStatus
    last_bear_body_atr: float | None
    returned_pump_share_pct: float | None
    blow_off: bool | None
    strong_reversal: bool | None
    bucket: str | None
    error: str | None = None


def feature_window_bounds(decision: ReplayDecision) -> tuple[int, int]:
    """Return the exact warm-up plus formation interval [start, end)."""
    decision_ms = int(decision.ts.timestamp() * 1000)
    end_ms = decision_ms // TIMEFRAME_MS * TIMEFRAME_MS
    start_ms = end_ms - (WARMUP_BARS + FORMATION_BARS) * TIMEFRAME_MS
    return start_ms, end_ms


def candle_anomaly_path_bounds(decision: ReplayDecision) -> tuple[int, int]:
    """Return one exact path covering feature context and baseline exit replay."""
    feature_start_ms, _ = feature_window_bounds(decision)
    _, exit_end_ms = expected_path_bounds(decision)
    return feature_start_ms, exit_end_ms


def _valid_price_candle(candle: Candle) -> bool:
    prices = (candle.open, candle.high, candle.low, candle.close)
    return (
        candle.ts_ms > 0
        and candle.ts_ms % TIMEFRAME_MS == 0
        and all(math.isfinite(value) and value > 0 for value in prices)
        and candle.high >= max(candle.open, candle.close, candle.low)
        and candle.low <= min(candle.open, candle.close, candle.high)
        and (candle.volume is None or (math.isfinite(candle.volume) and candle.volume >= 0))
    )


def _true_range(candle: Candle, previous_close: float) -> float:
    return max(
        candle.high - candle.low,
        abs(candle.high - previous_close),
        abs(candle.low - previous_close),
    )


def _unresolved(
    *,
    end_ms: int,
    error: str,
) -> CandleAnomalyFeatures:
    return CandleAnomalyFeatures(
        version=CANDLE_ANOMALY_FEATURE_VERSION,
        status="unresolved",
        feature_cutoff_at=datetime.fromtimestamp(end_ms / 1000, tz=UTC),
        formation_start_at=datetime.fromtimestamp(
            (end_ms - FORMATION_BARS * TIMEFRAME_MS) / 1000,
            tz=UTC,
        ),
        formation_end_at=datetime.fromtimestamp(end_ms / 1000, tz=UTC),
        formation_bars=FORMATION_BARS,
        warmup_bars=WARMUP_BARS,
        formation_return_pct=None,
        formation_peak_return_pct=None,
        positive_move_count=None,
        top_1_positive_move_share_pct=None,
        top_2_positive_move_share_pct=None,
        max_bull_body_atr=None,
        max_range_atr=None,
        strongest_bull_upper_wick_share_pct=None,
        max_volume_zscore=None,
        volume_zscore_samples=0,
        volume_status="unavailable",
        last_bear_body_atr=None,
        returned_pump_share_pct=None,
        blow_off=None,
        strong_reversal=None,
        bucket=None,
        error=error,
    )


def derive_candle_anomaly_features(
    decision: ReplayDecision,
    candles: tuple[Candle, ...],
) -> CandleAnomalyFeatures:
    """Derive HYP-005 features from candles fully closed by decision time."""
    start_ms, end_ms = feature_window_bounds(decision)
    required_timestamps = tuple(range(start_ms, end_ms, TIMEFRAME_MS))
    required = set(required_timestamps)
    by_timestamp: dict[int, Candle] = {}
    for candle in candles:
        if candle.ts_ms not in required:
            continue
        if candle.ts_ms in by_timestamp:
            return _unresolved(
                end_ms=end_ms,
                error=f"duplicate required candle at {candle.ts_ms}",
            )
        by_timestamp[candle.ts_ms] = candle
    missing = next(
        (timestamp for timestamp in required_timestamps if timestamp not in by_timestamp),
        None,
    )
    if missing is not None:
        return _unresolved(
            end_ms=end_ms,
            error=f"missing required candle at {missing}",
        )
    ordered = tuple(by_timestamp[timestamp] for timestamp in required_timestamps)
    invalid = next((candle for candle in ordered if not _valid_price_candle(candle)), None)
    if invalid is not None:
        return _unresolved(
            end_ms=end_ms,
            error=f"invalid required candle at {invalid.ts_ms}",
        )

    true_ranges = tuple(
        _true_range(ordered[index], ordered[index - 1].close) for index in range(1, len(ordered))
    )
    formation = ordered[WARMUP_BARS:]
    formation_start_close = ordered[WARMUP_BARS - 1].close
    positive_moves: list[float] = []
    max_bull_body_atr = 0.0
    max_range_atr = 0.0
    strongest_bull_upper_wick_share_pct = 0.0
    max_volume_zscore: float | None = None
    volume_zscore_samples = 0
    last_bear_body_atr = 0.0

    for index in range(WARMUP_BARS, len(ordered)):
        candle = ordered[index]
        previous_close = ordered[index - 1].close
        close_return = math.log(candle.close / previous_close)
        if close_return > 0:
            positive_moves.append(close_return)

        prior_true_ranges = true_ranges[index - ATR_BARS - 1 : index - 1]
        prior_atr = fmean(prior_true_ranges)
        if prior_atr <= 0:
            return _unresolved(
                end_ms=end_ms,
                error=f"non-positive prior ATR before {candle.ts_ms}",
            )
        bull_body_atr = max(0.0, candle.close - candle.open) / prior_atr
        range_atr = (candle.high - candle.low) / prior_atr
        if bull_body_atr > max_bull_body_atr:
            max_bull_body_atr = bull_body_atr
            candle_range = candle.high - candle.low
            strongest_bull_upper_wick_share_pct = (
                (candle.high - max(candle.open, candle.close)) / candle_range * 100
                if candle_range > 0
                else 0.0
            )
        max_range_atr = max(max_range_atr, range_atr)
        if index == len(ordered) - 1:
            last_bear_body_atr = max(0.0, candle.open - candle.close) / prior_atr

        prior_volumes = tuple(item.volume for item in ordered[index - VOLUME_ZSCORE_BARS : index])
        if candle.volume is None or any(volume is None for volume in prior_volumes):
            continue
        complete_volumes = tuple(float(volume) for volume in prior_volumes if volume is not None)
        deviation = pstdev(complete_volumes)
        zscore = 0.0 if deviation == 0 else (candle.volume - fmean(complete_volumes)) / deviation
        max_volume_zscore = zscore if max_volume_zscore is None else max(max_volume_zscore, zscore)
        volume_zscore_samples += 1

    positive_total = sum(positive_moves)
    sorted_positive = sorted(positive_moves, reverse=True)
    top_1_share = sorted_positive[0] / positive_total * 100 if positive_total > 0 else None
    top_2_share = sum(sorted_positive[:2]) / positive_total * 100 if positive_total > 0 else None
    formation_peak_high = max(candle.high for candle in formation)
    runup = formation_peak_high - formation_start_close
    returned_share = (
        (formation_peak_high - formation[-1].close) / runup * 100 if runup > 0 else None
    )
    formation_return_pct = (
        (formation[-1].close - formation_start_close) / formation_start_close * 100
    )
    formation_peak_return_pct = runup / formation_start_close * 100
    volume_status: VolumeStatus
    if volume_zscore_samples == FORMATION_BARS:
        volume_status = "complete"
    elif volume_zscore_samples == 0:
        volume_status = "unavailable"
    else:
        volume_status = "partial"

    if top_2_share is None or returned_share is None:
        blow_off = None
        strong_reversal = None
        bucket = None
        status: FeatureStatus = "unclassified"
    else:
        blow_off = (
            top_2_share >= BLOW_OFF_TOP2_SHARE_PCT
            and max_bull_body_atr >= BLOW_OFF_MIN_BULL_BODY_ATR
        )
        strong_reversal = (
            last_bear_body_atr >= STRONG_REVERSAL_MIN_BEAR_BODY_ATR
            and returned_share >= STRONG_REVERSAL_MIN_RETURNED_SHARE_PCT
        )
        bucket = (
            f"{'blow_off' if blow_off else 'grind'}__"
            f"{'strong_reversal' if strong_reversal else 'weak_reversal'}"
        )
        status = "complete" if volume_status == "complete" else "partial_volume"

    return CandleAnomalyFeatures(
        version=CANDLE_ANOMALY_FEATURE_VERSION,
        status=status,
        feature_cutoff_at=datetime.fromtimestamp(end_ms / 1000, tz=UTC),
        formation_start_at=datetime.fromtimestamp(
            (end_ms - FORMATION_BARS * TIMEFRAME_MS) / 1000,
            tz=UTC,
        ),
        formation_end_at=datetime.fromtimestamp(end_ms / 1000, tz=UTC),
        formation_bars=FORMATION_BARS,
        warmup_bars=WARMUP_BARS,
        formation_return_pct=formation_return_pct,
        formation_peak_return_pct=formation_peak_return_pct,
        positive_move_count=len(positive_moves),
        top_1_positive_move_share_pct=top_1_share,
        top_2_positive_move_share_pct=top_2_share,
        max_bull_body_atr=max_bull_body_atr,
        max_range_atr=max_range_atr,
        strongest_bull_upper_wick_share_pct=strongest_bull_upper_wick_share_pct,
        max_volume_zscore=max_volume_zscore,
        volume_zscore_samples=volume_zscore_samples,
        volume_status=volume_status,
        last_bear_body_atr=last_bear_body_atr,
        returned_pump_share_pct=returned_share,
        blow_off=blow_off,
        strong_reversal=strong_reversal,
        bucket=bucket,
    )
