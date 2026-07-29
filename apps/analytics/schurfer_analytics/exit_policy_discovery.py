"""Matched tradeable-cohort exit discovery with fixed-dollar-risk stop sizing."""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Literal

from .decision_quality import MARKET_QUALITY_CONTROL_POLICY, select_score_policy
from .ohlcv import TIMEFRAME_MS, Candle, ceil_to_timeframe
from .virtual_strategy import (
    BASELINE_EXIT_POLICY,
    BREAKEVEN_EXIT_POLICY,
    COMBINED_EXIT_POLICY,
    DEFAULT_COSTS,
    NO_PROGRESS_EXIT_POLICY,
    RECENT_PROGRESS_EXTENSION_EXIT_POLICY,
    CostParameters,
    ExitPolicy,
    MarketPath,
    VirtualTrade,
    exit_parameters,
    exit_policy_family_path_bounds,
    simulate_decision,
)

if TYPE_CHECKING:
    from .replay import ReplayDataset, ReplayDecision, ReplayEpisode
    from .virtual_market import DecisionMarketPath

EXIT_DISCOVERY_CORE_VERSION = "matched_tradeable_exit_discovery_v1"
EXIT_DISCOVERY_SELECTION_VERSION = "score_any_market_quality_first_crossing_v1"
EXIT_DISCOVERY_RISK_MODEL_VERSION = "fixed_dollar_initial_stop_risk_v1"
EXIT_DISCOVERY_ATR_VERSION = "prior_14_true_range_mean_v1"
EXIT_DISCOVERY_ATR_BARS = 14
EXIT_DISCOVERY_ATR_MULTIPLIER = 3.0
EXIT_DISCOVERY_WIDER_STOP_MULTIPLIER = 1.5
EXIT_DISCOVERY_ATR_MAX_BASELINE_MULTIPLIER = 2.0
EXIT_DISCOVERY_SIMPLE_LEVERAGE = 3.0

StopMode = Literal["baseline", "baseline_multiplier", "prior_atr"]


@dataclass(frozen=True)
class ExitDiscoveryVariant:
    key: str
    version: str
    exit_policy: ExitPolicy
    stop_mode: StopMode = "baseline"
    stop_multiplier: float = 1.0
    atr_multiplier: float | None = None
    max_baseline_multiplier: float = 1.0

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.version.strip():
            raise ValueError("exit discovery variant key and version must not be empty")
        if self.stop_mode not in {"baseline", "baseline_multiplier", "prior_atr"}:
            raise ValueError("unsupported exit discovery stop mode")
        numeric = (
            self.stop_multiplier,
            self.max_baseline_multiplier,
        )
        if any(not math.isfinite(value) or value < 1 for value in numeric):
            raise ValueError("exit discovery stop multipliers must be finite and at least one")
        if self.stop_mode == "prior_atr":
            if self.atr_multiplier is None or not math.isfinite(self.atr_multiplier):
                raise ValueError("prior-ATR stop requires a finite ATR multiplier")
            if self.atr_multiplier <= 0:
                raise ValueError("prior-ATR multiplier must be positive")
        elif self.atr_multiplier is not None:
            raise ValueError("ATR multiplier is only valid for the prior-ATR stop")


def _policy_variant(policy: ExitPolicy) -> ExitDiscoveryVariant:
    return ExitDiscoveryVariant(
        key=policy.key,
        version=f"discovery_{policy.version}",
        exit_policy=policy,
    )


BASELINE_EXIT_DISCOVERY_VARIANT = _policy_variant(BASELINE_EXIT_POLICY)
WIDER_STOP_EXIT_DISCOVERY_VARIANT = ExitDiscoveryVariant(
    key="wider_initial_stop_1_5x",
    version="wider_initial_stop_1_5x_fixed_risk_v1",
    exit_policy=BASELINE_EXIT_POLICY,
    stop_mode="baseline_multiplier",
    stop_multiplier=EXIT_DISCOVERY_WIDER_STOP_MULTIPLIER,
    max_baseline_multiplier=EXIT_DISCOVERY_WIDER_STOP_MULTIPLIER,
)
ATR_STOP_EXIT_DISCOVERY_VARIANT = ExitDiscoveryVariant(
    key="prior_atr_initial_stop",
    version="prior_atr_3x_clamped_1x_2x_fixed_risk_v1",
    exit_policy=BASELINE_EXIT_POLICY,
    stop_mode="prior_atr",
    atr_multiplier=EXIT_DISCOVERY_ATR_MULTIPLIER,
    max_baseline_multiplier=EXIT_DISCOVERY_ATR_MAX_BASELINE_MULTIPLIER,
)
EXIT_DISCOVERY_VARIANTS = (
    BASELINE_EXIT_DISCOVERY_VARIANT,
    _policy_variant(BREAKEVEN_EXIT_POLICY),
    _policy_variant(NO_PROGRESS_EXIT_POLICY),
    _policy_variant(COMBINED_EXIT_POLICY),
    _policy_variant(RECENT_PROGRESS_EXTENSION_EXIT_POLICY),
    WIDER_STOP_EXIT_DISCOVERY_VARIANT,
    ATR_STOP_EXIT_DISCOVERY_VARIANT,
)


