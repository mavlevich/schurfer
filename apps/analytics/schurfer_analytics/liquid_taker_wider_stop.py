"""Prospective liquid-taker and fixed-risk wider-stop shadow comparison."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .exit_policy_discovery import (
    EXIT_DISCOVERY_SIMPLE_LEVERAGE,
    EXIT_DISCOVERY_WIDER_STOP_MULTIPLIER,
)
from .liquid_taker_report import (
    LIQUID_TAKER_CANDIDATE_VERSION,
    LIQUID_TAKER_SELECTION_VERSION,
    LiquidTakerSelection,
    select_liquid_taker_decision,
)
from .virtual_strategy import (
    DEFAULT_COSTS,
    CostParameters,
    MarketPath,
    VirtualTrade,
    exit_parameters,
    simulate_decision,
)

if TYPE_CHECKING:
    from .replay import ReplayDataset, ReplayDecision, ReplayEpisode
    from .virtual_market import DecisionMarketPath

LIQUID_TAKER_WIDER_CORE_VERSION = "liquid_taker_wider_stop_core_v1"
LIQUID_TAKER_WIDER_SELECTION_VERSION = LIQUID_TAKER_SELECTION_VERSION
LIQUID_TAKER_WIDER_RISK_VERSION = "fixed_dollar_initial_stop_risk_v1"
LIQUID_TAKER_BASELINE_KEY = "liquid_taker_baseline"
LIQUID_TAKER_WIDER_KEY = "liquid_taker_wider_stop_1_5x"
LIQUID_TAKER_WIDER_VERSION = "liquid_taker_wider_stop_1_5x_fixed_risk_v1"
LIQUID_TAKER_WIDER_POSITION_SCALE = 1 / EXIT_DISCOVERY_WIDER_STOP_MULTIPLIER


@dataclass(frozen=True)
class LiquidTakerWiderResult:
    pump_event_id: int
    cluster_key: str
    base: str
    variant_key: str
    status: str
    selected_decision_id: str | None
    exchange: str | None
    bid_impact_bps: float | None
    ask_impact_bps: float | None
    round_trip_impact_bps: float | None
    measured_capacity_floor_usd: float | None
    baseline_initial_sl_pct: float | None
    effective_initial_sl_pct: float | None
    position_scale: float | None
    simple_3x_liquidation_buffer_pct: float | None
    risk_normalized_net_return_pct: float | None
    trade: VirtualTrade | None
    error: str | None = None


def _missing_path(episode: ReplayEpisode, decision: ReplayDecision) -> MarketPath:
    return MarketPath(
        pump_event_id=episode.pump_event_id,
        exchange=decision.exchange,
        base=decision.base,
        status="missing_path",
        candles=(),
        error="market path was not loaded",
    )


def _selection_result(
    episode: ReplayEpisode,
    selection: LiquidTakerSelection,
    *,
    variant_key: str,
) -> LiquidTakerWiderResult:
    status = "not_triggered" if selection.status == "not_triggered" else "selection_unresolved"
    return LiquidTakerWiderResult(
        pump_event_id=episode.pump_event_id,
        cluster_key=episode.cluster_key,
        base=episode.base,
        variant_key=variant_key,
        status=status,
        selected_decision_id=None,
        exchange=None,
        bid_impact_bps=None,
        ask_impact_bps=None,
        round_trip_impact_bps=None,
        measured_capacity_floor_usd=None,
        baseline_initial_sl_pct=None,
        effective_initial_sl_pct=None,
        position_scale=None,
        simple_3x_liquidation_buffer_pct=None,
        risk_normalized_net_return_pct=(0.0 if selection.status == "not_triggered" else None),
        trade=None,
        error=selection.error,
    )


def _trade_result(
    episode: ReplayEpisode,
    decision: ReplayDecision,
    selection: LiquidTakerSelection,
    path: MarketPath,
    *,
    variant_key: str,
    selection_reason: str,
    effective_initial_sl_pct: float,
    position_scale: float,
    costs: CostParameters,
) -> LiquidTakerWiderResult:
    trade = simulate_decision(
        episode,
        path,
        decision,
        selection_reason=selection_reason,
        costs=costs,
        initial_sl_pct_override=effective_initial_sl_pct,
        position_usd_scale=position_scale,
    )
    return LiquidTakerWiderResult(
        pump_event_id=episode.pump_event_id,
        cluster_key=episode.cluster_key,
        base=episode.base,
        variant_key=variant_key,
        status=trade.status,
        selected_decision_id=decision.decision_id,
        exchange=decision.exchange,
        bid_impact_bps=selection.bid_impact_bps,
        ask_impact_bps=selection.ask_impact_bps,
        round_trip_impact_bps=(
            selection.bid_impact_bps + selection.ask_impact_bps
            if selection.bid_impact_bps is not None and selection.ask_impact_bps is not None
            else None
        ),
        measured_capacity_floor_usd=selection.measured_capacity_floor_usd,
        baseline_initial_sl_pct=exit_parameters(decision.pump_pct).initial_sl_pct,
        effective_initial_sl_pct=effective_initial_sl_pct,
        position_scale=position_scale,
        simple_3x_liquidation_buffer_pct=(
            100 / EXIT_DISCOVERY_SIMPLE_LEVERAGE - effective_initial_sl_pct
        ),
        risk_normalized_net_return_pct=(
            trade.net_return_pct * position_scale
            if trade.status == "complete" and trade.net_return_pct is not None
            else None
        ),
        trade=trade,
        error=trade.error,
    )


def build_liquid_taker_wider_results(
    dataset: ReplayDataset,
    paths: tuple[DecisionMarketPath, ...],
    *,
    costs: CostParameters = DEFAULT_COSTS,
) -> tuple[LiquidTakerWiderResult, ...]:
    """Replay baseline and 1.5x stop on the exact same HYP-008 selection and path."""
    path_by_decision = {item.decision_id: item.path for item in paths}
    results: list[LiquidTakerWiderResult] = []
    for episode in dataset.eligible_episodes:
        selection = select_liquid_taker_decision(episode)
        decision = selection.decision
        if selection.status != "selected" or decision is None:
            results.extend(
                _selection_result(episode, selection, variant_key=variant_key)
                for variant_key in (
                    LIQUID_TAKER_BASELINE_KEY,
                    LIQUID_TAKER_WIDER_KEY,
                )
            )
            continue
        path = path_by_decision.get(decision.decision_id or "")
        if path is None:
            path = _missing_path(episode, decision)
        baseline_stop = exit_parameters(decision.pump_pct).initial_sl_pct
        results.append(
            _trade_result(
                episode,
                decision,
                selection,
                path,
                variant_key=LIQUID_TAKER_BASELINE_KEY,
                selection_reason=LIQUID_TAKER_CANDIDATE_VERSION,
                effective_initial_sl_pct=baseline_stop,
                position_scale=1.0,
                costs=costs,
            )
        )
        results.append(
            _trade_result(
                episode,
                decision,
                selection,
                path,
                variant_key=LIQUID_TAKER_WIDER_KEY,
                selection_reason=LIQUID_TAKER_WIDER_VERSION,
                effective_initial_sl_pct=(baseline_stop * EXIT_DISCOVERY_WIDER_STOP_MULTIPLIER),
                position_scale=LIQUID_TAKER_WIDER_POSITION_SCALE,
                costs=costs,
            )
        )
    return tuple(results)
