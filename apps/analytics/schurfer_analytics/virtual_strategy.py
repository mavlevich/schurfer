"""Pure baseline virtual-strategy simulation over complete OHLCV paths."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from schurfer_performance import (
    COST_MODEL_VERSION as SHARED_COST_MODEL_VERSION,
)
from schurfer_performance import (
    DEFAULT_COSTS as SHARED_DEFAULT_COSTS,
)
from schurfer_performance import (
    CostParameters as SharedCostParameters,
)
from schurfer_performance import (
    calculate_performance,
)

from .ohlcv import TIMEFRAME_MS, Candle, ceil_to_timeframe

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .replay import ReplayDecision, ReplayEpisode

VIRTUAL_STRATEGY_VERSION = "pump_short_v1_replay_v1"
ENTRY_MODEL_VERSION = "next_complete_5m_open_v1"
EXIT_MODEL_VERSION = "dynamic_short_bar_v1"
EXIT_POLICY_FAMILY_VERSION = "exit_policy_family_v1"
EXIT_ABLATION_FAMILY_VERSION = "exit_mechanics_ablation_family_v1"
COST_MODEL_VERSION = SHARED_COST_MODEL_VERSION
# Explicit re-exports preserve the existing analytics import surface while the
# implementation lives in the shared package.
CostParameters = SharedCostParameters
DEFAULT_COSTS = SHARED_DEFAULT_COSTS
SELECTION_MODEL_VERSION = "recorded_open_else_first_decision_v1"
MARKET_PATH_VERSION = "ccxt_5m_exact_anchor_v1"

TAKEN_ACTIONS = frozenset({"opened", "opened_dry_run"})


@dataclass(frozen=True)
class ExitParameters:
    initial_sl_pct: float
    activation_pct: float
    trail_pct: float
    trail_tighten_pct: float
    tighten_after_min: int
    max_hold_min: int


@dataclass(frozen=True)
class ExitPolicy:
    key: str
    version: str
    protect_breakeven_after_activation: bool = False
    no_progress_minutes: int | None = None
    max_extension_minutes: int = 0
    minimum_progress_pct: float = 0.0
    recent_progress_lookback_minutes: int | None = None
    extension_trail_pct: float | None = None

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.version.strip():
            raise ValueError("exit policy key and version must not be empty")
        optional_minutes = (
            self.no_progress_minutes,
            self.recent_progress_lookback_minutes,
        )
        if (
            any(value is not None and (value <= 0 or value % 5 != 0) for value in optional_minutes)
            or self.max_extension_minutes < 0
            or self.max_extension_minutes % 5 != 0
        ):
            raise ValueError("exit policy durations must be non-negative five-minute multiples")
        if self.no_progress_minutes is not None and self.max_extension_minutes == 0:
            raise ValueError("no-progress policy requires a bounded extension")
        if not math.isfinite(self.minimum_progress_pct) or self.minimum_progress_pct < 0:
            raise ValueError("minimum progress must be finite and non-negative")
        if (
            self.no_progress_minutes is not None
            or self.recent_progress_lookback_minutes is not None
        ) and self.minimum_progress_pct <= 0:
            raise ValueError("progress-aware policy requires a positive minimum progress")
        if (self.recent_progress_lookback_minutes is None) != (self.extension_trail_pct is None):
            raise ValueError("recent-progress extension requires both lookback and trail")
        if self.recent_progress_lookback_minutes is not None and self.max_extension_minutes == 0:
            raise ValueError("recent-progress policy requires a bounded extension")
        if self.extension_trail_pct is not None and (
            not math.isfinite(self.extension_trail_pct) or self.extension_trail_pct <= 0
        ):
            raise ValueError("extension trail must be finite and positive")

    def maximum_hold_minutes(self, params: ExitParameters) -> int:
        return params.max_hold_min + self.max_extension_minutes


@dataclass(frozen=True)
class ExitMechanics:
    """Bounded diagnostic switches around the shared production exit engine.

    These variants are discovery-only. The default keeps the production behavior
    byte-for-byte equivalent while matched-cohort diagnostics can disable one
    mechanism at a time without copying the candle simulator.
    """

    key: str
    version: str
    initial_stop_enabled: bool = True
    trailing_enabled: bool = True
    fixed_hold_minutes: int | None = None

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.version.strip():
            raise ValueError("exit mechanics key and version must not be empty")
        if self.fixed_hold_minutes is not None and (
            self.fixed_hold_minutes <= 0 or self.fixed_hold_minutes % 5 != 0
        ):
            raise ValueError("fixed hold must be a positive five-minute multiple")

    def baseline_hold_minutes(self, params: ExitParameters) -> int:
        return self.fixed_hold_minutes or params.max_hold_min

    def maximum_hold_minutes(
        self,
        params: ExitParameters,
        exit_policy: ExitPolicy,
    ) -> int:
        return self.baseline_hold_minutes(params) + exit_policy.max_extension_minutes


BASELINE_EXIT_POLICY = ExitPolicy(
    key="baseline",
    version="production_max_hold_v1",
)
BREAKEVEN_EXIT_POLICY = ExitPolicy(
    key="breakeven_after_activation",
    version="breakeven_after_activation_v1",
    protect_breakeven_after_activation=True,
)
NO_PROGRESS_EXIT_POLICY = ExitPolicy(
    key="no_progress_60m",
    version="no_progress_60m_step_0_5_extension_120m_v1",
    no_progress_minutes=60,
    max_extension_minutes=120,
    minimum_progress_pct=0.5,
)
COMBINED_EXIT_POLICY = ExitPolicy(
    key="breakeven_no_progress_60m",
    version="breakeven_no_progress_60m_step_0_5_extension_120m_v1",
    protect_breakeven_after_activation=True,
    no_progress_minutes=60,
    max_extension_minutes=120,
    minimum_progress_pct=0.5,
)
RECENT_PROGRESS_EXTENSION_EXIT_POLICY = ExitPolicy(
    key="recent_progress_extension",
    version="recent_progress_30m_step_0_5_extension_60m_trail_5_v1",
    max_extension_minutes=60,
    minimum_progress_pct=0.5,
    recent_progress_lookback_minutes=30,
    extension_trail_pct=5.0,
)
EXIT_POLICIES = (
    BASELINE_EXIT_POLICY,
    BREAKEVEN_EXIT_POLICY,
    NO_PROGRESS_EXIT_POLICY,
    COMBINED_EXIT_POLICY,
    RECENT_PROGRESS_EXTENSION_EXIT_POLICY,
)

BASELINE_EXIT_MECHANICS = ExitMechanics(
    key="full_v1",
    version="full_v1_exit_mechanics_v1",
)
MAX_HOLD_ONLY_EXIT_MECHANICS = ExitMechanics(
    key="max_hold_only",
    version="max_hold_only_v1",
    initial_stop_enabled=False,
    trailing_enabled=False,
)
INITIAL_SL_MAX_HOLD_EXIT_MECHANICS = ExitMechanics(
    key="initial_sl_max_hold",
    version="initial_sl_max_hold_v1",
    trailing_enabled=False,
)
FIXED_240_ONLY_EXIT_MECHANICS = ExitMechanics(
    key="fixed_240_only",
    version="fixed_240_only_v1",
    initial_stop_enabled=False,
    trailing_enabled=False,
    fixed_hold_minutes=240,
)
ECONOMICS_EXIT_MECHANICS = (
    BASELINE_EXIT_MECHANICS,
    MAX_HOLD_ONLY_EXIT_MECHANICS,
    INITIAL_SL_MAX_HOLD_EXIT_MECHANICS,
    FIXED_240_ONLY_EXIT_MECHANICS,
)


@dataclass(frozen=True)
class EpisodeSelection:
    decision: ReplayDecision
    taken: bool
    selection_reason: str


@dataclass(frozen=True)
class MarketPath:
    pump_event_id: int
    exchange: str
    base: str
    status: str
    candles: tuple[Candle, ...]
    error: str | None = None


@dataclass(frozen=True)
class VirtualTrade:
    pump_event_id: int
    cluster_key: str
    base: str
    exchange: str
    decision_id: str
    decision_at: datetime
    taken: bool
    selection_reason: str
    status: str
    classification: str
    exit_reason: str | None
    ambiguity_resolution: str | None
    entry_at: datetime | None
    exit_at: datetime | None
    entry_price: float | None
    exit_price: float | None
    entry_delay_seconds: float | None
    duration_minutes: float | None
    position_usd: float | None
    gross_return_pct: float | None
    net_return_pct: float | None
    gross_pnl_usd: float | None
    net_pnl_usd: float | None
    fee_cost_bps: float | None
    funding_cost_bps: float | None
    slippage_cost_bps: float | None
    mfe_pct: float | None
    mae_pct: float | None
    captured_move_pct: float | None
    error: str | None = None


def max_sequential_drawdown_usd(trades: Iterable[VirtualTrade]) -> float | None:
    """Return the chronological independent-trade P&L drawdown proxy."""
    ordered = sorted(
        (
            trade
            for trade in trades
            if trade.net_pnl_usd is not None and math.isfinite(trade.net_pnl_usd)
        ),
        key=lambda trade: (trade.decision_at, trade.pump_event_id),
    )
    if not ordered:
        return None
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for trade in ordered:
        equity += trade.net_pnl_usd or 0.0
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def exit_parameters(pump_pct: float | None) -> ExitParameters:
    """Mirror execution's three pump-magnitude exit bands."""
    magnitude = pump_pct if pump_pct is not None else 50.0
    if magnitude < 50:
        return ExitParameters(8.0, 8.0, 12.0, 8.0, 90, 180)
    if magnitude < 100:
        return ExitParameters(10.0, 12.0, 15.0, 10.0, 120, 240)
    return ExitParameters(12.0, 15.0, 20.0, 12.0, 180, 360)