@dataclass(frozen=True)
class ExitDiscoveryResult:
    pump_event_id: int
    cluster_key: str
    base: str
    variant_key: str
    status: str
    selected_decision_id: str | None
    exchange: str | None
    baseline_initial_sl_pct: float | None
    effective_initial_sl_pct: float | None
    prior_atr_pct: float | None
    position_scale: float | None
    simple_3x_liquidation_buffer_pct: float | None
    risk_normalized_net_return_pct: float | None
    trade: VirtualTrade | None
    error: str | None = None


def exit_discovery_path_bounds(decision: ReplayDecision) -> tuple[int, int]:
    """Return prior-only ATR warm-up plus the longest registered exit window."""
    entry_at_ms, end_ms = exit_policy_family_path_bounds(decision)
    warmup_bars = EXIT_DISCOVERY_ATR_BARS + 1
    return entry_at_ms - warmup_bars * TIMEFRAME_MS, end_ms


def _valid_candle(candle: Candle) -> bool:
    values = (candle.open, candle.high, candle.low, candle.close)
    return (
        all(math.isfinite(value) and value > 0 for value in values)
        and candle.high >= max(candle.open, candle.close, candle.low)
        and candle.low <= min(candle.open, candle.close, candle.high)
    )


def _required_candles(
    decision: ReplayDecision,
    path: MarketPath,
) -> tuple[tuple[Candle, ...] | None, str | None]:
    if path.status != "complete":
        return None, path.error or path.status
    start_ms, end_ms = exit_discovery_path_bounds(decision)
    timestamps = [candle.ts_ms for candle in path.candles]
    if len(timestamps) != len(set(timestamps)):
        return None, "duplicate candle timestamp in exit-discovery path"
    by_timestamp = {candle.ts_ms: candle for candle in path.candles}
    expected = tuple(range(start_ms, end_ms, TIMEFRAME_MS))
    candles = tuple(by_timestamp.get(timestamp) for timestamp in expected)
    if any(candle is None for candle in candles):
        return None, "missing one or more bars in the exit-discovery path"
    complete = tuple(candle for candle in candles if candle is not None)
    if any(not _valid_candle(candle) for candle in complete):
        return None, "invalid OHLC in the exit-discovery path"
    return complete, None


def prior_atr_pct(
    decision: ReplayDecision,
    path: MarketPath,
) -> tuple[float | None, str | None]:
    """Calculate 14 prior true ranges without reading the entry or future bars."""
    candles, error = _required_candles(decision, path)
    if error is not None or candles is None:
        return None, error or "exit-discovery path unavailable"
    entry_at_ms = ceil_to_timeframe(int(decision.ts.timestamp() * 1000))
    prior = tuple(candle for candle in candles if candle.ts_ms < entry_at_ms)
    if len(prior) != EXIT_DISCOVERY_ATR_BARS + 1:
        return None, "prior ATR requires exactly 15 complete pre-entry bars"
    ranges = tuple(
        max(
            candle.high - candle.low,
            abs(candle.high - previous.close),
            abs(candle.low - previous.close),
        )
        for previous, candle in pairwise(prior)
    )
    if len(ranges) != EXIT_DISCOVERY_ATR_BARS:
        return None, "prior ATR range count mismatch"
    entry = next((candle for candle in candles if candle.ts_ms == entry_at_ms), None)
    if entry is None:
        return None, "entry candle missing from exit-discovery path"
    atr = sum(ranges) / len(ranges)
    atr_pct = atr / entry.open * 100
    if not math.isfinite(atr_pct) or atr_pct <= 0:
        return None, "prior ATR must be finite and positive"
    return atr_pct, None


def effective_initial_stop_pct(
    decision: ReplayDecision,
    variant: ExitDiscoveryVariant,
    *,
    prior_atr_value_pct: float,
) -> tuple[float, float]:
    """Return effective stop and fixed-dollar-risk position scale."""
    baseline = exit_parameters(decision.pump_pct).initial_sl_pct
    if variant.stop_mode == "baseline":
        effective = baseline
    elif variant.stop_mode == "baseline_multiplier":
        effective = baseline * variant.stop_multiplier
    else:
        if variant.atr_multiplier is None:
            raise RuntimeError("prior-ATR variant lost its multiplier")
        effective = max(
            baseline,
            min(
                baseline * variant.max_baseline_multiplier,
                prior_atr_value_pct * variant.atr_multiplier,
            ),
        )
    return effective, baseline / effective


