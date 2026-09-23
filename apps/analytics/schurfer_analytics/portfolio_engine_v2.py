import enum
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime


class TradeDirection(enum.IntEnum):
    LONG = 1
    SHORT = -1


@dataclass
class PortfolioPosition:
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


class EventType(enum.IntEnum):
    EXIT = 0
    ENTRY = 1


@dataclass(order=True)
class Event:
    at: datetime
    type: EventType
    canonical_asset: str
    decision_id: str
    position: PortfolioPosition = field(compare=False)


@dataclass
class PortfolioMetrics:
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    final_equity: float = 0.0
    max_drawdown_pct: float = 0.0
    max_concurrent_positions: int = 0
    peak_equity: float = 0.0

    rejections_insufficient_capital: int = 0
    rejections_max_concurrent: int = 0
    rejections_max_per_asset: int = 0
    unresolved_fail_closed: int = 0

    available_cash: float = 0.0
    reserved_capital: float = 0.0
    notional_exposure: float = 0.0
    gross_exposure: float = 0.0
    net_exposure: float = 0.0
    leverage: float = 0.0

    rejected_entries: list[tuple[str, str]] = field(default_factory=list)  # (decision_id, reason)


def simulate_portfolio_v2(
    positions: Sequence[PortfolioPosition],
    initial_capital: float = 300.0,
    k_slots: int = 8,
    max_positions_per_asset: int = 1,
) -> PortfolioMetrics:
    events = []

    for pos in positions:
        events.append(
            Event(pos.entry_at, EventType.ENTRY, pos.canonical_asset, pos.decision_id, pos)
        )
        if pos.exit_at is not None:
            events.append(
                Event(pos.exit_at, EventType.EXIT, pos.canonical_asset, pos.decision_id, pos)
            )

    events.sort()

    available_cash = initial_capital
    peak_equity = initial_capital

    active_positions: dict[str, float] = {}  # decision_id -> allocated_capital
    asset_counts: dict[str, int] = {}

    metrics = PortfolioMetrics()

    for event in events:
        pos = event.position
        if event.type == EventType.ENTRY:
            if len(active_positions) >= k_slots:
                metrics.rejections_max_concurrent += 1
                metrics.rejected_entries.append((pos.decision_id, "max_concurrent_positions"))
                continue

            if asset_counts.get(pos.canonical_asset, 0) >= max_positions_per_asset:
                metrics.rejections_max_per_asset += 1
                metrics.rejected_entries.append((pos.decision_id, "max_positions_per_asset"))
                continue

            position_usd = initial_capital / k_slots

            if available_cash < position_usd:
                metrics.rejections_insufficient_capital += 1
                metrics.rejected_entries.append((pos.decision_id, "insufficient_capital"))
                continue

            available_cash -= position_usd
            active_positions[pos.decision_id] = position_usd
            asset_counts[pos.canonical_asset] = asset_counts.get(pos.canonical_asset, 0) + 1
            metrics.max_concurrent_positions = max(
                metrics.max_concurrent_positions, len(active_positions)
            )

            if pos.exit_at is None:
                metrics.unresolved_fail_closed += 1

        elif event.type == EventType.EXIT:
            if pos.decision_id not in active_positions:
                continue  # Was rejected

            allocation = active_positions.pop(pos.decision_id)
            asset_counts[pos.canonical_asset] -= 1

            if pos.net_return is not None and pos.gross_return is not None:
                net_pnl = allocation * pos.net_return
                gross_pnl = allocation * pos.gross_return

                metrics.total_trades += 1
                if net_pnl > 0:
                    metrics.winning_trades += 1
                else:
                    metrics.losing_trades += 1

                metrics.gross_pnl += gross_pnl
                metrics.net_pnl += net_pnl

                available_cash += allocation + net_pnl

                current_equity = available_cash + sum(active_positions.values())
                if current_equity > peak_equity:
                    peak_equity = current_equity
                else:
                    drawdown = (
                        (peak_equity - current_equity) / peak_equity if peak_equity > 0 else 0
                    )
                    if drawdown > metrics.max_drawdown_pct:
                        metrics.max_drawdown_pct = drawdown

    metrics.available_cash = available_cash
    metrics.reserved_capital = sum(active_positions.values())
    metrics.final_equity = metrics.available_cash + metrics.reserved_capital

    metrics.gross_exposure = metrics.reserved_capital
    metrics.notional_exposure = metrics.reserved_capital

    for pos_id, allocation in active_positions.items():
        for p in positions:
            if p.decision_id == pos_id:
                metrics.net_exposure += allocation * p.direction.value
                break

    if metrics.final_equity > 0:
        metrics.leverage = metrics.gross_exposure / metrics.final_equity

    return metrics
