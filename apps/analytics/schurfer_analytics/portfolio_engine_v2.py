# ruff: noqa: E501
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum

from .abnormal_flow_replay import DecisionFeatures, Outcome


class EventType(IntEnum):
    EXIT = 0
    ENTRY = 1


@dataclass(order=True)
class Event:
    at: datetime
    type: EventType
    # Tie-breaks: exit before entry (by Enum value 0 vs 1). Then by canonical_asset to be deterministic.
    canonical_asset: str
    decision: DecisionFeatures
    outcome: Outcome


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
    unresolved_positions: int = 0


def simulate_portfolio_v2(
    episodes: Sequence[DecisionFeatures],
    outcomes: dict[tuple[str, str, str, str, datetime], Outcome],
    outcome_horizon_minutes: int,
    initial_capital: float = 300.0,
    k_slots: int = 8,
    max_positions_per_asset: int = 1,
    fee_bps: float = 5.0,
) -> PortfolioMetrics:
    events = []
    horizon_delta = timedelta(minutes=outcome_horizon_minutes)

    for ep in episodes:
        key = ep.route_key()
        outcome = outcomes.get(key)
        if outcome is None or outcome.entry_price is None:
            # Unresolved position occupies capital / slots forever in this simulation
            # We add it as an entry but with no exit.
            # To handle this cleanly, we can just track unresolved count or simulate it
            events.append(
                Event(
                    ep.decision_at,
                    EventType.ENTRY,
                    ep.canonical_asset,
                    ep,
                    outcome
                    or Outcome(
                        ep.exchange,
                        ep.market_type,
                        ep.native_market_id,
                        ep.capture_version,
                        ep.symbol,
                        ep.decision_at,
                        None,
                        None,
                    ),
                )
            )
            continue

        events.append(Event(ep.decision_at, EventType.ENTRY, ep.canonical_asset, ep, outcome))
        # Exact chronological resolution: if exit happens at the exact same timestamp as another entry, EXIT resolves first.
        events.append(
            Event(ep.decision_at + horizon_delta, EventType.EXIT, ep.canonical_asset, ep, outcome)
        )

    events.sort()

    available_cash = initial_capital
    equity = initial_capital
    peak_equity = initial_capital

    positions: set[tuple[str, str, str, str, datetime]] = (
        set()
    )  # Store route_keys of active positions
    asset_counts: dict[str, int] = {}

    metrics = PortfolioMetrics(peak_equity=initial_capital)

    # Store cost basis to free capital properly
    margin_reserved = {}

    for event in events:
        rk = event.decision.route_key()
        if event.type == EventType.ENTRY:
            # Check limits
            if len(positions) >= k_slots:
                metrics.rejections_max_concurrent += 1
                continue

            if asset_counts.get(event.canonical_asset, 0) >= max_positions_per_asset:
                metrics.rejections_max_per_asset += 1
                continue

            slot_capital = equity / k_slots
            # We cannot allocate more than available cash
            allocation = min(slot_capital, available_cash)
            if allocation <= 0:
                metrics.rejections_insufficient_capital += 1
                continue

            available_cash -= allocation
            margin_reserved[rk] = allocation
            positions.add(rk)
            asset_counts[event.canonical_asset] = asset_counts.get(event.canonical_asset, 0) + 1
            metrics.max_concurrent_positions = max(metrics.max_concurrent_positions, len(positions))

            if event.outcome.entry_price is None:
                metrics.unresolved_positions += 1

        elif event.type == EventType.EXIT:
            if rk not in positions:
                continue  # Was rejected at entry

            allocation = margin_reserved.pop(rk)
            positions.remove(rk)
            asset_counts[event.canonical_asset] -= 1

            # Outcome has entry and exit prices
            entry_price = event.outcome.entry_price
            exit_price = event.outcome.exit_price
            if exit_price is None:
                metrics.unresolved_positions += 1
                # Unresolved positions hold capital/slots forever (do not restore positions or cash)
                # Since we already popped it from positions and margin_reserved, we must put it back!
                positions.add(rk)
                asset_counts[event.canonical_asset] += 1
                margin_reserved[rk] = allocation
                continue

            assert entry_price is not None
            gross_return = (exit_price - entry_price) / entry_price
            # Applying fee (assume applied on entry and exit)
            net_return = gross_return - (fee_bps * 2 / 10000.0)

            gross_pnl = allocation * gross_return
            net_pnl = allocation * net_return

            metrics.total_trades += 1
            if net_pnl > 0:
                metrics.winning_trades += 1
            else:
                metrics.losing_trades += 1

            metrics.gross_pnl += gross_pnl
            metrics.net_pnl += net_pnl

            equity += net_pnl
            available_cash += allocation + net_pnl

            if equity > peak_equity:
                peak_equity = equity
            else:
                drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
                if drawdown > metrics.max_drawdown_pct:
                    metrics.max_drawdown_pct = drawdown

    metrics.final_equity = equity
    return metrics