def _missing_path(episode: ReplayEpisode, decision: ReplayDecision) -> MarketPath:
    return MarketPath(
        pump_event_id=episode.pump_event_id,
        exchange=decision.exchange,
        base=decision.base,
        status="missing_path",
        candles=(),
        error="market path was not loaded",
    )


def _selection_failure(
    episode: ReplayEpisode,
    variant: ExitDiscoveryVariant,
    *,
    status: str,
    error: str,
) -> ExitDiscoveryResult:
    return ExitDiscoveryResult(
        pump_event_id=episode.pump_event_id,
        cluster_key=episode.cluster_key,
        base=episode.base,
        variant_key=variant.key,
        status=status,
        selected_decision_id=None,
        exchange=None,
        baseline_initial_sl_pct=None,
        effective_initial_sl_pct=None,
        prior_atr_pct=None,
        position_scale=None,
        simple_3x_liquidation_buffer_pct=None,
        risk_normalized_net_return_pct=None,
        trade=None,
        error=error,
    )


def build_exit_discovery_results(
    dataset: ReplayDataset,
    paths: tuple[DecisionMarketPath, ...],
    *,
    costs: CostParameters = DEFAULT_COSTS,
) -> tuple[ExitDiscoveryResult, ...]:
    """Replay every exit variant on one matched market-quality control cohort."""
    path_by_decision = {item.decision_id: item.path for item in paths}
    results: list[ExitDiscoveryResult] = []
    for episode in dataset.eligible_episodes:
        selection = select_score_policy(episode, MARKET_QUALITY_CONTROL_POLICY)
        decision = selection.decision
        if selection.status != "selected" or decision is None:
            error = selection.error or "market-quality control did not trigger"
            results.extend(
                _selection_failure(
                    episode,
                    variant,
                    status=f"selection_{selection.status}",
                    error=error,
                )
                for variant in EXIT_DISCOVERY_VARIANTS
            )
            continue
        path = path_by_decision.get(decision.decision_id or "")
        if path is None:
            path = _missing_path(episode, decision)
        atr_pct, path_error = prior_atr_pct(decision, path)
        if path_error is not None or atr_pct is None:
            results.extend(
                ExitDiscoveryResult(
                    pump_event_id=episode.pump_event_id,
                    cluster_key=episode.cluster_key,
                    base=episode.base,
                    variant_key=variant.key,
                    status="path_unavailable",
                    selected_decision_id=decision.decision_id,
                    exchange=decision.exchange,
                    baseline_initial_sl_pct=None,
                    effective_initial_sl_pct=None,
                    prior_atr_pct=None,
                    position_scale=None,
                    simple_3x_liquidation_buffer_pct=None,
                    risk_normalized_net_return_pct=None,
                    trade=None,
                    error=path_error or "exit-discovery path unavailable",
                )
                for variant in EXIT_DISCOVERY_VARIANTS
            )
            continue
        baseline_stop = exit_parameters(decision.pump_pct).initial_sl_pct
        for variant in EXIT_DISCOVERY_VARIANTS:
            effective_stop, position_scale = effective_initial_stop_pct(
                decision,
                variant,
                prior_atr_value_pct=atr_pct,
            )
            trade = simulate_decision(
                episode,
                path,
                decision,
                selection_reason=f"exit_discovery:{variant.key}",
                costs=costs,
                exit_policy=variant.exit_policy,
                initial_sl_pct_override=effective_stop,
                position_usd_scale=position_scale,
            )
            risk_normalized_return = (
                trade.net_return_pct * position_scale
                if trade.status == "complete" and trade.net_return_pct is not None
                else None
            )
            results.append(
                ExitDiscoveryResult(
                    pump_event_id=episode.pump_event_id,
                    cluster_key=episode.cluster_key,
                    base=episode.base,
                    variant_key=variant.key,
                    status=trade.status,
                    selected_decision_id=decision.decision_id,
                    exchange=decision.exchange,
                    baseline_initial_sl_pct=baseline_stop,
                    effective_initial_sl_pct=effective_stop,
                    prior_atr_pct=atr_pct,
                    position_scale=position_scale,
                    simple_3x_liquidation_buffer_pct=(
                        100 / EXIT_DISCOVERY_SIMPLE_LEVERAGE - effective_stop
                    ),
                    risk_normalized_net_return_pct=risk_normalized_return,
                    trade=trade,
                    error=trade.error,
                )
            )
    return tuple(results)
