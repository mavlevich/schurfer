"""SQLAlchemy persistence adapter for strategy-agnostic decision outcomes."""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol

from schurfer_journal.models import TradeDecision, TradeDecisionOutcome
from sqlalchemy import Integer, and_, column, func, or_, select, true, values
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .outcome_models import Decision, Outcome

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.dialects.postgresql import Insert
    from sqlalchemy.sql import Select


class OutcomeStore(Protocol):
    async def load_due_decisions(
        self,
        *,
        horizons: tuple[int, ...],
        resolver_version: str,
        retryable_statuses: tuple[str, ...],
        max_attempts: int,
        retry_after_seconds: int,
        batch_size: int,
    ) -> list[Decision]: ...

    async def persist_outcomes(
        self,
        outcomes: list[Outcome],
        *,
        resolver_version: str,
        timeframe_minutes: int,
    ) -> None: ...


def async_database_url(db_url: str) -> URL:
    """Use SQLAlchemy's async psycopg3 dialect for existing PostgreSQL URLs."""
    url = make_url(db_url)
    if url.drivername == "postgresql":
        return url.set(drivername="postgresql+psycopg")
    return url


def due_decisions_statement(
    *,
    horizons: tuple[int, ...],
    resolver_version: str,
    retryable_statuses: tuple[str, ...],
    max_attempts: int,
    retry_after_seconds: int,
    batch_size: int,
) -> Select[Any]:
    """Build the due-work query without embedding values in handwritten SQL."""
    decisions = TradeDecision.__table__
    outcomes = TradeDecisionOutcome.__table__
    horizon_values = values(
        column("horizon_minutes", Integer),
        name="horizons",
    ).data([(horizon,) for horizon in horizons])

    outcome_match = and_(
        outcomes.c.decision_id == decisions.c.decision_id,
        outcomes.c.horizon_minutes == horizon_values.c.horizon_minutes,
        outcomes.c.resolver_version == resolver_version,
    )
    due_at = decisions.c.ts + horizon_values.c.horizon_minutes * timedelta(minutes=1)
    retry_at = outcomes.c.updated_at + timedelta(seconds=retry_after_seconds)

    return (
        select(
            decisions.c.decision_id,
            decisions.c.ts,
            decisions.c.base,
            decisions.c.exchange,
            decisions.c.price,
            decisions.c.features,
            horizon_values.c.horizon_minutes,
        )
        .select_from(decisions.join(horizon_values, true()).outerjoin(outcomes, outcome_match))
        .where(
            decisions.c.decision_id.is_not(None),
            due_at <= func.now(),
            or_(
                outcomes.c.id.is_(None),
                and_(
                    outcomes.c.status.in_(retryable_statuses),
                    outcomes.c.attempt_count < max_attempts,
                    retry_at <= func.now(),
                ),
            ),
        )
        .order_by(decisions.c.ts, horizon_values.c.horizon_minutes)
        .limit(batch_size)
    )


def upsert_outcomes_statement(
    outcomes_to_write: list[Outcome],
    *,
    resolver_version: str,
    timeframe_minutes: int,
) -> Insert:
    """Build one PostgreSQL upsert for a bounded result batch."""
    table = TradeDecisionOutcome.__table__
    rows = [
        {
            **asdict(outcome),
            "resolver_version": resolver_version,
            "timeframe_minutes": timeframe_minutes,
            "attempt_count": 1,
            "resolved_at": func.now(),
            "created_at": func.now(),
            "updated_at": func.now(),
        }
        for outcome in outcomes_to_write
    ]
    statement = insert(TradeDecisionOutcome).values(rows)
    excluded = statement.excluded
    copied_columns = (
        "anchor_exchange",
        "source_exchange",
        "timeframe_minutes",
        "entry_price",
        "forward_price",
        "mfe_pct",
        "mae_pct",
        "short_return_pct",
        "bars_count",
        "expected_bars",
        "coverage_ratio",
        "status",
        "error",
    )
    return statement.on_conflict_do_update(
        constraint="uq_trade_decision_outcomes_decision_horizon_version",
        set_={
            **{name: excluded[name] for name in copied_columns},
            "attempt_count": table.c.attempt_count + 1,
            "resolved_at": func.now(),
            "updated_at": func.now(),
        },
    )


class OutcomeRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, db_url: str) -> OutcomeRepository:
        engine = create_async_engine(
            async_database_url(db_url),
            pool_pre_ping=True,
            pool_size=1,
            max_overflow=0,
        )
        return cls(engine)

    async def load_due_decisions(
        self,
        *,
        horizons: tuple[int, ...],
        resolver_version: str,
        retryable_statuses: tuple[str, ...],
        max_attempts: int,
        retry_after_seconds: int,
        batch_size: int,
    ) -> list[Decision]:
        statement = due_decisions_statement(
            horizons=horizons,
            resolver_version=resolver_version,
            retryable_statuses=retryable_statuses,
            max_attempts=max_attempts,
            retry_after_seconds=retry_after_seconds,
            batch_size=batch_size,
        )
        async with self._engine.connect() as connection:
            result = await connection.execute(statement)
            rows = result.all()

        grouped: dict[
            str,
            tuple[datetime, str, str, float | None, dict[str, Any] | None, list[int]],
        ] = {}
        for decision_id, ts, base, exchange, price, features, horizon in rows:
            key = str(decision_id)
            if key not in grouped:
                grouped[key] = (
                    ts,
                    base,
                    exchange,
                    float(price) if price is not None else None,
                    features,
                    [],
                )
            grouped[key][5].append(int(horizon))
        return [
            Decision(key, ts, base, exchange, price, features, tuple(sorted(horizons_for_row)))
            for key, (
                ts,
                base,
                exchange,
                price,
                features,
                horizons_for_row,
            ) in grouped.items()
        ]

    async def persist_outcomes(
        self,
        outcomes: list[Outcome],
        *,
        resolver_version: str,
        timeframe_minutes: int,
    ) -> None:
        if not outcomes:
            return
        statement = upsert_outcomes_statement(
            outcomes,
            resolver_version=resolver_version,
            timeframe_minutes=timeframe_minutes,
        )
        async with self._engine.begin() as connection:
            await connection.execute(statement)

    async def close(self) -> None:
        await self._engine.dispose()