def select_episode_decision(episode: ReplayEpisode) -> EpisodeSelection:
    """Select at most one point-in-time decision without consulting future outcomes."""
    opened = next(
        (decision for decision in episode.decisions if decision.action in TAKEN_ACTIONS),
        None,
    )
    if opened is not None:
        return EpisodeSelection(opened, True, "first_recorded_open")
    return EpisodeSelection(episode.decisions[0], False, "first_decision_counterfactual")


def expected_path_bounds(
    decision: ReplayDecision,
    *,
    exit_policy: ExitPolicy = BASELINE_EXIT_POLICY,
    exit_mechanics: ExitMechanics = BASELINE_EXIT_MECHANICS,
) -> tuple[int, int]:
    params = exit_parameters(decision.pump_pct)
    start_ms = ceil_to_timeframe(int(decision.ts.timestamp() * 1000))
    maximum_hold = exit_mechanics.maximum_hold_minutes(params, exit_policy)
    return start_ms, start_ms + maximum_hold * 60 * 1000


def economics_path_bounds(decision: ReplayDecision) -> tuple[int, int]:
    """Return the complete shared window required by every economics ablation."""
    starts_and_ends = tuple(
        expected_path_bounds(decision, exit_mechanics=mechanics)
        for mechanics in ECONOMICS_EXIT_MECHANICS
    )
    starts = {start for start, _ in starts_and_ends}
    if len(starts) != 1:
        raise RuntimeError("economics exit mechanics disagree on entry anchor")
    return starts_and_ends[0][0], max(end for _, end in starts_and_ends)


