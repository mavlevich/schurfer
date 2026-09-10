"""Pure endpoint replay for the extreme-mover discovery program.

This module deliberately evaluates only evidence already present on a selected
decision and its exact native forward outcome.  It does not manufacture an
intra-window path from repeated decisions and it does not substitute another
venue's outcome.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from statistics import fmean, median
from typing import TYPE_CHECKING, Any, Literal

from schurfer_performance import (
    COST_MODEL_VERSION,
    DEFAULT_COSTS,
    CostParameters,
    calculate_performance,
)

from .clustered_inference import (
    CLUSTER_BOOTSTRAP_VERSION,
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_BOOTSTRAP_SEED,
    ClusterObservation,
    cluster_bootstrap_mean,
    derived_seed,
)
from .outcomes import RESOLVER_VERSION
from .reporting import profit_factor

if TYPE_CHECKING:
    from .replay import ReplayDecision, ReplayOutcome

REPORT_VERSION = "extreme_mover_endpoint_replay_v1"
DISCOVERY_START = datetime(2026, 8, 27, tzinfo=UTC)
# Frozen at least four hours behind the implementation timestamp so every
# selected decision can have a mature 240-minute endpoint on the first run.
DISCOVERY_END = datetime(2026, 9, 10, 17, tzinfo=UTC)
SELECTION_VERSION = "first_and_first_quality_decision_v1"
ENTRY_LIQUIDITY_VERSION = "decision_impact_100usd_v1"
EXIT_SLIPPAGE_VERSION = "fixed_15bps_with_0x_2x_sensitivity_v1"
STRATEGY_VERSIONS = (
    "pump_short_measurement_v1",
    "pump_short_v1_market_quality",
)
HORIZONS_MINUTES = (15, 60, 240)
ANCHORS = ("first_decision", "first_quality")
DIRECTIONS = ("long", "short")
POSITION_NOTIONAL_USD = 100.0
ENTRY_IMPACT_NOTIONAL_KEY = "100"
PRIMARY_EXIT_SLIPPAGE_BPS = 15.0
EXIT_SLIPPAGE_SENSITIVITY_BPS = (0.0, 15.0, 30.0)
MIN_COMPLETED_TRADES = 100
MIN_ASSET_CLUSTERS = 30
MIN_UTC_WEEKS = 2

ResultStatus = Literal["complete", "cash", "unresolved"]
RoutingVerdict = Literal[
    "stop",
    "existing_data_candidate",
    "measurement_blocked",
    "insufficient_discovery",
]


@dataclass(frozen=True)
class Episode:
    pump_event_id: int
    cluster_key: str
    decisions: tuple[ReplayDecision, ...]


@dataclass(frozen=True)
class EpisodeResult:
    pump_event_id: int
    cluster_key: str
    anchor: str
    direction: str
    horizon_minutes: int
    status: ResultStatus
    reason: str | None
    decision_id: str | None
    decision_at: datetime | None
    exchange: str | None
    gross_return_pct: float | None
    net_return_pct: float | None
    net_return_zero_exit_slippage_pct: float | None
    net_return_double_exit_slippage_pct: float | None
    net_pnl_usd: float | None
    entry_impact_bps: float | None
    fee_cost_bps: float | None
    funding_cost_bps: float | None
    exit_slippage_bps: float | None
    mfe_pct: float | None
    mae_pct: float | None


@dataclass(frozen=True)
class CellMetrics:
    anchor: str
    direction: str
    horizon_minutes: int
    total_signals: int
    resolved_signals: int
    completed_trades: int
    cash: int
    unresolved: int
    clusters: int
    utc_weeks: int
    venues: int
    trades_per_calendar_day: float
    peak_concurrent_positions: int
    capital_occupancy_usd_hours: float
    mean_signal_net_return_pct: float | None
    mean_trade_net_return_pct: float | None
    mean_trade_zero_exit_slippage_pct: float | None
    mean_trade_double_exit_slippage_pct: float | None
    median_trade_net_return_pct: float | None
    win_rate_pct: float | None
    profit_factor: float | None
    total_net_pnl_usd: float | None
    max_sequential_drawdown_usd: float | None
    worst_trade_pct: float | None
    longest_losing_streak: int
    mean_mfe_pct: float | None
    mean_mae_pct: float | None
    bootstrap_lower_pct: float | None
    bootstrap_upper_pct: float | None
    largest_asset_share_pct: float | None
    largest_venue_share_pct: float | None
    largest_week_share_pct: float | None
    worst_leave_one_asset_out_mean_pct: float | None
    worst_leave_one_venue_out_mean_pct: float | None
    worst_leave_one_week_out_mean_pct: float | None
    candidate_ready: bool


@dataclass(frozen=True)
class DirectionVerdict:
    direction: str
    verdict: RoutingVerdict
    selected_cell: str | None
    reason: str


@dataclass(frozen=True)
class CoverageRow:
    name: str
    count: int


@dataclass(frozen=True)
class ExchangeCoverage:
    exchange: str
    selected_episodes: int
    exact_outcomes_60m: int
    unresolved_outcomes_60m: int
    fillable_long_entries_60m: int
    fillable_short_entries_60m: int


@dataclass(frozen=True)
class Manifest:
    report_version: str
    selection_version: str
    entry_liquidity_version: str
    exit_slippage_version: str
    cost_model_version: str
    resolver_version: str
    strategy_versions: tuple[str, ...]
    horizons_minutes: tuple[int, ...]
    anchors: tuple[str, ...]
    directions: tuple[str, ...]
    dataset_since: datetime
    dataset_until_exclusive: datetime
    database_snapshot_at: datetime
    generated_at: datetime
    code_revision: str
    working_tree_dirty: bool
    input_fingerprint: str
    position_notional_usd: float
    entry_impact_notional_key: str
    primary_exit_slippage_bps: float
    exit_slippage_sensitivity_bps: tuple[float, ...]
    taker_fee_bps_per_side: float
    funding_cost_bps_per_8h: float
    min_completed_trades: int
    min_asset_clusters: int
    min_utc_weeks: int
    bootstrap_version: str
    bootstrap_iterations: int
    bootstrap_seed: int
    interpretation: str = "viewed_window_discovery_only"


@dataclass(frozen=True)
class Report:
    manifest: Manifest
    episodes: int
    decisions: int
    coverage: tuple[CoverageRow, ...]
    exchange_coverage: tuple[ExchangeCoverage, ...]
    metrics: tuple[CellMetrics, ...]
    verdicts: tuple[DirectionVerdict, ...]
    episode_results: tuple[EpisodeResult, ...]


def _finite_positive(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _quality_allowed(decision: ReplayDecision) -> bool:
    liquidity = decision.liquidity
    if not isinstance(liquidity, dict):
        return False
    quality = liquidity.get("quality")
    return isinstance(quality, dict) and quality.get("allowed") is True


def _entry_impact(decision: ReplayDecision, direction: str) -> float | None:
    liquidity = decision.liquidity
    if not isinstance(liquidity, dict) or liquidity.get("status") != "sampled":
        return None
    impacts = liquidity.get("ask_impact_bps" if direction == "long" else "bid_impact_bps")
    if not isinstance(impacts, dict):
        return None
    value = impacts.get(ENTRY_IMPACT_NOTIONAL_KEY)
    if value is None:
        value = impacts.get(f"{POSITION_NOTIONAL_USD:.1f}")
    parsed = _finite_positive(value)
    if parsed is not None:
        return parsed
    if value is None:
        return None
    try:
        zero = float(value)
    except (TypeError, ValueError):
        return None
    return 0.0 if math.isfinite(zero) and zero == 0 else None


def _exact_outcome(decision: ReplayDecision, horizon_minutes: int) -> ReplayOutcome | None:
    matches = tuple(
        outcome for outcome in decision.outcomes if outcome.horizon_minutes == horizon_minutes
    )
    if len(matches) != 1:
        return None
    outcome = matches[0]
    if (
        outcome.status != "complete"
        or outcome.anchor_exchange != decision.exchange
        or outcome.source_exchange != decision.exchange
        or _finite_positive(outcome.entry_price) is None
        or _finite_positive(outcome.forward_price) is None
    ):
        return None
    return outcome


def build_episodes(decisions: tuple[ReplayDecision, ...]) -> tuple[Episode, ...]:
    grouped: dict[int, list[ReplayDecision]] = defaultdict(list)
    for decision in decisions:
        if (
            decision.pump_event_id is None
            or decision.pump_event_id <= 0
            or decision.strategy_version not in STRATEGY_VERSIONS
        ):
            continue
        grouped[decision.pump_event_id].append(decision)
    episodes: list[Episode] = []
    for event_id, rows in grouped.items():
        bases = {
            value.strip().upper()
            for row in rows
            for value in (row.base, row.event_base)
            if isinstance(value, str) and value.strip()
        }
        if len(bases) != 1:
            raise ValueError(
                f"pump event {event_id} has inconsistent base identity: {sorted(bases)}"
            )
        episodes.append(
            Episode(
                pump_event_id=event_id,
                cluster_key=f"base:{next(iter(bases))}",
                decisions=tuple(sorted(rows, key=lambda row: (row.ts, row.row_id))),
            )
        )
    return tuple(sorted(episodes, key=lambda item: (item.decisions[0].ts, item.pump_event_id)))


def _select(episode: Episode, anchor: str) -> ReplayDecision | None:
    if anchor == "first_decision":
        return episode.decisions[0]
    if anchor == "first_quality":
        return next((row for row in episode.decisions if _quality_allowed(row)), None)
    raise ValueError(f"unknown anchor: {anchor}")


def _unresolved_reason(decision: ReplayDecision, horizon_minutes: int) -> str:
    matches = tuple(
        outcome for outcome in decision.outcomes if outcome.horizon_minutes == horizon_minutes
    )
    if not matches:
        return "missing_outcome"
    if len(matches) > 1:
        return "duplicate_outcome"
    outcome = matches[0]
    if outcome.status != "complete":
        return f"outcome_status:{outcome.status}"
    if outcome.anchor_exchange != decision.exchange or outcome.source_exchange != decision.exchange:
        return "non_exact_venue_outcome"
    return "invalid_outcome_price"


def evaluate_episode(
    episode: Episode,
    *,
    costs: CostParameters = DEFAULT_COSTS,
    dataset_until_exclusive: datetime | None = None,
) -> tuple[EpisodeResult, ...]:
    results: list[EpisodeResult] = []
    for anchor in ANCHORS:
        decision = _select(episode, anchor)
        for direction in DIRECTIONS:
            for horizon in HORIZONS_MINUTES:
                if decision is None:
                    results.append(
                        EpisodeResult(
                            episode.pump_event_id,
                            episode.cluster_key,
                            anchor,
                            direction,
                            horizon,
                            "cash",
                            "no_observed_quality_decision",
                            None,
                            None,
                            None,
                            0.0,
                            0.0,
                            0.0,
                            0.0,
                            0.0,
                            None,
                            0.0,
                            0.0,
                            0.0,
                            None,
                            None,
                        )
                    )
                    continue
                if (
                    dataset_until_exclusive is not None
                    and decision.ts + timedelta(minutes=horizon) > dataset_until_exclusive
                ):
                    results.append(
                        EpisodeResult(
                            episode.pump_event_id,
                            episode.cluster_key,
                            anchor,
                            direction,
                            horizon,
                            "unresolved",
                            "outcome_straddles_window",
                            decision.decision_id,
                            decision.ts,
                            decision.exchange,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                        )
                    )
                    continue
                outcome = _exact_outcome(decision, horizon)
                if outcome is None:
                    results.append(
                        EpisodeResult(
                            episode.pump_event_id,
                            episode.cluster_key,
                            anchor,
                            direction,
                            horizon,
                            "unresolved",
                            _unresolved_reason(decision, horizon),
                            decision.decision_id,
                            decision.ts,
                            decision.exchange,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                        )
                    )
                    continue
                impact = _entry_impact(decision, direction)
                if impact is None:
                    results.append(
                        EpisodeResult(
                            episode.pump_event_id,
                            episode.cluster_key,
                            anchor,
                            direction,
                            horizon,
                            "cash",
                            "entry_liquidity_unavailable",
                            decision.decision_id,
                            decision.ts,
                            decision.exchange,
                            0.0,
                            0.0,
                            0.0,
                            0.0,
                            0.0,
                            None,
                            0.0,
                            0.0,
                            0.0,
                            None,
                            None,
                        )
                    )
                    continue
                entry = _finite_positive(outcome.entry_price)
                exit_price = _finite_positive(outcome.forward_price)
                if entry is None or exit_price is None:  # defensive; _exact_outcome checked both
                    raise AssertionError("exact outcome lost its validated prices")
                primary = calculate_performance(
                    position_usd=POSITION_NOTIONAL_USD,
                    entry_price=entry,
                    exit_price=exit_price,
                    side=direction,
                    duration_minutes=float(horizon),
                    entry_slippage_bps=impact,
                    exit_slippage_bps=PRIMARY_EXIT_SLIPPAGE_BPS,
                    costs=costs,
                )
                zero_exit = calculate_performance(
                    position_usd=POSITION_NOTIONAL_USD,
                    entry_price=entry,
                    exit_price=exit_price,
                    side=direction,
                    duration_minutes=float(horizon),
                    entry_slippage_bps=impact,
                    exit_slippage_bps=0.0,
                    costs=costs,
                )
                double_exit = calculate_performance(
                    position_usd=POSITION_NOTIONAL_USD,
                    entry_price=entry,
                    exit_price=exit_price,
                    side=direction,
                    duration_minutes=float(horizon),
                    entry_slippage_bps=impact,
                    exit_slippage_bps=2 * PRIMARY_EXIT_SLIPPAGE_BPS,
                    costs=costs,
                )
                short_mfe = outcome.mfe_pct
                short_mae = outcome.mae_pct
                results.append(
                    EpisodeResult(
                        episode.pump_event_id,
                        episode.cluster_key,
                        anchor,
                        direction,
                        horizon,
                        "complete",
                        None,
                        decision.decision_id,
                        decision.ts,
                        decision.exchange,
                        primary.gross_return_pct,
                        primary.net_return_pct,
                        zero_exit.net_return_pct,
                        double_exit.net_return_pct,
                        primary.net_pnl_usd,
                        impact,
                        primary.fee_cost_bps,
                        primary.funding_cost_bps,
                        PRIMARY_EXIT_SLIPPAGE_BPS,
                        short_mfe if direction == "short" else short_mae,
                        short_mae if direction == "short" else short_mfe,
                    )
                )
    return tuple(results)


def _week_key(value: datetime) -> str:
    iso_year, iso_week, _ = value.astimezone(UTC).isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _largest_share(values: list[str]) -> float | None:
    if not values:
        return None
    return max(Counter(values).values()) / len(values) * 100


def _worst_leave_one_out(values: list[tuple[str, float]]) -> float | None:
    keys = sorted({key for key, _ in values})
    if len(keys) < 2:
        return None
    means = [fmean(value for key, value in values if key != excluded) for excluded in keys]
    return min(means)


def _drawdown(rows: tuple[EpisodeResult, ...]) -> float | None:
    pnl = [row.net_pnl_usd for row in rows if row.net_pnl_usd is not None]
    if not pnl:
        return None
    equity = peak = drawdown = 0.0
    for value in pnl:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _losing_streak(values: list[float]) -> int:
    longest = current = 0
    for value in values:
        if value < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _peak_concurrency(rows: tuple[EpisodeResult, ...]) -> int:
    events: list[tuple[datetime, int]] = []
    for row in rows:
        if row.decision_at is None:
            continue
        events.append((row.decision_at, 1))
        events.append((row.decision_at + timedelta(minutes=row.horizon_minutes), -1))
    running = peak = 0
    for _, delta in sorted(events, key=lambda item: (item[0], item[1])):
        running += delta
        peak = max(peak, running)
    return peak


def _cell_metrics(
    rows: tuple[EpisodeResult, ...],
    *,
    anchor: str,
    direction: str,
    horizon: int,
    bootstrap_iterations: int,
    bootstrap_seed: int,
) -> CellMetrics:
    selected = tuple(
        row
        for row in rows
        if row.anchor == anchor and row.direction == direction and row.horizon_minutes == horizon
    )
    completed = tuple(row for row in selected if row.status == "complete")
    resolved = tuple(row for row in selected if row.net_return_pct is not None)
    trade_returns = [
        float(row.net_return_pct) for row in completed if row.net_return_pct is not None
    ]
    zero_exit_returns = [
        float(row.net_return_zero_exit_slippage_pct)
        for row in completed
        if row.net_return_zero_exit_slippage_pct is not None
    ]
    double_exit_returns = [
        float(row.net_return_double_exit_slippage_pct)
        for row in completed
        if row.net_return_double_exit_slippage_pct is not None
    ]
    signal_returns = [
        float(row.net_return_pct) for row in resolved if row.net_return_pct is not None
    ]
    clusters = sorted({row.cluster_key for row in completed})
    weeks = sorted({_week_key(row.decision_at) for row in completed if row.decision_at is not None})
    venues = sorted({row.exchange for row in completed if row.exchange is not None})
    bootstrap_lower = bootstrap_upper = None
    if signal_returns:
        estimate = cluster_bootstrap_mean(
            tuple(
                ClusterObservation(row.cluster_key, float(row.net_return_pct))
                for row in resolved
                if row.net_return_pct is not None
            ),
            iterations=bootstrap_iterations,
            seed=derived_seed(bootstrap_seed, f"{anchor}:{direction}:{horizon}"),
        ).estimate
        bootstrap_lower = estimate.lower_bound
        bootstrap_upper = estimate.upper_bound
    asset_values = [
        (row.cluster_key, float(row.net_return_pct))
        for row in completed
        if row.net_return_pct is not None
    ]
    venue_values = [
        (str(row.exchange), float(row.net_return_pct))
        for row in completed
        if row.net_return_pct is not None
    ]
    week_values = [
        (_week_key(row.decision_at), float(row.net_return_pct))
        for row in completed
        if row.net_return_pct is not None and row.decision_at is not None
    ]
    pf = profit_factor(trade_returns)
    mean_trade = fmean(trade_returns) if trade_returns else None
    worst_asset = _worst_leave_one_out(asset_values)
    worst_week = _worst_leave_one_out(week_values)
    worst_venue = _worst_leave_one_out(venue_values)
    profit_factor_positive = (pf is not None and pf > 1) or (
        pf is None and bool(trade_returns) and min(trade_returns) >= 0
    )
    candidate_ready = (
        len(completed) >= MIN_COMPLETED_TRADES
        and len(clusters) >= MIN_ASSET_CLUSTERS
        and len(weeks) >= MIN_UTC_WEEKS
        and mean_trade is not None
        and mean_trade > 0
        and profit_factor_positive
        and worst_asset is not None
        and worst_asset > 0
        and worst_week is not None
        and worst_week > 0
        and (worst_venue is None or worst_venue > 0)
    )
    decision_dates = [
        row.decision_at.astimezone(UTC).date() for row in selected if row.decision_at is not None
    ]
    calendar_days = (
        max(1, (max(decision_dates) - min(decision_dates)).days + 1) if decision_dates else 1
    )
    return CellMetrics(
        anchor=anchor,
        direction=direction,
        horizon_minutes=horizon,
        total_signals=len(selected),
        resolved_signals=len(resolved),
        completed_trades=len(completed),
        cash=sum(row.status == "cash" for row in selected),
        unresolved=sum(row.status == "unresolved" for row in selected),
        clusters=len(clusters),
        utc_weeks=len(weeks),
        venues=len(venues),
        trades_per_calendar_day=len(completed) / calendar_days,
        peak_concurrent_positions=_peak_concurrency(completed),
        capital_occupancy_usd_hours=(len(completed) * POSITION_NOTIONAL_USD * horizon / 60),
        mean_signal_net_return_pct=fmean(signal_returns) if signal_returns else None,
        mean_trade_net_return_pct=mean_trade,
        mean_trade_zero_exit_slippage_pct=(fmean(zero_exit_returns) if zero_exit_returns else None),
        mean_trade_double_exit_slippage_pct=(
            fmean(double_exit_returns) if double_exit_returns else None
        ),
        median_trade_net_return_pct=median(trade_returns) if trade_returns else None,
        win_rate_pct=(
            sum(value > 0 for value in trade_returns) / len(trade_returns) * 100
            if trade_returns
            else None
        ),
        profit_factor=pf,
        total_net_pnl_usd=sum(row.net_pnl_usd or 0.0 for row in completed) if completed else None,
        max_sequential_drawdown_usd=_drawdown(completed),
        worst_trade_pct=min(trade_returns) if trade_returns else None,
        longest_losing_streak=_losing_streak(trade_returns),
        mean_mfe_pct=fmean(row.mfe_pct for row in completed if row.mfe_pct is not None)
        if any(row.mfe_pct is not None for row in completed)
        else None,
        mean_mae_pct=fmean(row.mae_pct for row in completed if row.mae_pct is not None)
        if any(row.mae_pct is not None for row in completed)
        else None,
        bootstrap_lower_pct=bootstrap_lower,
        bootstrap_upper_pct=bootstrap_upper,
        largest_asset_share_pct=_largest_share([row.cluster_key for row in completed]),
        largest_venue_share_pct=_largest_share([str(row.exchange) for row in completed]),
        largest_week_share_pct=_largest_share(
            [_week_key(row.decision_at) for row in completed if row.decision_at is not None]
        ),
        worst_leave_one_asset_out_mean_pct=worst_asset,
        worst_leave_one_venue_out_mean_pct=worst_venue,
        worst_leave_one_week_out_mean_pct=worst_week,
        candidate_ready=candidate_ready,
    )


def _cell_key(metric: CellMetrics) -> str:
    return f"{metric.anchor}:{metric.direction}:{metric.horizon_minutes}m"


def _verdict(direction: str, metrics: tuple[CellMetrics, ...]) -> DirectionVerdict:
    directional = tuple(metric for metric in metrics if metric.direction == direction)
    candidates = tuple(metric for metric in directional if metric.candidate_ready)
    if candidates:
        selected = max(candidates, key=lambda item: item.mean_signal_net_return_pct or -math.inf)
        return DirectionVerdict(
            direction,
            "existing_data_candidate",
            _cell_key(selected),
            "at least one pre-declared cell clears the data and concentration gates",
        )
    mature = tuple(
        metric
        for metric in directional
        if metric.completed_trades >= MIN_COMPLETED_TRADES
        and metric.clusters >= MIN_ASSET_CLUSTERS
        and metric.utc_weeks >= MIN_UTC_WEEKS
    )
    if mature:
        return DirectionVerdict(
            direction,
            "stop",
            None,
            "mature cells exist but none retains positive after-cost economics "
            "across concentration sensitivities",
        )
    return DirectionVerdict(
        direction,
        "insufficient_discovery",
        None,
        "no cell reaches the declared completed-trade, asset-cluster and UTC-week floors",
    )


def _input_fingerprint(decisions: tuple[ReplayDecision, ...]) -> str:
    digest = hashlib.sha256()
    for decision in sorted(decisions, key=lambda row: (row.ts, row.row_id)):
        payload = asdict(decision)
        payload["ts"] = decision.ts.isoformat()
        digest.update(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _exchange_coverage(results: tuple[EpisodeResult, ...]) -> tuple[ExchangeCoverage, ...]:
    first_60 = tuple(
        row for row in results if row.anchor == "first_decision" and row.horizon_minutes == 60
    )
    exchanges = sorted({row.exchange for row in first_60 if row.exchange is not None})
    coverage: list[ExchangeCoverage] = []
    for exchange in exchanges:
        long_rows = tuple(
            row for row in first_60 if row.exchange == exchange and row.direction == "long"
        )
        short_rows = tuple(
            row for row in first_60 if row.exchange == exchange and row.direction == "short"
        )
        exact = sum(row.reason in {None, "entry_liquidity_unavailable"} for row in long_rows)
        coverage.append(
            ExchangeCoverage(
                exchange=exchange,
                selected_episodes=len(long_rows),
                exact_outcomes_60m=exact,
                unresolved_outcomes_60m=len(long_rows) - exact,
                fillable_long_entries_60m=sum(row.status == "complete" for row in long_rows),
                fillable_short_entries_60m=sum(row.status == "complete" for row in short_rows),
            )
        )
    return tuple(coverage)


def build_report(
    decisions: tuple[ReplayDecision, ...],
    *,
    dataset_since: datetime,
    dataset_until_exclusive: datetime,
    database_snapshot_at: datetime,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    costs: CostParameters = DEFAULT_COSTS,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> Report:
    if dataset_since.tzinfo is None or dataset_until_exclusive.tzinfo is None:
        raise ValueError("dataset bounds must be timezone-aware")
    if dataset_since >= dataset_until_exclusive:
        raise ValueError("dataset start must precede its exclusive end")
    if dataset_since != DISCOVERY_START or dataset_until_exclusive != DISCOVERY_END:
        raise ValueError("endpoint replay requires the frozen discovery window")
    if database_snapshot_at < DISCOVERY_END + timedelta(minutes=max(HORIZONS_MINUTES)):
        raise ValueError("endpoint replay window has not matured through 240 minutes")
    if bootstrap_iterations < 100:
        raise ValueError("bootstrap iterations must be at least 100")
    decision_ids = [decision.decision_id for decision in decisions]
    if len(decision_ids) != len(set(decision_ids)):
        raise ValueError("endpoint replay input contains duplicate decision IDs")
    unsupported_versions = sorted(
        {
            decision.strategy_version
            for decision in decisions
            if decision.strategy_version not in STRATEGY_VERSIONS
        },
        key=lambda value: "" if value is None else value,
    )
    if unsupported_versions:
        raise ValueError(
            f"endpoint replay input contains unsupported strategies: {unsupported_versions}"
        )
    if any(not (dataset_since <= decision.ts < dataset_until_exclusive) for decision in decisions):
        raise ValueError("endpoint replay input contains decisions outside the frozen window")

    input_coverage: Counter[str] = Counter()
    eligible: list[ReplayDecision] = []
    for decision in decisions:
        if decision.pump_event_id is None or decision.pump_event_id <= 0:
            input_coverage["missing_pump_event_id"] += 1
            continue
        if not decision.event_base or not decision.event_base.strip():
            input_coverage["missing_event_identity"] += 1
            continue
        eligible.append(decision)

    episodes = build_episodes(tuple(eligible))
    results = tuple(
        result
        for episode in episodes
        for result in evaluate_episode(
            episode,
            costs=costs,
            dataset_until_exclusive=dataset_until_exclusive,
        )
    )
    metrics = tuple(
        _cell_metrics(
            results,
            anchor=anchor,
            direction=direction,
            horizon=horizon,
            bootstrap_iterations=bootstrap_iterations,
            bootstrap_seed=bootstrap_seed,
        )
        for anchor in ANCHORS
        for direction in DIRECTIONS
        for horizon in HORIZONS_MINUTES
    )
    coverage = input_coverage + Counter(
        result.reason or result.status for result in results if result.status != "complete"
    )
    return Report(
        manifest=Manifest(
            report_version=REPORT_VERSION,
            selection_version=SELECTION_VERSION,
            entry_liquidity_version=ENTRY_LIQUIDITY_VERSION,
            exit_slippage_version=EXIT_SLIPPAGE_VERSION,
            cost_model_version=COST_MODEL_VERSION,
            resolver_version=RESOLVER_VERSION,
            strategy_versions=STRATEGY_VERSIONS,
            horizons_minutes=HORIZONS_MINUTES,
            anchors=ANCHORS,
            directions=DIRECTIONS,
            dataset_since=dataset_since,
            dataset_until_exclusive=dataset_until_exclusive,
            database_snapshot_at=database_snapshot_at,
            generated_at=generated_at,
            code_revision=code_revision.strip(),
            working_tree_dirty=working_tree_dirty,
            input_fingerprint=_input_fingerprint(decisions),
            position_notional_usd=POSITION_NOTIONAL_USD,
            entry_impact_notional_key=ENTRY_IMPACT_NOTIONAL_KEY,
            primary_exit_slippage_bps=PRIMARY_EXIT_SLIPPAGE_BPS,
            exit_slippage_sensitivity_bps=EXIT_SLIPPAGE_SENSITIVITY_BPS,
            taker_fee_bps_per_side=costs.taker_fee_bps_per_side,
            funding_cost_bps_per_8h=costs.funding_cost_bps_per_8h,
            min_completed_trades=MIN_COMPLETED_TRADES,
            min_asset_clusters=MIN_ASSET_CLUSTERS,
            min_utc_weeks=MIN_UTC_WEEKS,
            bootstrap_version=CLUSTER_BOOTSTRAP_VERSION,
            bootstrap_iterations=bootstrap_iterations,
            bootstrap_seed=bootstrap_seed,
        ),
        episodes=len(episodes),
        decisions=len(decisions),
        coverage=tuple(
            CoverageRow(name, count)
            for name, count in sorted(coverage.items(), key=lambda item: (-item[1], item[0]))
        ),
        exchange_coverage=_exchange_coverage(results),
        metrics=metrics,
        verdicts=tuple(_verdict(direction, metrics) for direction in DIRECTIONS),
        episode_results=results,
    )


__all__ = [
    "ANCHORS",
    "DIRECTIONS",
    "DISCOVERY_END",
    "DISCOVERY_START",
    "HORIZONS_MINUTES",
    "STRATEGY_VERSIONS",
    "CellMetrics",
    "Episode",
    "EpisodeResult",
    "Report",
    "build_episodes",
    "build_report",
    "evaluate_episode",
]
