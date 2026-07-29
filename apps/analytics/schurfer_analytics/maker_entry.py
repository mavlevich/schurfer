"""Look-ahead-safe maker-entry upper-bound over recorded exact-venue candles."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .decision_quality import MARKET_QUALITY_CONTROL_POLICY, select_score_policy
from .ohlcv import ONE_MINUTE_MS, TIMEFRAME_MS, Candle, next_timeframe_after
from .virtual_market import MAKER_FILL_TIMEOUT_MINUTES, MakerDecisionPaths, maker_path_bounds
from .virtual_strategy import (
    DEFAULT_COSTS,
    CostParameters,
    MarketPath,
    VirtualTrade,
    decision_impact_bps,
    simulate_decision,
    simulate_decision_at_entry_price,
)

if TYPE_CHECKING:
    from .replay import ReplayDecision, ReplayEpisode

MAKER_ENTRY_MODEL_VERSION = "recorded_best_ask_potential_fill_v1"
MAKER_SELECTION_VERSION = "first_market_quality_eligible_score_any_v1"
MAKER_FILL_EVIDENCE_VERSION = "activation_aware_post_decision_bar_cross_v2"
MAKER_COST_MODEL_VERSION = "zero_slippage_maker_entry_taker_exit_v1"
MAKER_SAME_RESOLUTION_TAKER_VERSION = "first_complete_1m_open_taker_control_v1"
MAKER_ENTRY_FEE_BPS = 0.0

MakerStatus = Literal[
    "complete",
    "cash_unfilled",
    "not_triggered",
    "selection_unresolved",
    "path_unavailable",
    "input_unavailable",
]


@dataclass(frozen=True)
class MakerEntryResult:
    pump_event_id: int
    cluster_key: str
    base: str
    status: MakerStatus
    selected_decision_id: str | None
    exchange: str | None
    path_timeframe: str | None
    limit_price: float | None
    fill_bar_at_ms: int | None
    fill_bar_index: int | None
    fill_delay_minutes: float | None
    fill_evidence: str | None
    baseline_trade: VirtualTrade | None
    taker_1m_trade: VirtualTrade | None
    maker_trade: VirtualTrade | None
    episode_net_return_pct: float | None
    missed_baseline_winner: bool
    adverse_selection_stop_30m: bool
    error: str | None = None


def recorded_best_ask(decision: ReplayDecision) -> float | None:
    liquidity = decision.liquidity
    if not isinstance(liquidity, dict) or liquidity.get("status") != "sampled":
        return None
    raw = liquidity.get("best_ask")
    if raw is None:
        return None
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _complete_window(
    path: MarketPath,
    decision: ReplayDecision,
    timeframe_ms: int,
) -> bool:
    if path.status != "complete":
        return False
    start_ms, end_ms = maker_path_bounds(decision, timeframe_ms)
    by_timestamp = {candle.ts_ms for candle in path.candles}
    return all(timestamp in by_timestamp for timestamp in range(start_ms, end_ms, timeframe_ms))


def choose_maker_path(
    paths: MakerDecisionPaths,
    decision: ReplayDecision,
) -> tuple[MarketPath | None, str | None, int | None, str | None]:
    if _complete_window(paths.one_minute, decision, ONE_MINUTE_MS):
        return paths.one_minute, "1m_primary", ONE_MINUTE_MS, None
    if _complete_window(paths.five_minute, decision, TIMEFRAME_MS):
        return paths.five_minute, "5m_fallback", TIMEFRAME_MS, None
    errors = (
        f"1m={paths.one_minute.error or paths.one_minute.status}; "
        f"5m={paths.five_minute.error or paths.five_minute.status}"
    )
    return None, None, None, errors


def potential_fill(
    candles: tuple[Candle, ...],
    decision: ReplayDecision,
    *,
    limit_price: float,
    timeframe_ms: int,
) -> tuple[Candle | None, str | None, int | None]:
    """Find the first potential fill without consulting the decision candle."""
    decision_ms = int(decision.ts.timestamp() * 1000)
    start_ms = next_timeframe_after(decision_ms, timeframe_ms)
    end_ms = start_ms + MAKER_FILL_TIMEOUT_MINUTES * 60 * 1000
    for candle in candles:
        if candle.ts_ms < start_ms or candle.ts_ms >= end_ms:
            continue
        if candle.high >= limit_price:
            if candle.open >= limit_price:
                evidence = (
                    "marketable_on_activation"
                    if candle.ts_ms == start_ms
                    else "crossed_between_bars"
                )
            else:
                evidence = "crossed_intrabar" if candle.high > limit_price else "touched_only"
            return candle, evidence, (candle.ts_ms - start_ms) // timeframe_ms
    return None, None, None


def _empty_result(
    episode: ReplayEpisode,
    *,
    status: MakerStatus,
    decision: ReplayDecision | None = None,
    baseline_trade: VirtualTrade | None = None,
    episode_net_return_pct: float | None = None,
    error: str | None = None,
) -> MakerEntryResult:
    return MakerEntryResult(
        pump_event_id=episode.pump_event_id,
        cluster_key=episode.cluster_key,
        base=episode.base,
        status=status,
        selected_decision_id=decision.decision_id if decision else None,
        exchange=decision.exchange if decision else None,
        path_timeframe=None,
        limit_price=None,
        fill_bar_at_ms=None,
        fill_bar_index=None,
        fill_delay_minutes=None,
        fill_evidence=None,
        baseline_trade=baseline_trade,
        taker_1m_trade=None,
        maker_trade=None,
        episode_net_return_pct=episode_net_return_pct,
        missed_baseline_winner=False,
        adverse_selection_stop_30m=False,
        error=error,
    )


def evaluate_maker_entry(
    episode: ReplayEpisode,
    paths: MakerDecisionPaths | None,
    *,
    costs: CostParameters = DEFAULT_COSTS,
    maker_entry_fee_bps: float = MAKER_ENTRY_FEE_BPS,
) -> MakerEntryResult:
    """Evaluate one discovery episode with cash for an unfilled maker order."""
    selection = select_score_policy(episode, MARKET_QUALITY_CONTROL_POLICY)
    if selection.status == "not_triggered":
        return _empty_result(episode, status="not_triggered", episode_net_return_pct=0.0)
    decision = selection.decision
    if selection.status == "unresolved" or decision is None:
        return _empty_result(
            episode,
            status="selection_unresolved",
            error=selection.error or "selection unresolved",
        )
    if paths is None:
        return _empty_result(
            episode,
            status="path_unavailable",
            decision=decision,
            error="maker paths were not loaded",
        )
    if paths.decision_id != decision.decision_id:
        return _empty_result(
            episode,
            status="path_unavailable",
            decision=decision,
            error="maker paths do not match the selected decision",
        )

    baseline = simulate_decision(
        episode,
        paths.five_minute,
        decision,
        selection_reason="maker_local_taker_baseline_v1",
        costs=costs,
    )
    limit_price = recorded_best_ask(decision)
    if limit_price is None:
        return _empty_result(
            episode,
            status="input_unavailable",
            decision=decision,
            baseline_trade=baseline,
            error="recorded best ask is unavailable",
        )
    path, timeframe, timeframe_ms, error = choose_maker_path(paths, decision)
    if path is None or timeframe is None or timeframe_ms is None:
        return _empty_result(
            episode,
            status="path_unavailable",
            decision=decision,
            baseline_trade=baseline,
            error=error or "complete maker path is unavailable",
        )
    taker_1m: VirtualTrade | None = None
    if timeframe == "1m_primary":
        first_bar_ms = next_timeframe_after(
            int(decision.ts.timestamp() * 1000),
            timeframe_ms,
        )
        first_bar = next(
            (candle for candle in path.candles if candle.ts_ms == first_bar_ms),
            None,
        )
        entry_impact = decision_impact_bps(decision, "bid")
        if first_bar is not None and entry_impact is not None:
            taker_1m = simulate_decision_at_entry_price(
                episode,
                path,
                decision,
                entry_at_ms=first_bar_ms,
                entry_price=first_bar.open,
                timeframe_ms=timeframe_ms,
                selection_reason=MAKER_SAME_RESOLUTION_TAKER_VERSION,
                entry_slippage_bps=entry_impact,
                entry_fee_bps=costs.taker_fee_bps_per_side,
                exit_fee_bps=costs.taker_fee_bps_per_side,
                costs=costs,
            )
    fill_candle, evidence, fill_bar_index = potential_fill(
        path.candles,
        decision,
        limit_price=limit_price,
        timeframe_ms=timeframe_ms,
    )
    if fill_candle is None:
        return MakerEntryResult(
            pump_event_id=episode.pump_event_id,
            cluster_key=episode.cluster_key,
            base=episode.base,
            status="cash_unfilled",
            selected_decision_id=decision.decision_id,
            exchange=decision.exchange,
            path_timeframe=timeframe,
            limit_price=limit_price,
            fill_bar_at_ms=None,
            fill_bar_index=None,
            fill_delay_minutes=None,
            fill_evidence=None,
            baseline_trade=baseline,
            taker_1m_trade=taker_1m,
            maker_trade=None,
            episode_net_return_pct=0.0,
            missed_baseline_winner=bool(
                baseline.net_return_pct is not None and baseline.net_return_pct > 0
            ),
            adverse_selection_stop_30m=False,
        )

    entry_at_ms = fill_candle.ts_ms + timeframe_ms
    maker = simulate_decision_at_entry_price(
        episode,
        path,
        decision,
        entry_at_ms=entry_at_ms,
        entry_price=limit_price,
        timeframe_ms=timeframe_ms,
        selection_reason=MAKER_ENTRY_MODEL_VERSION,
        entry_slippage_bps=0.0,
        entry_fee_bps=maker_entry_fee_bps,
        exit_fee_bps=costs.taker_fee_bps_per_side,
        costs=costs,
    )
    if maker.status != "complete" or maker.net_return_pct is None:
        return MakerEntryResult(
            pump_event_id=episode.pump_event_id,
            cluster_key=episode.cluster_key,
            base=episode.base,
            status="path_unavailable",
            selected_decision_id=decision.decision_id,
            exchange=decision.exchange,
            path_timeframe=timeframe,
            limit_price=limit_price,
            fill_bar_at_ms=fill_candle.ts_ms,
            fill_bar_index=fill_bar_index,
            fill_delay_minutes=(
                fill_bar_index * timeframe_ms / 60_000 if fill_bar_index is not None else None
            ),
            fill_evidence=evidence,
            baseline_trade=baseline,
            taker_1m_trade=taker_1m,
            maker_trade=maker,
            episode_net_return_pct=None,
            missed_baseline_winner=False,
            adverse_selection_stop_30m=False,
            error=maker.error or maker.status,
        )
    adverse = bool(
        maker.exit_reason == "initial_sl"
        and maker.duration_minutes is not None
        and maker.duration_minutes <= 30
    )
    return MakerEntryResult(
        pump_event_id=episode.pump_event_id,
        cluster_key=episode.cluster_key,
        base=episode.base,
        status="complete",
        selected_decision_id=decision.decision_id,
        exchange=decision.exchange,
        path_timeframe=timeframe,
        limit_price=limit_price,
        fill_bar_at_ms=fill_candle.ts_ms,
        fill_bar_index=fill_bar_index,
        fill_delay_minutes=(
            fill_bar_index * timeframe_ms / 60_000 if fill_bar_index is not None else None
        ),
        fill_evidence=evidence,
        baseline_trade=baseline,
        taker_1m_trade=taker_1m,
        maker_trade=maker,
        episode_net_return_pct=maker.net_return_pct,
        missed_baseline_winner=False,
        adverse_selection_stop_30m=adverse,
    )
