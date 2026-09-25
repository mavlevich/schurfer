"""Deterministic research-only portfolio accounting for settled positions."""

from __future__ import annotations

import enum
import itertools
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime


class TradeDirection(enum.IntEnum):
    LONG = 1
    SHORT = -1


class PositionSizingPolicy(enum.Enum):
    FIXED_INITIAL_EQUITY = "fixed_initial_equity"
    CURRENT_EQUITY_EQUAL_WEIGHT = "current_equity_equal_weight"


class UnresolvedCapitalPolicy(enum.Enum):
    """How long an accepted unresolved position keeps its capital.

    ``HOLD_TO_END`` is the original fail-closed behavior: the slot is never released.
    ``PLANNED_EXIT`` releases the slot at the position's ``planned_exit_at`` and books
    an explicit, caller-supplied gross-return assumption. Either way the run remains
    ``accounting_complete=False``; the assumption is a scenario, never an outcome.
    """

    HOLD_TO_END = "hold_to_end"
    PLANNED_EXIT = "planned_exit"


@dataclass(frozen=True)
class PortfolioPosition:
    """One normalized position candidate.

    ``gross_return`` and ``net_return`` are direction-aware returns on notional.
    For a resolved position, ``net_return`` must equal gross return less the recorded
    slippage, fee, and funding costs. An unresolved position has no exit or returns.
    Its ``planned_exit_at`` is when the strategy would have exited; it is used only by
    ``UnresolvedCapitalPolicy.PLANNED_EXIT``.
    """

    decision_id: str
    canonical_asset: str
    direction: TradeDirection
    entry_at: datetime
    exit_at: datetime | None
    gross_return: float | None
    entry_slippage_bps: float
    exit_slippage_bps: float
    fees_bps: float
    funding_bps: float
    net_return: float | None
    unresolved_reason: str | None = None
    planned_exit_at: datetime | None = None


class EventType(enum.IntEnum):
    EXIT = 0
    ENTRY = 1


@dataclass(order=True, frozen=True)
class Event:
    at: datetime
    event_type: EventType
    canonical_asset: str
    decision_id: str
    position: PortfolioPosition = field(compare=False)


@dataclass(frozen=True)
class RejectedEntry:
    decision_id: str
    reason: str


@dataclass
class PortfolioMetrics:
    accepted_entries: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    final_equity: float = 0.0
    max_drawdown_pct: float = 0.0
    max_concurrent_positions: int = 0
    unresolved_fail_closed: int = 0
    unresolved_released: int = 0
    unresolved_assumed_net_pnl: float = 0.0
    available_cash: float = 0.0
    reserved_capital: float = 0.0
    notional_exposure: float = 0.0
    gross_exposure: float = 0.0
    net_exposure: float = 0.0
    leverage: float = 0.0
    peak_gross_exposure: float = 0.0
    peak_abs_net_exposure: float = 0.0
    peak_leverage: float = 0.0
    min_entry_margin: float | None = None
    max_entry_margin: float | None = None
    average_entry_margin: float | None = None
    accounting_complete: bool = True
    rejection_counts: dict[str, int] = field(default_factory=dict)
    rejected_entries: list[RejectedEntry] = field(default_factory=list)


@dataclass(frozen=True)
class _ActivePosition:
    position: PortfolioPosition
    margin: float
    notional: float


