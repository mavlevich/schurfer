"""Pure baseline virtual-strategy simulation over complete OHLCV paths."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from .ohlcv import TIMEFRAME_MS, Candle, ceil_to_timeframe

if TYPE_CHECKING:
    from .replay import ReplayDecision, ReplayEpisode

VIRTUAL_STRATEGY_VERSION = "pump_short_v1_replay_v1"
ENTRY_MODEL_VERSION = "next_complete_5m_open_v1"
EXIT_MODEL_VERSION = "dynamic_short_bar_v1"
EXIT_POLICY_FAMILY_VERSION = "exit_policy_family_v1"
COST_MODEL_VERSION = "conservative_costs_v1"
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


@dataclass(frozen=True)
class CostParameters:
    """Pre-registered conservative costs expressed against position notional."""

    taker_fee_bps_per_side: float = 10.0
    funding_cost_bps_per_8h: float = 5.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.taker_fee_bps_per_side) or self.taker_fee_bps_per_side < 0:
            raise ValueError("taker fee must be finite and non-negative")
        if not math.isfinite(self.funding_cost_bps_per_8h) or self.funding_cost_bps_per_8h < 0:
            raise ValueError("funding cost must be finite and non-negative")


DEFAULT_COSTS = CostParameters()


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
) -> tuple[int, int]:
    params = exit_parameters(decision.pump_pct)
    start_ms = ceil_to_timeframe(int(decision.ts.timestamp() * 1000))
    return start_ms, start_ms + exit_policy.maximum_hold_minutes(params) * 60 * 1000


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


def _impact_bps(decision: ReplayDecision, side: Literal["bid", "ask"]) -> float | None:
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
) -> tuple[Candle, ...] | None:
    if entry_at_ms is None:
        start_ms, end_ms = expected_path_bounds(decision, exit_policy=exit_policy)
    else:
        start_ms = entry_at_ms
        params = exit_parameters(decision.pump_pct)
        end_ms = start_ms + exit_policy.maximum_hold_minutes(params) * 60 * 1000
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
) -> VirtualTrade:
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
    )
    if not path:
        return _unresolved(
            episode,
            selection,
            status="incomplete_market_path",
            error="missing one or more complete 5-minute bars",
        )
    position_usd = _position_usd(decision)
    bid_impact = _impact_bps(decision, "bid")
    ask_impact = _impact_bps(decision, "ask")
    if position_usd is None or bid_impact is None or ask_impact is None:
        return _unresolved(
            episode,
            selection,
            status="cost_inputs_unavailable",
            error="position size or decision-time bid/ask impact is unavailable",
        )

    params = exit_parameters(decision.pump_pct)
    entry_price = path[0].open
    stop_price = entry_price * (1 + params.initial_sl_pct / 100)
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
            stop_hit = candle.high >= stop_price
            activation_hit = candle.low <= activation_price
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

        if elapsed_end_minutes >= params.max_hold_min and not extension_active:
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

        if elapsed_end_minutes >= exit_policy.maximum_hold_minutes(params):
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
    gross_return_pct = (entry_price - exit_price) / entry_price * 100
    fee_cost_bps = costs.taker_fee_bps_per_side * 2
    funding_cost_bps = costs.funding_cost_bps_per_8h * duration_minutes / 480
    slippage_cost_bps = bid_impact + ask_impact
    total_cost_pct = (fee_cost_bps + funding_cost_bps + slippage_cost_bps) / 100
    net_return_pct = gross_return_pct - total_cost_pct
    mfe_pct = max(0.0, (entry_price - observed_low) / entry_price * 100)
    mae_pct = max(0.0, (observed_high - entry_price) / entry_price * 100)
    captured_move_pct = gross_return_pct / mfe_pct * 100 if mfe_pct > 0 else None

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
        classification=_classify(selection.taken, net_return_pct),
        exit_reason=exit_reason,
        ambiguity_resolution=ambiguity,
        entry_at=datetime.fromtimestamp(entry_at_ms / 1000, tz=UTC),
        exit_at=datetime.fromtimestamp(exit_at_ms / 1000, tz=UTC),
        entry_price=entry_price,
        exit_price=exit_price,
        entry_delay_seconds=entry_at_ms / 1000 - decision.ts.timestamp(),
        duration_minutes=duration_minutes,
        position_usd=position_usd,
        gross_return_pct=gross_return_pct,
        net_return_pct=net_return_pct,
        gross_pnl_usd=position_usd * gross_return_pct / 100,
        net_pnl_usd=position_usd * net_return_pct / 100,
        fee_cost_bps=fee_cost_bps,
        funding_cost_bps=funding_cost_bps,
        slippage_cost_bps=slippage_cost_bps,
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
    )
