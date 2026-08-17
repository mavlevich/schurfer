"""Postgres adapter for the prospective momentum-flow WATCH worker."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    Column,
    DateTime,
    Double,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    Uuid,
    and_,
    desc,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from .momentum_flow_watch_evaluator import (
    SymbolWatchState,
    WatchBar,
    WatchEvaluation,
)
from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from .momentum_flow_watch_contract import WatchContract

_metadata = MetaData()

_bars = Table(
    "bybit_momentum_bars_1m",
    _metadata,
    Column("exchange", String),
    Column("market_type", String),
    Column("symbol", String),
    Column("capture_version", String),
    Column("bucket_start", DateTime(timezone=True)),
    Column("universe_version", String),
    Column("close_price", Double),
    Column("buy_total_notional_usd", Double),
    Column("sell_total_notional_usd", Double),
    Column("open_interest", Double),
    Column("open_interest_event_at", DateTime(timezone=True)),
    Column("open_interest_observed_at", DateTime(timezone=True)),
    Column("last_trade_event_at", DateTime(timezone=True)),
    Column("last_trade_received_at", DateTime(timezone=True)),
    Column("last_ticker_event_at", DateTime(timezone=True)),
    Column("last_ticker_received_at", DateTime(timezone=True)),
    Column("unbackfilled_gap_minutes", Integer),
    Column("complete", Boolean),
    Column("created_at", DateTime(timezone=True)),
    schema="timeseries",
)

_runs = Table(
    "momentum_flow_watch_runs",
    _metadata,
    Column("watch_version", String),
    Column("contract_sha256", String),
    Column("contract_json", JSONB),
    Column("cohort_started_at", DateTime(timezone=True)),
    Column("last_bucket_start", DateTime(timezone=True)),
    Column("status", String),
    Column("updated_at", DateTime(timezone=True)),
    schema="app",
)

_evaluations = Table(
    "momentum_flow_watch_evaluations_1m",
    _metadata,
    Column("exchange", String),
    Column("market_type", String),
    Column("symbol", String),
    Column("capture_version", String),
    Column("watch_version", String),
    Column("bucket_start", DateTime(timezone=True)),
    Column("universe_version", String),
    Column("quality_ready", Boolean),
    Column("raw_qualified", Boolean),
    Column("decision_status", String),
    Column("reason_codes", ARRAY(Text)),
    Column("price_return_60m_pct", Double),
    Column("price_return_15m_pct", Double),
    Column("oi_growth_60m_pct", Double),
    Column("buy_notional_15m_usd", Double),
    Column("sell_notional_15m_usd", Double),
    Column("flow_notional_15m_usd", Double),
    Column("buy_imbalance_15m", Double),
    Column("flow_acceleration_15m_vs_prior_45m", Double),
    Column("cross_section_size", Integer),
    Column("oi_growth_threshold_pct", Double),
    Column("buy_imbalance_threshold", Double),
    Column("flow_acceleration_threshold", Double),
    Column("source_event_at", DateTime(timezone=True)),
    Column("source_received_at", DateTime(timezone=True)),
    Column("bucket_ready_at", DateTime(timezone=True)),
    Column("evaluator_started_at", DateTime(timezone=True)),
    Column("evaluator_completed_at", DateTime(timezone=True)),
    Column("decision_at", DateTime(timezone=True)),
    Column("episode_id", Uuid),
    Column("watch_id", Uuid),
    Column("state_active_after", Boolean),
    Column("state_clear_streak_after", Integer),
    Column("state_last_watch_at_after", DateTime(timezone=True)),
    Column("input_hash", LargeBinary),
    schema="timeseries",
)

_states = Table(
    "momentum_flow_watch_states",
    _metadata,
    Column("watch_version", String),
    Column("exchange", String),
    Column("market_type", String),
    Column("symbol", String),
    Column("active_episode", Boolean),
    Column("clear_streak", Integer),
    Column("last_watch_at", DateTime(timezone=True)),
    Column("episode_id", Uuid),
    Column("last_bucket_start", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
    schema="app",
)


@dataclass(frozen=True)
class WatchRun:
    watch_version: str
    contract_sha256: str
    contract_json: dict[str, Any]
    cohort_started_at: datetime
    last_bucket_start: datetime | None
    status: str


@dataclass(frozen=True)
class WatchBucketInput:
    bucket_start: datetime
    universe_version: str
    symbols: tuple[str, ...]
    bars_by_symbol: dict[str, tuple[WatchBar, ...]]


@dataclass(frozen=True)
class EvaluationWrite:
    evaluation: WatchEvaluation
    state_after: SymbolWatchState
    state_changed: bool
    evaluator_started_at: datetime
    evaluator_completed_at: datetime
    decision_at: datetime


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _canonical_input_hash(row: dict[str, Any]) -> bytes:
    excluded = {
        "evaluator_started_at",
        "evaluator_completed_at",
        "decision_at",
        "input_hash",
    }
    payload = {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in row.items()
        if key not in excluded
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode()).digest()


def evaluation_row(write: EvaluationWrite, contract: WatchContract) -> dict[str, Any]:
    evaluation = write.evaluation
    features = asdict(evaluation.features) if evaluation.features is not None else {}
    thresholds = evaluation.thresholds
    row: dict[str, Any] = {
        "exchange": contract.source_exchange,
        "market_type": contract.market_type,
        "symbol": evaluation.symbol,
        "capture_version": contract.capture_version,
        "watch_version": contract.watch_version,
        "bucket_start": evaluation.bucket_start,
        "universe_version": evaluation.universe_version,
        "quality_ready": evaluation.quality_ready,
        "raw_qualified": evaluation.raw_qualified,
        "decision_status": evaluation.decision_status,
        "reason_codes": list(evaluation.reason_codes),
        "price_return_60m_pct": features.get("price_return_60m_pct"),
        "price_return_15m_pct": features.get("price_return_15m_pct"),
        "oi_growth_60m_pct": features.get("oi_growth_60m_pct"),
        "buy_notional_15m_usd": features.get("buy_notional_15m_usd"),
        "sell_notional_15m_usd": features.get("sell_notional_15m_usd"),
        "flow_notional_15m_usd": features.get("flow_notional_15m_usd"),
        "buy_imbalance_15m": features.get("buy_imbalance_15m"),
        "flow_acceleration_15m_vs_prior_45m": features.get("flow_acceleration_15m_vs_prior_45m"),
        "cross_section_size": thresholds.sample_size,
        "oi_growth_threshold_pct": thresholds.oi_growth_60m_pct,
        "buy_imbalance_threshold": thresholds.buy_imbalance_15m,
        "flow_acceleration_threshold": (thresholds.flow_acceleration_15m_vs_prior_45m),
        "source_event_at": evaluation.source_event_at,
        "source_received_at": evaluation.source_received_at,
        "bucket_ready_at": evaluation.bucket_ready_at,
        "evaluator_started_at": write.evaluator_started_at,
        "evaluator_completed_at": write.evaluator_completed_at,
        "decision_at": write.decision_at,
        "episode_id": evaluation.episode_id,
        "watch_id": evaluation.watch_id,
        "state_active_after": write.state_after.active_episode,
        "state_clear_streak_after": write.state_after.clear_streak,
        "state_last_watch_at_after": write.state_after.last_watch_at,
    }
    row["input_hash"] = _canonical_input_hash(row)
    return row


class MomentumFlowWatchRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._lock_connection: AsyncConnection | None = None

    @classmethod
    def from_url(cls, database_url: str) -> MomentumFlowWatchRepository:
        return cls(
            create_async_engine(
                async_database_url(database_url),
                pool_pre_ping=True,
                pool_size=2,
                max_overflow=0,
            )
        )

    async def acquire_worker_lock(self, watch_version: str) -> bool:
        if self._lock_connection is not None:
            return True
        connection = await self._engine.connect()
        result = await connection.execute(
            select(func.pg_try_advisory_lock(func.hashtext(watch_version)))
        )
        if not bool(result.scalar_one()):
            await connection.close()
            return False
        self._lock_connection = connection
        return True

    async def register_run(
        self,
        *,
        contract: WatchContract,
        contract_sha256: str,
        now: datetime,
    ) -> WatchRun:
        statement = insert(_runs).values(
            watch_version=contract.watch_version,
            contract_sha256=contract_sha256,
            contract_json=json.loads(contract.canonical_json()),
            cohort_started_at=_utc(now),
            status="active",
        )
        async with self._engine.begin() as connection:
            await connection.execute(
                statement.on_conflict_do_nothing(index_elements=[_runs.c.watch_version])
            )
            result = await connection.execute(
                select(
                    _runs.c.watch_version,
                    _runs.c.contract_sha256,
                    _runs.c.contract_json,
                    _runs.c.cohort_started_at,
                    _runs.c.last_bucket_start,
                    _runs.c.status,
                ).where(_runs.c.watch_version == contract.watch_version)
            )
            row = result.one()
        run = WatchRun(*row)
        if run.contract_sha256 != contract_sha256:
            raise RuntimeError("stored momentum WATCH contract hash does not match this binary")
        if run.contract_json != json.loads(contract.canonical_json()):
            raise RuntimeError("stored momentum WATCH contract JSON does not match this binary")
        if run.status != "active":
            raise RuntimeError(f"momentum WATCH run is {run.status}")
        return run

    async def due_buckets(
        self,
        *,
        contract: WatchContract,
        cohort_started_at: datetime,
        limit: int,
    ) -> tuple[datetime, ...]:
        last_result = await self._execute(
            select(_runs.c.last_bucket_start).where(_runs.c.watch_version == contract.watch_version)
        )
        last_bucket = last_result.scalar_one_or_none()
        lower = max(
            _utc(cohort_started_at).replace(second=0, microsecond=0) + timedelta(minutes=1),
            _utc(last_bucket) + timedelta(minutes=1)
            if last_bucket is not None
            else datetime.min.replace(tzinfo=UTC),
        )
        statement = (
            select(_bars.c.bucket_start)
            .where(
                _bars.c.exchange == contract.source_exchange,
                _bars.c.market_type == contract.market_type,
                _bars.c.capture_version == contract.capture_version,
                _bars.c.bucket_start >= lower,
                _bars.c.bucket_start
                <= func.now()
                - timedelta(seconds=60 + min(contract.max_bucket_decision_delay_seconds // 4, 30)),
            )
            .distinct()
            .order_by(_bars.c.bucket_start)
            .limit(limit)
        )
        result = await self._execute(statement)
        return tuple(_utc(row[0]) for row in result.all())

    async def load_bucket(
        self,
        *,
        contract: WatchContract,
        bucket_start: datetime,
    ) -> WatchBucketInput | None:
        target = _utc(bucket_start)
        universe_result = await self._execute(
            select(_bars.c.universe_version, func.count())
            .where(
                _bars.c.exchange == contract.source_exchange,
                _bars.c.market_type == contract.market_type,
                _bars.c.capture_version == contract.capture_version,
                _bars.c.bucket_start == target,
            )
            .group_by(_bars.c.universe_version)
            .order_by(desc(func.count()), _bars.c.universe_version)
            .limit(1)
        )
        universe = universe_result.first()
        if universe is None:
            return None
        universe_version = str(universe[0])
        since = target - timedelta(minutes=contract.lookback_minutes)
        statement = (
            select(
                _bars.c.symbol,
                _bars.c.universe_version,
                _bars.c.bucket_start,
                _bars.c.created_at,
                _bars.c.close_price,
                _bars.c.buy_total_notional_usd,
                _bars.c.sell_total_notional_usd,
                _bars.c.open_interest,
                _bars.c.open_interest_event_at,
                _bars.c.open_interest_observed_at,
                _bars.c.last_trade_event_at,
                _bars.c.last_trade_received_at,
                _bars.c.last_ticker_event_at,
                _bars.c.last_ticker_received_at,
                _bars.c.unbackfilled_gap_minutes,
                _bars.c.complete,
            )
            .where(
                _bars.c.exchange == contract.source_exchange,
                _bars.c.market_type == contract.market_type,
                _bars.c.capture_version == contract.capture_version,
                _bars.c.bucket_start >= since,
                _bars.c.bucket_start <= target,
            )
            .order_by(_bars.c.symbol, _bars.c.bucket_start)
        )
        result = await self._execute(statement)
        grouped: dict[str, list[WatchBar]] = {}
        for row in result.all():
            bar = WatchBar(
                symbol=str(row.symbol),
                universe_version=str(row.universe_version),
                bucket_start=_utc(row.bucket_start),
                created_at=_utc(row.created_at),
                close_price=float(row.close_price) if row.close_price is not None else None,
                buy_total_notional_usd=float(row.buy_total_notional_usd or 0.0),
                sell_total_notional_usd=float(row.sell_total_notional_usd or 0.0),
                open_interest=(float(row.open_interest) if row.open_interest is not None else None),
                open_interest_event_at=row.open_interest_event_at,
                open_interest_observed_at=row.open_interest_observed_at,
                last_trade_event_at=row.last_trade_event_at,
                last_trade_received_at=row.last_trade_received_at,
                last_ticker_event_at=row.last_ticker_event_at,
                last_ticker_received_at=row.last_ticker_received_at,
                unbackfilled_gap_minutes=int(row.unbackfilled_gap_minutes or 0),
                complete=bool(row.complete),
            )
            grouped.setdefault(bar.symbol, []).append(bar)
        symbols = tuple(sorted(grouped))
        return WatchBucketInput(
            bucket_start=target,
            universe_version=universe_version,
            symbols=symbols,
            bars_by_symbol={symbol: tuple(grouped[symbol]) for symbol in symbols},
        )

    async def has_any_recent_valid_price(
        self,
        *,
        contract: WatchContract,
        lookback_minutes: int,
    ) -> bool:
        """True if at least one COMPLETE bar with a positive close_price
        exists for this contract's own (exchange, market_type,
        capture_version) in the last lookback_minutes -- see
        momentum_flow_producer_readiness's own doc comment for why this
        exists. Requires close_price > 0 (not just NOT NULL: a zero or
        negative price would pass a bare non-NULL check while still being
        garbage) and complete = true (a still-forming or gap-marked
        synthetic bar is not evidence the producer can feed a real
        decision).

        Deliberately named for exactly what it checks, not more: this is
        a LIMIT 1 existence check against ONE symbol somewhere in the
        whole captured universe, not a coverage ratio. A producer that
        feeds valid price to one symbol out of hundreds still passes this
        -- it answers "is price capability present at all" (the binary
        failure mode the 2026-08-15..17 incident actually was: Binance
        had ZERO valid prices anywhere), not "is the full cross-section/
        OI ready for a real decision." That stronger, coverage-aware
        readiness question belongs to a future PR4 coverage gate (see
        docs/research/binance-watch-input-readiness-v1.md), not this
        method -- do not read a True here as "the producer is fully
        healthy."""
        since = datetime.now(UTC) - timedelta(minutes=lookback_minutes)
        result = await self._execute(
            select(_bars.c.symbol)
            .where(
                _bars.c.exchange == contract.source_exchange,
                _bars.c.market_type == contract.market_type,
                _bars.c.capture_version == contract.capture_version,
                _bars.c.bucket_start >= since,
                _bars.c.close_price > 0,
                _bars.c.complete.is_(True),
            )
            .limit(1)
        )
        return result.first() is not None

    async def load_states(
        self,
        *,
        contract: WatchContract,
    ) -> dict[str, SymbolWatchState]:
        statement = (
            select(
                _states.c.symbol,
                _states.c.active_episode,
                _states.c.clear_streak,
                _states.c.last_watch_at,
                _states.c.episode_id,
            )
            .where(
                _states.c.watch_version == contract.watch_version,
                _states.c.exchange == contract.source_exchange,
                _states.c.market_type == contract.market_type,
            )
            .order_by(_states.c.symbol)
        )
        result = await self._execute(statement)
        return {
            str(row.symbol): SymbolWatchState(
                active_episode=bool(row.active_episode),
                clear_streak=int(row.clear_streak),
                last_watch_at=row.last_watch_at,
                episode_id=row.episode_id,
            )
            for row in result.all()
        }

    async def persist_bucket(
        self,
        writes: tuple[EvaluationWrite, ...],
        *,
        contract: WatchContract,
    ) -> None:
        if not writes:
            return
        rows = [evaluation_row(write, contract) for write in writes]
        symbols = [write.evaluation.symbol for write in writes]
        if len(set(symbols)) != len(symbols):
            raise ValueError("persist_bucket requires one evaluation per symbol")
        bucket_start = writes[0].evaluation.bucket_start
        if any(write.evaluation.bucket_start != bucket_start for write in writes):
            raise ValueError("persist_bucket requires one bucket")
        statement = (
            insert(_evaluations)
            .values(rows)
            .on_conflict_do_nothing(
                index_elements=[
                    _evaluations.c.exchange,
                    _evaluations.c.market_type,
                    _evaluations.c.symbol,
                    _evaluations.c.watch_version,
                    _evaluations.c.bucket_start,
                ]
            )
        )
        state_rows = [
            {
                "watch_version": contract.watch_version,
                "exchange": contract.source_exchange,
                "market_type": contract.market_type,
                "symbol": write.evaluation.symbol,
                "active_episode": write.state_after.active_episode,
                "clear_streak": write.state_after.clear_streak,
                "last_watch_at": write.state_after.last_watch_at,
                "episode_id": write.state_after.episode_id,
                "last_bucket_start": write.evaluation.bucket_start,
                "updated_at": write.evaluator_completed_at,
            }
            for write in writes
            if write.state_changed
        ]
        state_upsert = None
        if state_rows:
            state_insert = insert(_states).values(state_rows)
            state_upsert = state_insert.on_conflict_do_update(
                index_elements=[
                    _states.c.watch_version,
                    _states.c.exchange,
                    _states.c.market_type,
                    _states.c.symbol,
                ],
                set_={
                    "active_episode": state_insert.excluded.active_episode,
                    "clear_streak": state_insert.excluded.clear_streak,
                    "last_watch_at": state_insert.excluded.last_watch_at,
                    "episode_id": state_insert.excluded.episode_id,
                    "last_bucket_start": state_insert.excluded.last_bucket_start,
                    "updated_at": state_insert.excluded.updated_at,
                },
                where=_states.c.last_bucket_start <= state_insert.excluded.last_bucket_start,
            )
        expected = {str(row["symbol"]): bytes(row["input_hash"]) for row in rows}
        async with self._engine.begin() as connection:
            await connection.execute(statement)
            existing = await connection.execute(
                select(_evaluations.c.symbol, _evaluations.c.input_hash).where(
                    and_(
                        _evaluations.c.exchange == contract.source_exchange,
                        _evaluations.c.market_type == contract.market_type,
                        _evaluations.c.watch_version == contract.watch_version,
                        _evaluations.c.bucket_start == bucket_start,
                        _evaluations.c.symbol.in_(symbols),
                    )
                )
            )
            actual = {str(symbol): bytes(input_hash) for symbol, input_hash in existing.all()}
            if actual != expected:
                raise RuntimeError("momentum WATCH idempotency hash mismatch")
            if state_upsert is not None:
                await connection.execute(state_upsert)
            await connection.execute(
                update(_runs)
                .where(_runs.c.watch_version == contract.watch_version)
                .values(
                    last_bucket_start=func.greatest(
                        func.coalesce(_runs.c.last_bucket_start, bucket_start),
                        bucket_start,
                    ),
                    updated_at=max(write.evaluator_completed_at for write in writes),
                )
            )

    async def _execute(self, statement: Any) -> Any:
        async with self._engine.connect() as connection:
            return await connection.execute(statement)

    async def close(self) -> None:
        if self._lock_connection is not None:
            await self._lock_connection.close()
            self._lock_connection = None
        await self._engine.dispose()