def _finite(value: float) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _validate_inputs(
    positions: Sequence[PortfolioPosition],
    *,
    initial_capital: float,
    k_slots: int,
    leverage: float,
    max_positions_per_asset: int,
    sizing_policy: PositionSizingPolicy,
    unresolved_policy: UnresolvedCapitalPolicy,
    unresolved_gross_return: float | None,
    max_gross_exposure_usd: float | None,
    max_abs_net_exposure_usd: float | None,
    max_leverage: float | None,
) -> None:
    if not _finite(initial_capital) or initial_capital <= 0:
        raise ValueError("initial_capital must be finite and positive")
    if k_slots <= 0:
        raise ValueError("k_slots must be positive")
    if max_positions_per_asset <= 0:
        raise ValueError("max_positions_per_asset must be positive")
    if not isinstance(sizing_policy, PositionSizingPolicy):
        raise ValueError("sizing_policy must be a PositionSizingPolicy")
    if not isinstance(unresolved_policy, UnresolvedCapitalPolicy):
        raise ValueError("unresolved_policy must be an UnresolvedCapitalPolicy")
    planned_exit = unresolved_policy is UnresolvedCapitalPolicy.PLANNED_EXIT
    if planned_exit and (unresolved_gross_return is None or not _finite(unresolved_gross_return)):
        raise ValueError("planned_exit needs an explicit finite unresolved_gross_return")
    if not planned_exit and unresolved_gross_return is not None:
        raise ValueError("unresolved_gross_return applies only to planned_exit")
    if not _finite(leverage) or leverage <= 0:
        raise ValueError("leverage must be finite and positive")
    for name, limit in (
        ("max_gross_exposure_usd", max_gross_exposure_usd),
        ("max_abs_net_exposure_usd", max_abs_net_exposure_usd),
        ("max_leverage", max_leverage),
    ):
        if limit is not None and (not _finite(limit) or limit <= 0):
            raise ValueError(f"{name} must be finite and positive when provided")

    ids = [position.decision_id for position in positions]
    if any(not decision_id for decision_id in ids) or len(set(ids)) != len(ids):
        raise ValueError("decision_id values must be non-empty and unique")

    for position in positions:
        if position.entry_at.tzinfo is None:
            raise ValueError(f"{position.decision_id}: entry_at must be timezone-aware")
        costs = (
            position.entry_slippage_bps,
            position.exit_slippage_bps,
            position.fees_bps,
            position.funding_bps,
        )
        if not all(_finite(cost) for cost in costs):
            raise ValueError(f"{position.decision_id}: costs must be finite")
        if any(cost < 0 for cost in costs[:3]):
            raise ValueError(f"{position.decision_id}: slippage and fees cannot be negative")

        if position.exit_at is None:
            if position.gross_return is not None or position.net_return is not None:
                raise ValueError(f"{position.decision_id}: unresolved position cannot have returns")
            if not position.unresolved_reason:
                raise ValueError(f"{position.decision_id}: unresolved position needs a reason")
            if planned_exit and (
                position.planned_exit_at is None
                or position.planned_exit_at.tzinfo is None
                or position.planned_exit_at <= position.entry_at
            ):
                raise ValueError(
                    f"{position.decision_id}: planned_exit needs a planned_exit_at after entry"
                )
            continue

        if position.exit_at.tzinfo is None or position.exit_at <= position.entry_at:
            raise ValueError(f"{position.decision_id}: exit_at must be after entry_at")
        if position.unresolved_reason is not None:
            raise ValueError(f"{position.decision_id}: resolved position has unresolved_reason")
        if position.gross_return is None or position.net_return is None:
            raise ValueError(
                f"{position.decision_id}: resolved position needs gross and net returns"
            )
        if not _finite(position.gross_return) or not _finite(position.net_return):
            raise ValueError(f"{position.decision_id}: returns must be finite")
        total_cost_bps = sum(costs)
        expected_net = position.gross_return - total_cost_bps / 10_000.0
        if not math.isclose(position.net_return, expected_net, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError(
                f"{position.decision_id}: net_return does not match registered cost provenance"
            )


def simulate_portfolio_v2(
    positions: Sequence[PortfolioPosition],
    *,
    initial_capital: float = 300.0,
    k_slots: int = 8,
    max_positions_per_asset: int = 1,
    sizing_policy: PositionSizingPolicy = PositionSizingPolicy.FIXED_INITIAL_EQUITY,
    unresolved_policy: UnresolvedCapitalPolicy = UnresolvedCapitalPolicy.HOLD_TO_END,
    unresolved_gross_return: float | None = None,
    leverage: float = 1.0,
    max_gross_exposure_usd: float | None = None,
    max_abs_net_exposure_usd: float | None = None,
    max_leverage: float | None = None,
) -> PortfolioMetrics:
    """Run a chronological K-slot simulation without partial allocations.

    Fixed sizing preserves the original v2 behavior. Current-equity equal weighting
    recomputes one slot as current realized equity divided by K immediately before each
    entry, so losses do not permanently strand a slot merely because its old fixed
    notional is no longer affordable.

    Under ``PLANNED_EXIT`` an unresolved position is settled at ``planned_exit_at`` with
    ``unresolved_gross_return`` less its recorded costs. That scenario PnL is reported in
    ``unresolved_assumed_net_pnl`` and in equity, never in realized trade counts or
    ``net_pnl``.
    """

    _validate_inputs(
        positions,
        initial_capital=initial_capital,
        k_slots=k_slots,
        leverage=leverage,
        max_positions_per_asset=max_positions_per_asset,
        sizing_policy=sizing_policy,
        unresolved_policy=unresolved_policy,
        unresolved_gross_return=unresolved_gross_return,
        max_gross_exposure_usd=max_gross_exposure_usd,
        max_abs_net_exposure_usd=max_abs_net_exposure_usd,
        max_leverage=max_leverage,
    )
    planned_exit = unresolved_policy is UnresolvedCapitalPolicy.PLANNED_EXIT
    events: list[Event] = []
    for position in positions:
        events.append(
            Event(
                position.entry_at,
                EventType.ENTRY,
                position.canonical_asset,
                position.decision_id,
                position,
            )
        )
        release_at = position.exit_at
        if release_at is None and planned_exit:
            release_at = position.planned_exit_at
        if release_at is not None:
            events.append(
                Event(
                    release_at,
                    EventType.EXIT,
                    position.canonical_asset,
                    position.decision_id,
                    position,
                )
            )
    events.sort()

    metrics = PortfolioMetrics(available_cash=initial_capital, final_equity=initial_capital)
    available_cash = initial_capital
    peak_equity = initial_capital
    fixed_margin_per_position = initial_capital / k_slots
    active: dict[str, _ActivePosition] = {}
    asset_counts: dict[str, int] = {}

    def exposures() -> tuple[float, float, float, float]:
        gross = sum(item.notional for item in active.values())
        net = sum(item.notional * item.position.direction.value for item in active.values())
        equity = available_cash + sum(item.margin for item in active.values())
        current_leverage = gross / equity if equity > 0 else math.inf
        return gross, net, equity, current_leverage

    def reject(position: PortfolioPosition, reason: str) -> None:
        metrics.rejection_counts[reason] = metrics.rejection_counts.get(reason, 0) + 1
        metrics.rejected_entries.append(RejectedEntry(position.decision_id, reason))

    def update_peaks() -> None:
        gross, net, _, current_leverage = exposures()
        metrics.peak_gross_exposure = max(metrics.peak_gross_exposure, gross)
        metrics.peak_abs_net_exposure = max(metrics.peak_abs_net_exposure, abs(net))
        metrics.peak_leverage = max(metrics.peak_leverage, current_leverage)

    for _at, timestamp_events_iter in itertools.groupby(events, key=lambda event: event.at):
        timestamp_events = list(timestamp_events_iter)

        # Settle the whole timestamp before measuring equity. Serially measuring exits
        # that occurred at the same instant makes drawdown depend on the lexical asset
        # tie-break rather than on the portfolio path.
        settled_exit = False
        for event in timestamp_events:
            if event.event_type is not EventType.EXIT:
                continue
            position = event.position
            active_position = active.pop(position.decision_id, None)
            if active_position is None:
                continue
            asset_counts[position.canonical_asset] -= 1
            settled_exit = True
            if position.exit_at is None:
                assert unresolved_gross_return is not None
                costs_bps = (
                    position.entry_slippage_bps
                    + position.exit_slippage_bps
                    + position.fees_bps
                    + position.funding_bps
                )
                assumed_pnl = active_position.notional * (
                    unresolved_gross_return - costs_bps / 10_000.0
                )
                available_cash += active_position.margin + assumed_pnl
                metrics.unresolved_released += 1
                metrics.unresolved_assumed_net_pnl += assumed_pnl
                continue
            assert position.gross_return is not None
            assert position.net_return is not None
            gross_pnl = active_position.notional * position.gross_return
            net_pnl = active_position.notional * position.net_return
            available_cash += active_position.margin + net_pnl
            metrics.total_trades += 1
            metrics.winning_trades += int(net_pnl > 0)
            metrics.losing_trades += int(net_pnl <= 0)
            metrics.gross_pnl += gross_pnl
            metrics.net_pnl += net_pnl

        if settled_exit:
            _, _, equity, _ = exposures()
            peak_equity = max(peak_equity, equity)
            if peak_equity > 0:
                metrics.max_drawdown_pct = max(
                    metrics.max_drawdown_pct,
                    (peak_equity - equity) / peak_equity,
                )
            update_peaks()

        # Events are already deterministically ordered; processing entries after the
        # complete exit batch lets capital released at this timestamp be reused.
        for event in timestamp_events:
            if event.event_type is not EventType.ENTRY:
                continue
            position = event.position
            if len(active) >= k_slots:
                reject(position, "max_concurrent_positions")
                continue
            if asset_counts.get(position.canonical_asset, 0) >= max_positions_per_asset:
                reject(position, "max_positions_per_asset")
                continue
            gross, net, equity, _ = exposures()
            margin_per_position = (
                equity / k_slots
                if sizing_policy is PositionSizingPolicy.CURRENT_EQUITY_EQUAL_WEIGHT
                else fixed_margin_per_position
            )
            notional_per_position = margin_per_position * leverage
            if available_cash < margin_per_position:
                reject(position, "insufficient_capital")
                continue
            proposed_gross = gross + notional_per_position
            proposed_net = net + notional_per_position * position.direction.value
            proposed_leverage = proposed_gross / equity if equity > 0 else math.inf
            if max_gross_exposure_usd is not None and proposed_gross > max_gross_exposure_usd:
                reject(position, "max_gross_exposure")
                continue
            if (
                max_abs_net_exposure_usd is not None
                and abs(proposed_net) > max_abs_net_exposure_usd
            ):
                reject(position, "max_abs_net_exposure")
                continue
            if max_leverage is not None and proposed_leverage > max_leverage:
                reject(position, "max_leverage")
                continue

            available_cash -= margin_per_position
            active[position.decision_id] = _ActivePosition(
                position=position,
                margin=margin_per_position,
                notional=notional_per_position,
            )
            asset_counts[position.canonical_asset] = (
                asset_counts.get(position.canonical_asset, 0) + 1
            )
            metrics.accepted_entries += 1
            metrics.min_entry_margin = (
                margin_per_position
                if metrics.min_entry_margin is None
                else min(metrics.min_entry_margin, margin_per_position)
            )
            metrics.max_entry_margin = (
                margin_per_position
                if metrics.max_entry_margin is None
                else max(metrics.max_entry_margin, margin_per_position)
            )
            if metrics.average_entry_margin is None:
                metrics.average_entry_margin = margin_per_position
            else:
                metrics.average_entry_margin += (
                    margin_per_position - metrics.average_entry_margin
                ) / metrics.accepted_entries
            metrics.max_concurrent_positions = max(metrics.max_concurrent_positions, len(active))
            if position.exit_at is None:
                metrics.unresolved_fail_closed += 1
                metrics.accounting_complete = False
            update_peaks()

    gross, net, equity, current_leverage = exposures()
    metrics.available_cash = available_cash
    metrics.reserved_capital = sum(item.margin for item in active.values())
    metrics.notional_exposure = gross
    metrics.gross_exposure = gross
    metrics.net_exposure = net
    metrics.leverage = current_leverage
    metrics.final_equity = equity
    return metrics
