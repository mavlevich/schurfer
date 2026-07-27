"""SQLAlchemy persistence for derivatives context probes and durable resolution."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from schurfer_journal.models import (
    PumpDerivativesContextRun,
    PumpDerivativesContextSample,
    PumpEvent,
    PumpEventSource,
)
from sqlalchemy import String, and_, column, func, or_, select, values
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .derivatives_context import (
    DerivativesContextObservation,
    DerivativesContextTarget,
    DerivativesContextWork,
)
from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from sqlalchemy.dialects.postgresql import Insert
    from sqlalchemy.sql import Select
    from sqlalchemy.sql.dml import ReturningInsert


class DerivativesContextStore(Protocol):
    async def load_due_work(
        self,
        *,
        supported_pairs: tuple[tuple[str, str], ...],
        resolver_version: str,
        cohort_start: datetime,
        after_minutes: int,
        retryable_statuses: tuple[str, ...],
        max_attempts: int,
        retry_after_seconds: int,
        batch_size: int,
    ) -> tuple[DerivativesContextWork, ...]: ...

    async def persist_observations(
        self,
        observations: tuple[DerivativesContextObservation, ...],
        *,
        resolver_version: str,
        ccxt_version: str,
    ) -> None: ...


def _support_label(value: bool | str) -> str:
    if value == "emulated":
        return "emulated"
    return "native" if value is True else "unsupported"


def latest_targets_statement(
    exchanges: tuple[str, ...],
    *,
    since: datetime,
    until: datetime,
    after_minutes: int,
) -> Select[Any]:
    anchor_at = func.coalesce(
        PumpEvent.entry_qualified_at,
        PumpEvent.first_seen_at,
    )
    complete_before = until - timedelta(minutes=after_minutes)
    ranked = (
        select(
            PumpEventSource.event_id.label("event_id"),
            PumpEventSource.exchange.label("exchange"),
            PumpEvent.base.label("base"),
            PumpEventSource.unified_symbol.label("unified_symbol"),
            PumpEventSource.market_id.label("market_id"),
            PumpEventSource.identity_key.label("identity_key"),
            anchor_at.label("anchor_at"),
            func.row_number()
            .over(
                partition_by=PumpEventSource.exchange,
                order_by=(
                    anchor_at.desc(),
                    PumpEventSource.last_seen_at.desc(),
                    PumpEventSource.id.desc(),
                ),
            )
            .label("target_rank"),
        )
        .join(PumpEvent, PumpEvent.id == PumpEventSource.event_id)
        .where(
            PumpEventSource.exchange.in_(exchanges),
            PumpEventSource.unified_symbol.is_not(None),
            PumpEventSource.market_id.is_not(None),
            PumpEventSource.identity_key.is_not(None),
            PumpEventSource.identity_conflict.is_(False),
            anchor_at >= since,
            anchor_at < complete_before,
        )
        .subquery("ranked_derivatives_context_targets")
    )
    return (
        select(
            ranked.c.event_id,
            ranked.c.exchange,
            ranked.c.base,
            ranked.c.unified_symbol,
            ranked.c.market_id,
            ranked.c.identity_key,
            ranked.c.anchor_at,
        )
        .where(ranked.c.target_rank == 1)
        .order_by(ranked.c.exchange)
    )


def due_context_work_statement(
    *,
    supported_pairs: tuple[tuple[str, str], ...],
    resolver_version: str,
    cohort_start: datetime,
    after_minutes: int,
    retryable_statuses: tuple[str, ...],
    max_attempts: int,
    retry_after_seconds: int,
    batch_size: int,
) -> Select[Any]:
    """Build bounded point-in-time work selection for validated venue/method pairs."""
    sources = PumpEventSource.__table__
    events = PumpEvent.__table__
    runs = PumpDerivativesContextRun.__table__
    pair_values = values(
        column("exchange", String(32)),
        column("method", String(40)),
        name="supported_derivatives_pairs",
    ).data(supported_pairs)
    anchor_at = func.coalesce(events.c.entry_qualified_at, events.c.first_seen_at)
    run_match = and_(
        runs.c.event_id == sources.c.event_id,
        runs.c.exchange == sources.c.exchange,
        runs.c.method == pair_values.c.method,
        runs.c.resolver_version == resolver_version,
    )
    retry_at = runs.c.updated_at + timedelta(seconds=retry_after_seconds)

    return (
        select(
            sources.c.event_id,
            sources.c.exchange,
            events.c.base,
            sources.c.unified_symbol,
            sources.c.market_id,
            sources.c.identity_key,
            anchor_at.label("anchor_at"),
            pair_values.c.method,
        )
        .select_from(
            sources.join(events, events.c.id == sources.c.event_id)
            .join(pair_values, pair_values.c.exchange == sources.c.exchange)
            .outerjoin(runs, run_match)
        )
        .where(
            sources.c.unified_symbol.is_not(None),
            sources.c.market_id.is_not(None),
            sources.c.identity_key.is_not(None),
            sources.c.identity_conflict.is_(False),
            anchor_at >= cohort_start,
            anchor_at + timedelta(minutes=after_minutes) <= func.now(),
            or_(
                runs.c.id.is_(None),
                and_(
                    runs.c.status.in_(retryable_statuses),
                    runs.c.attempt_count < max_attempts,
                    retry_at <= func.now(),
                ),
            ),
        )
        .order_by(anchor_at, sources.c.event_id, sources.c.exchange, pair_values.c.method)
        .limit(batch_size)
    )


def upsert_context_run_statement(
    observation: DerivativesContextObservation,
    *,
    resolver_version: str,
    ccxt_version: str,
) -> ReturningInsert[Any]:
    """Build an idempotent run upsert that preserves the retry count."""
    result = observation.result
    if (
        result.event_id is None
        or result.anchor_at is None
        or result.unified_symbol is None
        or result.requested_since is None
        or result.requested_until is None
    ):
        raise ValueError("durable derivatives context requires a complete target")
    table = PumpDerivativesContextRun.__table__
    statement = insert(PumpDerivativesContextRun).values(
        event_id=result.event_id,
        exchange=result.exchange,
        method=result.method,
        capability=result.capability,
        declared_support=_support_label(result.declared_support),
        resolver_version=resolver_version,
        unified_symbol=result.unified_symbol,
        market_id=result.market_id,
        identity_key=result.identity_key,
        anchor_at=result.anchor_at,
        requested_since=result.requested_since,
        requested_until=result.requested_until,
        timeframe=result.effective_timeframe,
        request_limit=result.effective_limit,
        request_count=result.request_count,
        returned_rows=result.returned_rows,
        valid_timestamp_rows=result.valid_timestamp_rows,
        in_window_rows=result.in_window_rows,
        invalid_rows=result.invalid_rows,
        expected_rows=result.expected_rows,
        coverage_ratio=result.coverage_ratio,
        covers_start=result.covers_start,
        covers_end=result.covers_end,
        missing_rows=result.missing_rows,
        duplicate_rows=result.duplicate_rows,
        max_gap_minutes=result.max_gap_minutes,
        pagination_exhausted=result.pagination_exhausted,
        status=result.status,
        attempt_count=1,
        error=result.error,
        ccxt_version=ccxt_version,
        resolved_at=result.fetched_at,
        created_at=func.now(),
        updated_at=func.now(),
    )
    excluded = statement.excluded
    copied_columns = (
        "capability",
        "declared_support",
        "unified_symbol",
        "market_id",
        "identity_key",
        "anchor_at",
        "requested_since",
        "requested_until",
        "timeframe",
        "request_limit",
        "request_count",
        "returned_rows",
        "valid_timestamp_rows",
        "in_window_rows",
        "invalid_rows",
        "expected_rows",
        "coverage_ratio",
        "covers_start",
        "covers_end",
        "missing_rows",
        "duplicate_rows",
        "max_gap_minutes",
        "pagination_exhausted",
        "status",
        "error",
        "ccxt_version",
        "resolved_at",
    )
    return statement.on_conflict_do_update(
        constraint="uq_pump_derivatives_context_run",
        set_={
            **{name: excluded[name] for name in copied_columns},
            "attempt_count": table.c.attempt_count + 1,
            "updated_at": func.now(),
        },
    ).returning(table.c.id)


def upsert_context_samples_statement(
    run_id: int,
    observation: DerivativesContextObservation,
) -> Insert:
    """Build one idempotent sample upsert for a resolved context run."""
    rows = [
        {
            **asdict(sample),
            "run_id": run_id,
        }
        for sample in observation.samples
    ]
    statement = insert(PumpDerivativesContextSample).values(rows)
    return statement.on_conflict_do_update(
        constraint="uq_pump_derivatives_context_sample",
        set_={
            "source_at": statement.excluded.source_at,
            "payload": statement.excluded.payload,
        },
    )


class DerivativesContextRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, db_url: str) -> DerivativesContextRepository:
        return cls(
            create_async_engine(
                async_database_url(db_url),
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
            )
        )

    async def load_latest_targets(
        self,
        exchanges: tuple[str, ...],
        *,
        since: datetime,
        until: datetime,
        after_minutes: int,
    ) -> tuple[DerivativesContextTarget, ...]:
        statement = latest_targets_statement(
            exchanges,
            since=since,
            until=until,
            after_minutes=after_minutes,
        )
        async with self._engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                isolation_level="REPEATABLE READ",
                postgresql_readonly=True,
            )
            async with connection.begin():
                result = await connection.execute(statement)
        return tuple(
            DerivativesContextTarget(
                event_id=int(row["event_id"]),
                exchange=str(row["exchange"]),
                base=str(row["base"]),
                unified_symbol=str(row["unified_symbol"]),
                market_id=(str(row["market_id"]) if row["market_id"] is not None else None),
                identity_key=(
                    str(row["identity_key"]) if row["identity_key"] is not None else None
                ),
                anchor_at=row["anchor_at"],
            )
            for row in result.mappings().all()
        )

    async def load_due_work(
        self,
        *,
        supported_pairs: tuple[tuple[str, str], ...],
        resolver_version: str,
        cohort_start: datetime,
        after_minutes: int,
        retryable_statuses: tuple[str, ...],
        max_attempts: int,
        retry_after_seconds: int,
        batch_size: int,
    ) -> tuple[DerivativesContextWork, ...]:
        if not supported_pairs:
            return ()
        statement = due_context_work_statement(
            supported_pairs=supported_pairs,
            resolver_version=resolver_version,
            cohort_start=cohort_start,
            after_minutes=after_minutes,
            retryable_statuses=retryable_statuses,
            max_attempts=max_attempts,
            retry_after_seconds=retry_after_seconds,
            batch_size=batch_size,
        )
        async with self._engine.connect() as connection:
            result = await connection.execute(statement)
        return tuple(
            DerivativesContextWork(
                target=DerivativesContextTarget(
                    event_id=int(row["event_id"]),
                    exchange=str(row["exchange"]),
                    base=str(row["base"]),
                    unified_symbol=str(row["unified_symbol"]),
                    market_id=(str(row["market_id"]) if row["market_id"] is not None else None),
                    identity_key=(
                        str(row["identity_key"]) if row["identity_key"] is not None else None
                    ),
                    anchor_at=row["anchor_at"],
                ),
                method=str(row["method"]),
            )
            for row in result.mappings().all()
        )

    async def persist_observations(
        self,
        observations: tuple[DerivativesContextObservation, ...],
        *,
        resolver_version: str,
        ccxt_version: str,
    ) -> None:
        if not observations:
            return
        async with self._engine.begin() as connection:
            for observation in observations:
                run_result = await connection.execute(
                    upsert_context_run_statement(
                        observation,
                        resolver_version=resolver_version,
                        ccxt_version=ccxt_version,
                    )
                )
                run_id = int(run_result.scalar_one())
                if observation.samples:
                    await connection.execute(upsert_context_samples_statement(run_id, observation))

    async def close(self) -> None:
        await self._engine.dispose()
