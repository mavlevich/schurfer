"""Matched policy economics and bounded exit-mechanics discovery diagnostics."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import fmean
from typing import TYPE_CHECKING, Protocol

from .decision_quality import SCORE_POLICIES, ScorePolicy, select_score_policy
from .virtual_strategy import (
    BASELINE_EXIT_MECHANICS,
    ECONOMICS_EXIT_MECHANICS,
    CostParameters,
    MarketPath,
    VirtualTrade,
    simulate_decision,
)

if TYPE_CHECKING:
    from .replay import ReplayDataset, ReplayEpisode

MATCHED_ECONOMICS_VERSION = "matched_policy_economics_v1"
ECONOMICS_POLICY_KEYS = ("score_any", "score_4", "score_6")


class EconomicsEpisodeResult(Protocol):
    pump_event_id: int
    cluster_key: str
    policy_key: str
    status: str
    selected_decision_id: str | None
    exchange: str | None
    spread_bps: float | None
    entry_impact_bps: float | None
    exit_impact_bps: float | None
    episode_net_return_pct: float | None
    trade: VirtualTrade | None


@dataclass(frozen=True)
class MatchedPolicyEconomics:
    policy_key: str
    episodes: int
    clusters: int
    selected: int
    cash: int
    mean_episode_gross_return_pct: float | None
    mean_entry_impact_bps: float | None
    mean_exit_impact_bps: float | None
    mean_fee_cost_bps: float | None
    mean_funding_cost_bps: float | None
    mean_total_cost_bps: float | None
    mean_episode_net_return_pct: float | None
    mean_trade_net_return_pct: float | None


@dataclass(frozen=True)
class LiquiditySegmentEconomics:
    policy_key: str
    dimension: str
    bucket: str
    trades: int
    clusters: int
    mean_gross_return_pct: float
    mean_entry_impact_bps: float
    mean_exit_impact_bps: float
    mean_fee_cost_bps: float
    mean_funding_cost_bps: float
    mean_total_cost_bps: float
    mean_net_return_pct: float


@dataclass(frozen=True)
class ExitAblationTrade:
    policy_key: str
    mechanics_key: str
    trade: VirtualTrade


@dataclass(frozen=True)
class ExitAblationMetrics:
    policy_key: str
    mechanics_key: str
    episodes: int
    clusters: int
    mean_net_return_pct: float | None
    win_rate_pct: float | None
    initial_stop_rate_pct: float | None
    mean_duration_minutes: float | None
    mean_mfe_pct: float | None
    mean_mae_pct: float | None


@dataclass(frozen=True)
class ExitMechanicsEffect:
    policy_key: str
    effect_key: str
    reference_key: str
    variant_key: str
    episodes: int
    mean_reference_net_return_pct: float | None
    mean_variant_net_return_pct: float | None
    mean_delta_pct: float | None
    improved_episodes: int
    worsened_episodes: int
    unchanged_episodes: int


@dataclass(frozen=True)
class InitialStopFollowThrough:
    policy_key: str
    initial_stop_exits: int
    fixed_240_resolved: int
    fixed_240_positive: int
    fixed_240_positive_rate_pct: float | None
    mean_fixed_240_net_return_pct: float | None
    mean_fixed_240_mae_pct: float | None


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def _economics_policies() -> tuple[ScorePolicy, ...]:
    by_key = {policy.key: policy for policy in SCORE_POLICIES}
    missing = [key for key in ECONOMICS_POLICY_KEYS if key not in by_key]
    if missing:
        raise RuntimeError(f"matched economics policies are missing: {missing}")
    return tuple(by_key[key] for key in ECONOMICS_POLICY_KEYS)


def _complete_trade(result: EconomicsEpisodeResult) -> VirtualTrade | None:
    trade = result.trade
    if trade is None or trade.status != "complete" or trade.net_return_pct is None:
        return None
    return trade


def matched_policy_economics(
    results: tuple[EconomicsEpisodeResult, ...],
) -> tuple[MatchedPolicyEconomics, ...]:
    """Compare policies on one completely resolved episode denominator."""
    target_results = tuple(
        result for result in results if result.policy_key in ECONOMICS_POLICY_KEYS
    )
    by_event_policy = {
        (result.pump_event_id, result.policy_key): result for result in target_results
    }
    event_ids = sorted({result.pump_event_id for result in target_results})
    matched_event_ids = tuple(
        event_id
        for event_id in event_ids
        if all(
            (result := by_event_policy.get((event_id, policy_key))) is not None
            and result.episode_net_return_pct is not None
            for policy_key in ECONOMICS_POLICY_KEYS
        )
    )
    rows: list[MatchedPolicyEconomics] = []
    for policy_key in ECONOMICS_POLICY_KEYS:
        selected = tuple(by_event_policy[(event_id, policy_key)] for event_id in matched_event_ids)
        if not selected:
            rows.append(
                MatchedPolicyEconomics(
                    policy_key=policy_key,
                    episodes=0,
                    clusters=0,
                    selected=0,
                    cash=0,
                    mean_episode_gross_return_pct=None,
                    mean_entry_impact_bps=None,
                    mean_exit_impact_bps=None,
                    mean_fee_cost_bps=None,
                    mean_funding_cost_bps=None,
                    mean_total_cost_bps=None,
                    mean_episode_net_return_pct=None,
                    mean_trade_net_return_pct=None,
                )
            )
            continue
        gross_returns: list[float] = []
        net_returns: list[float] = []
        entry_impacts: list[float] = []
        exit_impacts: list[float] = []
        fee_costs: list[float] = []
        funding_costs: list[float] = []
        trade_returns: list[float] = []
        for result in selected:
            net_return = result.episode_net_return_pct
            if net_return is None:
                raise RuntimeError("matched economics contains an unresolved episode")
            trade = _complete_trade(result)
            if trade is None:
                gross_returns.append(0.0)
                net_returns.append(net_return)
                entry_impacts.append(0.0)
                exit_impacts.append(0.0)
                fee_costs.append(0.0)
                funding_costs.append(0.0)
                continue
            required = (
                trade.gross_return_pct,
                result.entry_impact_bps,
                result.exit_impact_bps,
                trade.fee_cost_bps,
                trade.funding_cost_bps,
                trade.net_return_pct,
            )
            if any(value is None for value in required):
                raise RuntimeError("complete trade is missing economics components")
            gross_returns.append(trade.gross_return_pct or 0.0)
            net_returns.append(net_return)
            entry_impacts.append(result.entry_impact_bps or 0.0)
            exit_impacts.append(result.exit_impact_bps or 0.0)
            fee_costs.append(trade.fee_cost_bps or 0.0)
            funding_costs.append(trade.funding_cost_bps or 0.0)
            trade_returns.append(trade.net_return_pct or 0.0)
        mean_entry = fmean(entry_impacts)
        mean_exit = fmean(exit_impacts)
        mean_fees = fmean(fee_costs)
        mean_funding = fmean(funding_costs)
        rows.append(
            MatchedPolicyEconomics(
                policy_key=policy_key,
                episodes=len(selected),
                clusters=len({result.cluster_key for result in selected}),
                selected=sum(result.selected_decision_id is not None for result in selected),
                cash=sum(result.status == "not_triggered" for result in selected),
                mean_episode_gross_return_pct=fmean(gross_returns),
                mean_entry_impact_bps=mean_entry,
                mean_exit_impact_bps=mean_exit,
                mean_fee_cost_bps=mean_fees,
                mean_funding_cost_bps=mean_funding,
                mean_total_cost_bps=mean_entry + mean_exit + mean_fees + mean_funding,
                mean_episode_net_return_pct=fmean(net_returns),
                mean_trade_net_return_pct=_mean(trade_returns),
            )
        )
    return tuple(rows)


def _spread_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value <= 10:
        return "0-10"
    if value <= 25:
        return "10-25"
    if value <= 50:
        return "25-50"
    return "50+"


def _impact_bucket(value: float) -> str:
    if value <= 20:
        return "0-20"
    if value <= 50:
        return "20-50"
    if value <= 100:
        return "50-100"
    return "100+"


def liquidity_segment_economics(
    results: tuple[EconomicsEpisodeResult, ...],
) -> tuple[LiquiditySegmentEconomics, ...]:
    """Describe completed trades without presenting segment N as matched policy N."""
    grouped: dict[tuple[str, str, str], list[tuple[EconomicsEpisodeResult, VirtualTrade]]] = (
        defaultdict(list)
    )
    for result in results:
        if result.policy_key not in ECONOMICS_POLICY_KEYS:
            continue
        trade = _complete_trade(result)
        if trade is None or result.entry_impact_bps is None or result.exit_impact_bps is None:
            continue
        round_trip_impact = result.entry_impact_bps + result.exit_impact_bps
        dimensions = (
            ("overall", "all"),
            ("exchange", result.exchange or "missing"),
            ("spread_bps", _spread_bucket(result.spread_bps)),
            ("round_trip_impact_bps", _impact_bucket(round_trip_impact)),
        )
        for dimension, bucket in dimensions:
            grouped[(result.policy_key, dimension, bucket)].append((result, trade))

    rows: list[LiquiditySegmentEconomics] = []
    for (policy_key, dimension, bucket), items in sorted(grouped.items()):
        trades = [trade for _, trade in items]
        required_values = tuple(
            (
                trade.gross_return_pct,
                result.entry_impact_bps,
                result.exit_impact_bps,
                trade.fee_cost_bps,
                trade.funding_cost_bps,
                trade.net_return_pct,
            )
            for result, trade in items
        )
        if any(any(value is None for value in values) for values in required_values):
            raise RuntimeError("complete segmented trade is missing economics components")
        mean_entry = fmean(result.entry_impact_bps or 0.0 for result, _ in items)
        mean_exit = fmean(result.exit_impact_bps or 0.0 for result, _ in items)
        mean_fees = fmean(trade.fee_cost_bps or 0.0 for trade in trades)
        mean_funding = fmean(trade.funding_cost_bps or 0.0 for trade in trades)
        rows.append(
            LiquiditySegmentEconomics(
                policy_key=policy_key,
                dimension=dimension,
                bucket=bucket,
                trades=len(trades),
                clusters=len({result.cluster_key for result, _ in items}),
                mean_gross_return_pct=fmean(trade.gross_return_pct or 0.0 for trade in trades),
                mean_entry_impact_bps=mean_entry,
                mean_exit_impact_bps=mean_exit,
                mean_fee_cost_bps=mean_fees,
                mean_funding_cost_bps=mean_funding,
                mean_total_cost_bps=mean_entry + mean_exit + mean_fees + mean_funding,
                mean_net_return_pct=fmean(trade.net_return_pct or 0.0 for trade in trades),
            )
        )
    return tuple(rows)


def _missing_path(episode: ReplayEpisode, exchange: str, base: str) -> MarketPath:
    return MarketPath(
        pump_event_id=episode.pump_event_id,
        exchange=exchange,
        base=base,
        status="missing_path",
        candles=(),
        error="market path was not loaded",
    )


def build_exit_ablation_trades(
    dataset: ReplayDataset,
    path_by_decision: dict[str, MarketPath],
    costs: CostParameters,
) -> tuple[ExitAblationTrade, ...]:
    rows: list[ExitAblationTrade] = []
    for episode in dataset.eligible_episodes:
        for policy in _economics_policies():
            selection = select_score_policy(episode, policy)
            decision = selection.decision
            if selection.status != "selected" or decision is None:
                continue
            path = path_by_decision.get(decision.decision_id or "")
            if path is None:
                path = _missing_path(episode, decision.exchange, decision.base)
            for mechanics in ECONOMICS_EXIT_MECHANICS:
                rows.append(
                    ExitAblationTrade(
                        policy_key=policy.key,
                        mechanics_key=mechanics.key,
                        trade=simulate_decision(
                            episode,
                            path,
                            decision,
                            selection_reason=f"economics:{policy.key}:{mechanics.key}",
                            costs=costs,
                            exit_mechanics=mechanics,
                        ),
                    )
                )
    return tuple(rows)


def _paired_ablation_ids(
    rows: tuple[ExitAblationTrade, ...],
    policy_key: str,
) -> set[int]:
    by_event: dict[int, dict[str, VirtualTrade]] = defaultdict(dict)
    for row in rows:
        if row.policy_key == policy_key:
            by_event[row.trade.pump_event_id][row.mechanics_key] = row.trade
    required = {mechanics.key for mechanics in ECONOMICS_EXIT_MECHANICS}
    return {
        event_id
        for event_id, trades in by_event.items()
        if set(trades) == required and all(trade.status == "complete" for trade in trades.values())
    }


def exit_ablation_metrics(
    rows: tuple[ExitAblationTrade, ...],
) -> tuple[ExitAblationMetrics, ...]:
    metrics: list[ExitAblationMetrics] = []
    for policy_key in ECONOMICS_POLICY_KEYS:
        paired_ids = _paired_ablation_ids(rows, policy_key)
        for mechanics in ECONOMICS_EXIT_MECHANICS:
            trades = [
                row.trade
                for row in rows
                if row.policy_key == policy_key
                and row.mechanics_key == mechanics.key
                and row.trade.pump_event_id in paired_ids
            ]
            returns = [trade.net_return_pct for trade in trades if trade.net_return_pct is not None]
            metrics.append(
                ExitAblationMetrics(
                    policy_key=policy_key,
                    mechanics_key=mechanics.key,
                    episodes=len(trades),
                    clusters=len({trade.cluster_key for trade in trades}),
                    mean_net_return_pct=_mean(returns),
                    win_rate_pct=(
                        sum(value > 0 for value in returns) / len(returns) * 100
                        if returns
                        else None
                    ),
                    initial_stop_rate_pct=(
                        sum(trade.exit_reason == "initial_sl" for trade in trades)
                        / len(trades)
                        * 100
                        if trades
                        else None
                    ),
                    mean_duration_minutes=_mean(
                        [
                            trade.duration_minutes
                            for trade in trades
                            if trade.duration_minutes is not None
                        ]
                    ),
                    mean_mfe_pct=_mean(
                        [trade.mfe_pct for trade in trades if trade.mfe_pct is not None]
                    ),
                    mean_mae_pct=_mean(
                        [trade.mae_pct for trade in trades if trade.mae_pct is not None]
                    ),
                )
            )
    return tuple(metrics)


_EXIT_EFFECT_PAIRS = (
    ("initial_stop_effect", "max_hold_only", "initial_sl_max_hold"),
    ("trailing_effect", "initial_sl_max_hold", "full_v1"),
    ("dynamic_clock_effect", "fixed_240_only", "max_hold_only"),
)


def exit_mechanics_effects(
    rows: tuple[ExitAblationTrade, ...],
) -> tuple[ExitMechanicsEffect, ...]:
    by_key = {
        (row.policy_key, row.mechanics_key, row.trade.pump_event_id): row.trade for row in rows
    }
    output: list[ExitMechanicsEffect] = []
    for policy_key in ECONOMICS_POLICY_KEYS:
        for effect_key, reference_key, variant_key in _EXIT_EFFECT_PAIRS:
            pairs = [
                (
                    by_key[(policy_key, reference_key, event_id)],
                    by_key[(policy_key, variant_key, event_id)],
                )
                for event_id in sorted(_paired_ablation_ids(rows, policy_key))
            ]
            resolved = [
                (reference, variant)
                for reference, variant in pairs
                if reference.net_return_pct is not None and variant.net_return_pct is not None
            ]
            deltas = [
                (variant.net_return_pct or 0.0) - (reference.net_return_pct or 0.0)
                for reference, variant in resolved
            ]
            output.append(
                ExitMechanicsEffect(
                    policy_key=policy_key,
                    effect_key=effect_key,
                    reference_key=reference_key,
                    variant_key=variant_key,
                    episodes=len(resolved),
                    mean_reference_net_return_pct=_mean(
                        [reference.net_return_pct or 0.0 for reference, _ in resolved]
                    ),
                    mean_variant_net_return_pct=_mean(
                        [variant.net_return_pct or 0.0 for _, variant in resolved]
                    ),
                    mean_delta_pct=_mean(deltas),
                    improved_episodes=sum(delta > 1e-12 for delta in deltas),
                    worsened_episodes=sum(delta < -1e-12 for delta in deltas),
                    unchanged_episodes=sum(abs(delta) <= 1e-12 for delta in deltas),
                )
            )
    return tuple(output)


def initial_stop_follow_through(
    rows: tuple[ExitAblationTrade, ...],
) -> tuple[InitialStopFollowThrough, ...]:
    by_key = {
        (row.policy_key, row.mechanics_key, row.trade.pump_event_id): row.trade for row in rows
    }
    output: list[InitialStopFollowThrough] = []
    for policy_key in ECONOMICS_POLICY_KEYS:
        paired_ids = sorted(_paired_ablation_ids(rows, policy_key))
        stopouts = [
            event_id
            for event_id in paired_ids
            if by_key[(policy_key, BASELINE_EXIT_MECHANICS.key, event_id)].exit_reason
            == "initial_sl"
        ]
        fixed = [
            by_key[(policy_key, "fixed_240_only", event_id)]
            for event_id in stopouts
            if by_key[(policy_key, "fixed_240_only", event_id)].net_return_pct is not None
        ]
        positive = [
            trade
            for trade in fixed
            if trade.net_return_pct is not None and trade.net_return_pct > 0
        ]
        output.append(
            InitialStopFollowThrough(
                policy_key=policy_key,
                initial_stop_exits=len(stopouts),
                fixed_240_resolved=len(fixed),
                fixed_240_positive=len(positive),
                fixed_240_positive_rate_pct=(len(positive) / len(fixed) * 100 if fixed else None),
                mean_fixed_240_net_return_pct=_mean(
                    [trade.net_return_pct for trade in fixed if trade.net_return_pct is not None]
                ),
                mean_fixed_240_mae_pct=_mean(
                    [trade.mae_pct for trade in fixed if trade.mae_pct is not None]
                ),
            )
        )
    return tuple(output)
