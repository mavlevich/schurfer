"""SQLAlchemy adapter for deterministic episode-replay inputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from schurfer_journal.models import PumpEvent, TradeDecision, TradeDecisionOutcome
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .outcome_repository import async_database_url
from .replay import (
    MEASUREMENT_ONLY_STRATEGY_VERSION,
    ReplayDecision,
    ReplayFilters,
    ReplayOutcome,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, Sequence
    from datetime import datetime

    from sqlalchemy.sql import Select


def replay_inputs_statement(filters: ReplayFilters) -> Select[Any]:
    decisions = TradeDecision.__table__
    events = PumpEvent.__table__
    outcomes = TradeDecisionOutcome.__table__
    outcome_match = and_(
        outcomes.c.decision_id == decisions.c.decision_id,
        outcomes.c.resolver_version == filters.resolver_version,
        outcomes.c.horizon_minutes.in_(filters.required_horizons),
    )
    clauses = [decisions.c.ts < filters.until]
    if filters.since is not None:
        clauses.append(decisions.c.ts >= filters.since)
    if MEASUREMENT_ONLY_STRATEGY_VERSION not in filters.strategy_versions:
        clauses.append(
            or_(
                decisions.c.strategy_version.is_distinct_from(MEASUREMENT_ONLY_STRATEGY_VERSION),
                func.coalesce(
                    decisions.c.features.contains({"measurement_only": True}),
                    False,
                ).is_not(True),
            )
        )
    return (
        select(
            decisions.c.id.label("row_id"),
            decisions.c.decision_id,
            decisions.c.pump_event_id,
            events.c.base.label("event_base"),
            case(
                (
                    decisions.c.features["measurement_only"].as_boolean().is_(True),
                    events.c.first_seen_at,
                ),
                else_=func.coalesce(
                    events.c.entry_qualified_at,
                    events.c.first_seen_at,
                ),
            ).label("event_first_seen_at"),
            events.c.closed_at.label("event_closed_at"),
            decisions.c.ts,
            decisions.c.base,
            decisions.c.exchange,
            decisions.c.action,
            decisions.c.reason,
            decisions.c.score,
            decisions.c.pump_pct,
            decisions.c.price,
            decisions.c.strategy_version,
            decisions.c.features,
            decisions.c.liquidity,
            outcomes.c.horizon_minutes,
            outcomes.c.status.label("outcome_status"),
            outcomes.c.anchor_exchange,
            outcomes.c.source_exchange,
            outcomes.c.entry_price,
            outcomes.c.forward_price,
            outcomes.c.mfe_pct,
            outcomes.c.mae_pct,
            outcomes.c.short_return_pct,
            outcomes.c.coverage_ratio,
        )
        .select_from(
            decisions.outerjoin(events, events.c.id == decisions.c.pump_event_id).outerjoin(
                outcomes,
                outcome_match,
            )
        )
        .where(*clauses)
        .order_by(decisions.c.ts, decisions.c.id, outcomes.c.horizon_minutes)
    )


def _optional_float(value: Any) -> float | None:
    return float(value) if value is not None else None


@dataclass
class _DecisionBuilder:
    row_id: int
    decision_id: str | None
    pump_event_id: int | None
    event_base: str | None
    event_first_seen_at: datetime | None
    event_closed_at: datetime | None
    ts: datetime
    base: str
    exchange: str
    action: str
    reason: str
    score: int | None
    pump_pct: float | None
    price: float | None
    strategy_version: str | None
    features: dict[str, Any] | None
    liquidity: dict[str, Any] | None
    outcomes: list[ReplayOutcome] = field(default_factory=list)

    def freeze(self) -> ReplayDecision:
        return ReplayDecision(
            row_id=self.row_id,
            decision_id=self.decision_id,
            pump_event_id=self.pump_event_id,
            event_base=self.event_base,
            event_first_seen_at=self.event_first_seen_at,
            event_closed_at=self.event_closed_at,
            ts=self.ts,
            base=self.base,
            exchange=self.exchange,
            action=self.action,
            reason=self.reason,
            score=self.score,
            pump_pct=self.pump_pct,
            price=self.price,
            strategy_version=self.strategy_version,
            features=self.features,
            liquidity=self.liquidity,
            outcomes=tuple(sorted(self.outcomes, key=lambda item: item.horizon_minutes)),
        )


def _builder(row: Any) -> _DecisionBuilder:
    decision_id = row["decision_id"]
    return _DecisionBuilder(
        row_id=int(row["row_id"]),
        decision_id=str(decision_id) if decision_id is not None else None,
        pump_event_id=int(row["pump_event_id"]) if row["pump_event_id"] is not None else None,
        event_base=str(row["event_base"]) if row["event_base"] is not None else None,
        event_first_seen_at=row["event_first_seen_at"],
        event_closed_at=row["event_closed_at"],
        ts=row["ts"],
        base=str(row["base"]),
        exchange=str(row["exchange"]),
        action=str(row["action"]),
        reason=str(row["reason"]),
        score=int(row["score"]) if row["score"] is not None else None,
        pump_pct=_optional_float(row["pump_pct"]),
        price=_optional_float(row["price"]),
        strategy_version=(
            str(row["strategy_version"]) if row["strategy_version"] is not None else None
        ),
        features=row["features"] if isinstance(row["features"], dict) else None,
        liquidity=row["liquidity"] if isinstance(row["liquidity"], dict) else None,
    )


def _outcome(row: Any) -> ReplayOutcome | None:
    horizon = row["horizon_minutes"]
    if horizon is None:
        return None
    return ReplayOutcome(
        horizon_minutes=int(horizon),
        status=str(row["outcome_status"]),
        anchor_exchange=(
            str(row["anchor_exchange"]) if row["anchor_exchange"] is not None else None
        ),
        source_exchange=(
            str(row["source_exchange"]) if row["source_exchange"] is not None else None
        ),
        entry_price=_optional_float(row["entry_price"]),
        forward_price=_optional_float(row["forward_price"]),
        mfe_pct=_optional_float(row["mfe_pct"]),
        mae_pct=_optional_float(row["mae_pct"]),
        short_return_pct=_optional_float(row["short_return_pct"]),
        coverage_ratio=_optional_float(row["coverage_ratio"]),
    )


def map_replay_rows(rows: Sequence[Any]) -> list[ReplayDecision]:
    builders: dict[int, _DecisionBuilder] = {}
    for row in rows:
        row_id = int(row["row_id"])
        builder = builders.get(row_id)
        if builder is None:
            builder = _builder(row)
            builders[row_id] = builder
        outcome = _outcome(row)
        if outcome is not None:
            builder.outcomes.append(outcome)
    return [builder.freeze() for builder in builders.values()]


async def map_replay_row_stream(rows: AsyncIterable[Any]) -> list[ReplayDecision]:
    """Map ordered replay rows without retaining the raw SQL result set."""
    decisions: list[ReplayDecision] = []
    completed_row_ids: set[int] = set()
    builder: _DecisionBuilder | None = None
    current_row_id: int | None = None

    async for row in rows:
        row_id = int(row["row_id"])
        if current_row_id != row_id:
            if builder is not None and current_row_id is not None:
                decisions.append(builder.freeze())
                completed_row_ids.add(current_row_id)
            if row_id in completed_row_ids:
                raise ValueError("replay rows must be contiguous per decision")
            builder = _builder(row)
            current_row_id = row_id
        outcome = _outcome(row)
        if outcome is not None and builder is not None:
            builder.outcomes.append(outcome)

    if builder is not None:
        decisions.append(builder.freeze())
    return decisions


class ReplayRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, db_url: str) -> ReplayRepository:
        return cls(
            create_async_engine(
                async_database_url(db_url),
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
            )
        )

    async def load(self, filters: ReplayFilters) -> list[ReplayDecision]:
        async with self._engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                isolation_level="REPEATABLE READ",
                postgresql_readonly=True,
            )
            async with connection.begin():
                result = await connection.stream(
                    replay_inputs_statement(filters),
                    execution_options={"yield_per": 500},
                )
                return await map_replay_row_stream(result.mappings())

    async def close(self) -> None:
        await self._engine.dispose()