def exit_policy_family_path_bounds(decision: ReplayDecision) -> tuple[int, int]:
    """Return the full candle window required by every registered exit policy."""
    starts_and_ends = tuple(
        expected_path_bounds(decision, exit_policy=policy) for policy in EXIT_POLICIES
    )
    starts = {start for start, _ in starts_and_ends}
    if len(starts) != 1:
        raise RuntimeError("registered exit policies disagree on entry anchor")
    return starts_and_ends[0][0], max(end for _, end in starts_and_ends)


def market_path_fingerprint(paths: tuple[MarketPath, ...]) -> str:
    payload = []
    for path in sorted(paths, key=lambda item: item.pump_event_id):
        payload.append(
            {
                "pump_event_id": path.pump_event_id,
                "exchange": path.exchange,
                "base": path.base,
                "status": path.status,
                "error": path.error,
                "candles": [asdict(candle) for candle in path.candles],
            }
        )
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _finite_positive(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _position_usd(decision: ReplayDecision) -> float | None:
    if not isinstance(decision.features, dict):
        return None
    config = decision.features.get("config")
    if not isinstance(config, dict):
        return None
    return _finite_positive(config.get("signal_position_usd"))


def decision_impact_bps(
    decision: ReplayDecision,
    side: Literal["bid", "ask"],
) -> float | None:
    """Extract the recorded decision-time VWAP impact for the configured depth."""
    liquidity = decision.liquidity
    if not isinstance(liquidity, dict) or liquidity.get("status") != "sampled":
        return None
    quality = liquidity.get("quality")
    if not isinstance(quality, dict):
        return None
    target = _finite_positive(quality.get("depth_target_usd"))
    impacts = liquidity.get(f"{side}_impact_bps")
    if target is None or not isinstance(impacts, dict):
        return None
    key = f"{target:.2f}".rstrip("0").rstrip(".")
    value = impacts.get(key)
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _unresolved(
    episode: ReplayEpisode,
    selection: EpisodeSelection,
    *,
    status: str,
    error: str,
) -> VirtualTrade:
    decision = selection.decision
    return VirtualTrade(
        pump_event_id=episode.pump_event_id,
        cluster_key=episode.cluster_key,
        base=episode.base,
        exchange=decision.exchange,
        decision_id=decision.decision_id or "",
        decision_at=decision.ts,
        taken=selection.taken,
        selection_reason=selection.selection_reason,
        status=status,
        classification="unresolved",
        exit_reason=None,
        ambiguity_resolution=None,
        entry_at=None,
        exit_at=None,
        entry_price=None,
        exit_price=None,
        entry_delay_seconds=None,
        duration_minutes=None,
        position_usd=_position_usd(decision),
        gross_return_pct=None,
        net_return_pct=None,
        gross_pnl_usd=None,
        net_pnl_usd=None,
        fee_cost_bps=None,
        funding_cost_bps=None,
        slippage_cost_bps=None,
        mfe_pct=None,
        mae_pct=None,
        captured_move_pct=None,
        error=error,
    )


def _complete_path(
    decision: ReplayDecision,
    candles: tuple[Candle, ...],
    *,
    entry_at_ms: int | None = None,
    exit_policy: ExitPolicy = BASELINE_EXIT_POLICY,
    exit_mechanics: ExitMechanics = BASELINE_EXIT_MECHANICS,
) -> tuple[Candle, ...] | None:
    if entry_at_ms is None:
        start_ms, end_ms = expected_path_bounds(
            decision,
            exit_policy=exit_policy,
            exit_mechanics=exit_mechanics,
        )
    else:
        start_ms = entry_at_ms
        params = exit_parameters(decision.pump_pct)
        maximum_hold = exit_mechanics.maximum_hold_minutes(params, exit_policy)
        end_ms = start_ms + maximum_hold * 60 * 1000
    expected_count = (end_ms - start_ms) // TIMEFRAME_MS
    by_timestamp = {candle.ts_ms: candle for candle in candles}
    expected_timestamps = range(start_ms, end_ms, TIMEFRAME_MS)
    path = tuple(by_timestamp.get(timestamp) for timestamp in expected_timestamps)
    if len(path) != expected_count or any(candle is None for candle in path):
        return None
    complete = tuple(candle for candle in path if candle is not None)
    if any(
        not all(
            math.isfinite(value) and value > 0
            for value in (candle.open, candle.high, candle.low, candle.close)
        )
        or candle.high < max(candle.open, candle.close, candle.low)
        or candle.low > min(candle.open, candle.close, candle.high)
        for candle in complete
    ):
        return None
    return complete


def exit_policy_family_path_is_complete(
    decision: ReplayDecision,
    candles: tuple[Candle, ...],
) -> bool:
    """Fail the paired family closed unless its longest registered path is complete."""
    params = exit_parameters(decision.pump_pct)
    longest_policy = max(
        EXIT_POLICIES,
        key=lambda policy: policy.maximum_hold_minutes(params),
    )
    return _complete_path(decision, candles, exit_policy=longest_policy) is not None


def _classify(taken: bool, net_return_pct: float) -> str:
    won = net_return_pct > 0
    if taken:
        return "taken_won" if won else "taken_lost"
    return "skipped_would_have_won" if won else "skipped_correctly_avoided"


def _breakeven_stop_price(
    entry_price: float,
    costs: CostParameters,
    duration_minutes: float,
    bid_impact_bps: float,
    ask_impact_bps: float,
) -> float:
    funding_bps = costs.funding_cost_bps_per_8h * duration_minutes / 480
    total_cost_bps = (
        costs.taker_fee_bps_per_side * 2 + funding_bps + bid_impact_bps + ask_impact_bps
    )
    return entry_price * (1 - total_cost_bps / 10_000)


def _simulate_selected_entry(
    episode: ReplayEpisode,
    market_path: MarketPath,
    selection: EpisodeSelection,
    entry_at_ms: int,
    *,
    costs: CostParameters = DEFAULT_COSTS,
    exit_policy: ExitPolicy = BASELINE_EXIT_POLICY,
    exit_mechanics: ExitMechanics = BASELINE_EXIT_MECHANICS,
    initial_sl_pct_override: float | None = None,
    position_usd_scale: float = 1.0,
) -> VirtualTrade:
    if initial_sl_pct_override is not None and (
        not math.isfinite(initial_sl_pct_override) or initial_sl_pct_override <= 0
    ):
        raise ValueError("initial stop override must be finite and positive")
    if not math.isfinite(position_usd_scale) or not 0 < position_usd_scale <= 1:
        raise ValueError("position USD scale must be finite and in (0, 1]")
    decision = selection.decision
    if market_path.status != "complete":
        return _unresolved(
            episode,
            selection,
            status="market_path_unavailable",
            error=market_path.error or market_path.status,
        )
    if (
        market_path.pump_event_id != episode.pump_event_id
        or market_path.exchange != decision.exchange
        or market_path.base.casefold() != decision.base.casefold()
    ):
        return _unresolved(
            episode,
            selection,
            status="market_path_mismatch",
            error="market path does not match selected episode decision",
        )
    baseline_entry_ms = ceil_to_timeframe(int(decision.ts.timestamp() * 1000))
    if entry_at_ms < baseline_entry_ms or entry_at_ms % TIMEFRAME_MS != 0:
        return _unresolved(
            episode,
            selection,
            status="invalid_virtual_entry",
            error="entry must be an aligned bar at or after the baseline entry",
        )
    path = _complete_path(
        decision,
        market_path.candles,
        entry_at_ms=entry_at_ms,
        exit_policy=exit_policy,
        exit_mechanics=exit_mechanics,
    )
    if not path:
        return _unresolved(
            episode,
            selection,
            status="incomplete_market_path",
            error="missing one or more complete 5-minute bars",
        )
    recorded_position_usd = _position_usd(decision)
    bid_impact = decision_impact_bps(decision, "bid")
    ask_impact = decision_impact_bps(decision, "ask")
    if recorded_position_usd is None or bid_impact is None or ask_impact is None:
        return _unresolved(
            episode,
            selection,
            status="cost_inputs_unavailable",
            error="position size or decision-time bid/ask impact is unavailable",
        )
    position_usd = recorded_position_usd * position_usd_scale

    params = exit_parameters(decision.pump_pct)
    entry_price = path[0].open
    initial_sl_pct = initial_sl_pct_override or params.initial_sl_pct
    stop_price = entry_price * (1 + initial_sl_pct / 100)
    activation_price = entry_price * (1 - params.activation_pct / 100)
    best_price: float | None = None
    exit_price: float | None = None
    exit_at_ms: int | None = None
    exit_reason: str | None = None
    ambiguity: str | None = None
    observed_low = entry_price
    observed_high = entry_price
    favorable_price = entry_price
    last_progress_at_ms = entry_at_ms
    extension_active = False

    for candle in path:
        elapsed_minutes = (candle.ts_ms - entry_at_ms) / 60_000
        candle_end_ms = candle.ts_ms + TIMEFRAME_MS
        elapsed_end_minutes = (candle_end_ms - entry_at_ms) / 60_000
        trail_pct = (
            params.trail_tighten_pct
            if elapsed_minutes >= params.tighten_after_min
            else params.trail_pct
        )
        if extension_active and exit_policy.extension_trail_pct is not None:
            trail_pct = min(trail_pct, exit_policy.extension_trail_pct)
        if best_price is None:
            stop_hit = exit_mechanics.initial_stop_enabled and candle.high >= stop_price
            activation_hit = exit_mechanics.trailing_enabled and candle.low <= activation_price
            if stop_hit:
                observed_high = max(observed_high, stop_price)
                exit_price = stop_price
                exit_at_ms = candle.ts_ms + TIMEFRAME_MS
                exit_reason = "initial_sl"
                if activation_hit:
                    ambiguity = "conservative_stop_first"
                break
            if activation_hit:
                best_price = candle.low
                observed_low = min(observed_low, candle.low)
                trailing_price = best_price * (1 + trail_pct / 100)
                if exit_policy.protect_breakeven_after_activation:
                    trailing_price = min(
                        trailing_price,
                        _breakeven_stop_price(
                            entry_price,
                            costs,
                            elapsed_end_minutes,
                            bid_impact,
                            ask_impact,
                        ),
                    )
                if candle.high >= trailing_price:
                    observed_high = max(observed_high, trailing_price)
                    exit_price = trailing_price
                    exit_at_ms = candle.ts_ms + TIMEFRAME_MS
                    exit_reason = (
                        "protected_stop"
                        if exit_policy.protect_breakeven_after_activation
                        else "trailing_stop"
                    )
                    ambiguity = "conservative_stop_first"
                    break
        else:
            previous_trailing_price = best_price * (1 + trail_pct / 100)
            if exit_policy.protect_breakeven_after_activation:
                previous_trailing_price = min(
                    previous_trailing_price,
                    _breakeven_stop_price(
                        entry_price,
                        costs,
                        elapsed_end_minutes,
                        bid_impact,
                        ask_impact,
                    ),
                )
            if candle.high >= previous_trailing_price:
                observed_high = max(observed_high, previous_trailing_price)
                exit_price = previous_trailing_price
                exit_at_ms = candle_end_ms
                exit_reason = (
                    "protected_stop"
                    if exit_policy.protect_breakeven_after_activation
                    else "trailing_stop"
                )
                break
            if candle.low < best_price:
                best_price = candle.low
                observed_low = min(observed_low, candle.low)
                tightened_price = best_price * (1 + trail_pct / 100)
                if exit_policy.protect_breakeven_after_activation:
                    tightened_price = min(
                        tightened_price,
                        _breakeven_stop_price(
                            entry_price,
                            costs,
                            elapsed_end_minutes,
                            bid_impact,
                            ask_impact,
                        ),
                    )
                if candle.high >= tightened_price:
                    observed_high = max(observed_high, tightened_price)
                    exit_price = tightened_price
                    exit_at_ms = candle_end_ms
                    exit_reason = (
                        "protected_stop"
                        if exit_policy.protect_breakeven_after_activation
                        else "trailing_stop"
                    )
                    ambiguity = "conservative_stop_first"
                    break
        observed_low = min(observed_low, candle.low)
        observed_high = max(observed_high, candle.high)
        progress_threshold = favorable_price * (1 - exit_policy.minimum_progress_pct / 100)
        if candle.low < favorable_price and candle.low <= progress_threshold:
            favorable_price = candle.low
            last_progress_at_ms = candle_end_ms

        if (
            exit_policy.no_progress_minutes is not None
            and candle_end_ms - last_progress_at_ms >= exit_policy.no_progress_minutes * 60_000
        ):
            exit_price = candle.close
            exit_at_ms = candle_end_ms
            exit_reason = "no_progress"
            break

        baseline_hold_minutes = exit_mechanics.baseline_hold_minutes(params)
        if elapsed_end_minutes >= baseline_hold_minutes and not extension_active:
            lookback = exit_policy.recent_progress_lookback_minutes
            if lookback is not None:
                recently_improved = (
                    best_price is not None
                    and favorable_price < entry_price
                    and candle_end_ms - last_progress_at_ms <= lookback * 60_000
                )
                if recently_improved:
                    extension_active = True
                else:
                    exit_price = candle.close
                    exit_at_ms = candle_end_ms
                    exit_reason = "max_hold_no_recent_progress"
                    break
            elif exit_policy.max_extension_minutes == 0:
                exit_price = candle.close
                exit_at_ms = candle_end_ms
                exit_reason = "max_hold"
                break

        if elapsed_end_minutes >= exit_mechanics.maximum_hold_minutes(params, exit_policy):
            exit_price = candle.close
            exit_at_ms = candle_end_ms
            exit_reason = "absolute_max_hold"
            break

    if exit_price is None:
        exit_price = path[-1].close
        exit_at_ms = path[-1].ts_ms + TIMEFRAME_MS
        exit_reason = "absolute_max_hold"
    if exit_at_ms is None or exit_reason is None:
        raise RuntimeError("virtual exit invariant violated")

    duration_minutes = (exit_at_ms - entry_at_ms) / 60_000
    accounting = calculate_performance(
        position_usd=position_usd,
        entry_price=entry_price,
        exit_price=exit_price,
        side="short",
        duration_minutes=duration_minutes,
        entry_slippage_bps=bid_impact,
        exit_slippage_bps=ask_impact,
        costs=costs,
    )
    if accounting.net_return_pct is None or accounting.net_pnl_usd is None:
        raise RuntimeError("complete replay accounting unexpectedly incomplete")
    mfe_pct = max(0.0, (entry_price - observed_low) / entry_price * 100)
    mae_pct = max(0.0, (observed_high - entry_price) / entry_price * 100)
    captured_move_pct = accounting.gross_return_pct / mfe_pct * 100 if mfe_pct > 0 else None

    return VirtualTrade(
        pump_event_id=episode.pump_event_id,
        cluster_key=episode.cluster_key,
        base=episode.base,
        exchange=decision.exchange,
        decision_id=decision.decision_id or "",
        decision_at=decision.ts,
        taken=selection.taken,
        selection_reason=selection.selection_reason,
        status="complete",
        classification=_classify(selection.taken, accounting.net_return_pct),
        exit_reason=exit_reason,
        ambiguity_resolution=ambiguity,
        entry_at=datetime.fromtimestamp(entry_at_ms / 1000, tz=UTC),
        exit_at=datetime.fromtimestamp(exit_at_ms / 1000, tz=UTC),
        entry_price=entry_price,
        exit_price=exit_price,
        entry_delay_seconds=entry_at_ms / 1000 - decision.ts.timestamp(),
        duration_minutes=duration_minutes,
        position_usd=position_usd,
        gross_return_pct=accounting.gross_return_pct,
        net_return_pct=accounting.net_return_pct,
        gross_pnl_usd=accounting.gross_pnl_usd,
        net_pnl_usd=accounting.net_pnl_usd,
        fee_cost_bps=accounting.fee_cost_bps,
        funding_cost_bps=accounting.funding_cost_bps,
        slippage_cost_bps=accounting.slippage_cost_bps,
        mfe_pct=mfe_pct,
        mae_pct=mae_pct,
        captured_move_pct=captured_move_pct,
    )


def simulate_episode(
    episode: ReplayEpisode,
    market_path: MarketPath,
    *,
    costs: CostParameters = DEFAULT_COSTS,
    exit_policy: ExitPolicy = BASELINE_EXIT_POLICY,
    exit_mechanics: ExitMechanics = BASELINE_EXIT_MECHANICS,
) -> VirtualTrade:
    """Replay one baseline short using conservative within-bar ordering.

    Entry is the next complete 5-minute bar open. This avoids using the decision's
    still-forming candle. When activation and a stop can both occur in one bar, the
    adverse exit is assumed first and recorded explicitly.
    """
    selection = select_episode_decision(episode)
    entry_at_ms = ceil_to_timeframe(int(selection.decision.ts.timestamp() * 1000))
    return _simulate_selected_entry(
        episode,
        market_path,
        selection,
        entry_at_ms,
        costs=costs,
        exit_policy=exit_policy,
        exit_mechanics=exit_mechanics,
    )


def simulate_episode_at_entry(
    episode: ReplayEpisode,
    market_path: MarketPath,
    *,
    entry_at_ms: int,
    selection_reason: str,
    costs: CostParameters = DEFAULT_COSTS,
) -> VirtualTrade:
    """Replay the baseline-selected decision at an explicit point-in-time entry bar."""
    normalized_reason = selection_reason.strip()
    if not normalized_reason:
        raise ValueError("selection reason must not be empty")
    baseline = select_episode_decision(episode)
    selection = EpisodeSelection(
        decision=baseline.decision,
        taken=baseline.taken,
        selection_reason=normalized_reason,
    )
    return _simulate_selected_entry(
        episode,
        market_path,
        selection,
        entry_at_ms,
        costs=costs,
    )


def simulate_decision(
    episode: ReplayEpisode,
    market_path: MarketPath,
    decision: ReplayDecision,
    *,
    selection_reason: str,
    costs: CostParameters = DEFAULT_COSTS,
    exit_policy: ExitPolicy = BASELINE_EXIT_POLICY,
    exit_mechanics: ExitMechanics = BASELINE_EXIT_MECHANICS,
    initial_sl_pct_override: float | None = None,
    position_usd_scale: float = 1.0,
) -> VirtualTrade:
    """Replay one explicitly selected point-in-time decision.

    Threshold experiments can choose different decisions, and therefore different
    exact venues, inside the same pump episode. Keeping that selection outside the
    exit engine lets every experiment reuse the same entry, exit, and cost semantics.
    """
    normalized_reason = selection_reason.strip()
    if not normalized_reason:
        raise ValueError("selection reason must not be empty")
    if decision not in episode.decisions:
        raise ValueError("selected decision does not belong to the episode")
    selection = EpisodeSelection(
        decision=decision,
        taken=False,
        selection_reason=normalized_reason,
    )
    entry_at_ms = ceil_to_timeframe(int(decision.ts.timestamp() * 1000))
    return _simulate_selected_entry(
        episode,
        market_path,
        selection,
        entry_at_ms,
        costs=costs,
        exit_policy=exit_policy,
        exit_mechanics=exit_mechanics,
        initial_sl_pct_override=initial_sl_pct_override,
        position_usd_scale=position_usd_scale,
    )
