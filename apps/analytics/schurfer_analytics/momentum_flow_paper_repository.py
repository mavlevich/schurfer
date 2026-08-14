"""Postgres adapter for the prospective momentum-flow paper probe."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

from schurfer_performance import AccountingResult, calculate_performance
from sqlalchemy import (
    Column,
    DateTime,
    Double,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    Uuid,
    exists,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from uuid import UUID

    from .momentum_flow_paper_contract import PaperContract
    from .momentum_flow_paper_market import ExecutableQuote, QuoteFailure

_metadata = MetaData()

_watch_evaluations = Table(
    "momentum_flow_watch_evaluations_1m",
    _metadata,
    Column("exchange", String),
    Column("market_type", String),
    Column("symbol", String),
    Column("watch_version", String),
    Column("bucket_start", DateTime(timezone=True)),
    Column("decision_status", String),
    Column("decision_at", DateTime(timezone=True)),
    Column("episode_id", Uuid),
    Column("watch_id", Uuid),
    schema="timeseries",
)

_runs = Table(
    "momentum_flow_paper_runs",
    _metadata,
    Column("paper_version", String),
    Column("contract_sha256", String),
    Column("contract_json", JSONB),
    Column("cohort_started_at", DateTime(timezone=True)),
    Column("status", String),
    schema="app",
)

_probes = Table(
    "momentum_flow_paper_probes",
    _metadata,
    Column("paper_id", Uuid),
    Column("paper_version", String),
    Column("watch_version", String),
    Column("watch_id", Uuid),
    Column("episode_id", Uuid),
    Column("exchange", String),
    Column("market_type", String),
    Column("symbol", String),
    Column("watch_bucket_start", DateTime(timezone=True)),
    Column("watch_decision_at", DateTime(timezone=True)),
    Column("claimed_at", DateTime(timezone=True)),
    Column("entry_status", String),
    Column("entry_reason", String),
    Column("entry_quote_requested_at", DateTime(timezone=True)),
    Column("entry_quote_observed_at", DateTime(timezone=True)),
    Column("entry_exchange_event_at", DateTime(timezone=True)),
    Column("entry_quote_latency_ms", Integer),
    Column("unified_symbol", String),
    Column("market_id", String),
    Column("contract_size", Double),
    Column("entry_best_bid", Double),
    Column("entry_best_ask", Double),
    Column("entry_mid", Double),
    Column("entry_spread_bps", Double),
    Column("entry_vwap", Double),
    Column("entry_impact_bps", Double),
    Column("entry_filled_notional_usd", Double),
    Column("entry_at", DateTime(timezone=True)),
    Column("position_status", String),
    Column("exit_reason", String),
    Column("exit_quote_requested_at", DateTime(timezone=True)),
    Column("exit_quote_observed_at", DateTime(timezone=True)),
    Column("exit_exchange_event_at", DateTime(timezone=True)),
    Column("exit_quote_latency_ms", Integer),
    Column("exit_best_bid", Double),
    Column("exit_best_ask", Double),
    Column("exit_mid", Double),
    Column("exit_spread_bps", Double),
    Column("exit_vwap", Double),
    Column("exit_impact_bps", Double),
    Column("exit_filled_notional_usd", Double),
    Column("exit_at", DateTime(timezone=True)),
    Column("max_favorable_return_pct", Double),
    Column("max_adverse_return_pct", Double),
    Column("gross_return_pct", Double),
    Column("net_return_pct", Double),
    Column("gross_pnl_usd", Double),
    Column("net_pnl_usd", Double),
    Column("fees_usd", Double),
    Column("funding_usd", Double),
    Column("accounting_status", String),
    Column("accounting_error", Text),
    Column("last_error", Text),
    Column("updated_at", DateTime(timezone=True)),
    schema="app",
)

_outcomes = Table(
    "momentum_flow_paper_outcomes",
    _metadata,
    Column("paper_id", Uuid),
    Column("horizon_minutes", Integer),
    Column("due_at", DateTime(timezone=True)),
    Column("status", String),
    Column("quote_requested_at", DateTime(timezone=True)),
    Column("quote_observed_at", DateTime(timezone=True)),
    Column("exchange_event_at", DateTime(timezone=True)),
    Column("quote_latency_ms", Integer),
    Column("best_bid", Double),
    Column("best_ask", Double),
    Column("mid", Double),
    Column("spread_bps", Double),
    Column("bid_vwap", Double),
    Column("bid_impact_bps", Double),
    Column("filled_notional_usd", Double),
    Column("gross_return_pct", Double),
    Column("net_return_pct", Double),
    Column("gross_pnl_usd", Double),
    Column("net_pnl_usd", Double),
    Column("fees_usd", Double),
    Column("funding_usd", Double),
    Column("accounting_status", String),
    Column("accounting_error", Text),
    Column("error", Text),
    Column("updated_at", DateTime(timezone=True)),
    schema="app",
)


@dataclass(frozen=True)
class PaperRun:
    paper_version: str
    contract_sha256: str
    contract_json: dict[str, Any]
    cohort_started_at: datetime
    status: str


@dataclass(frozen=True)
class WatchCandidate:
    watch_id: UUID
    episode_id: UUID
    exchange: str
    market_type: str
    symbol: str
    bucket_start: datetime
    decision_at: datetime


@dataclass(frozen=True)
class PaperProbe:
    paper_id: UUID
    symbol: str
    entry_at: datetime
    entry_vwap: float
    position_status: str
    max_favorable_return_pct: float | None
    max_adverse_return_pct: float | None


@dataclass(frozen=True)
class PaperHealth:
    total: int
    opened: int
    rejected: int
    open_positions: int
    closed_positions: int
    exit_unresolved: int
    pending_outcomes: int
    complete_outcomes: int
    missed_outcomes: int
    last_entry_at: datetime | None
    last_exit_at: datetime | None


@dataclass(frozen=True)
class ProbeQuoteEvaluation:
    duration_minutes: float
    performance: AccountingResult
    favorable_return_pct: float
    adverse_return_pct: float
    exit_reason: str | None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def paper_id_for(contract: PaperContract, watch_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"{contract.paper_version}:{watch_id}")


def _accounting(
    contract: PaperContract,
    *,
    entry_price: float,
    exit_price: float,
    duration_minutes: float,
) -> AccountingResult:
    # Exact VWAPs already include spread and impact, so applying impact again as
    # modeled slippage would double count it. Fees and funding remain conservative.
    return calculate_performance(
        position_usd=contract.position_notional_usd,
        entry_price=entry_price,
        exit_price=exit_price,
        side=contract.side,
        duration_minutes=max(0.0, duration_minutes),
        entry_slippage_bps=0.0,
        exit_slippage_bps=0.0,
    )


def _accounting_values(result: AccountingResult) -> dict[str, Any]:
    return {
        "gross_return_pct": result.gross_return_pct,
        "net_return_pct": result.net_return_pct,
        "gross_pnl_usd": result.gross_pnl_usd,
        "net_pnl_usd": result.net_pnl_usd,
        "fees_usd": result.fees_usd,
        "funding_usd": result.funding_usd,
        "accounting_status": result.status,
        "accounting_error": result.error,
    }


def evaluate_probe_quote(
    probe: PaperProbe,
    quote: ExecutableQuote,
    *,
    contract: PaperContract,
) -> ProbeQuoteEvaluation:
    duration = max(0.0, (quote.observed_at - probe.entry_at).total_seconds() / 60)
    performance = _accounting(
        contract,
        entry_price=probe.entry_vwap,
        exit_price=quote.vwap,
        duration_minutes=duration,
    )
    gross = performance.gross_return_pct
    exit_reason: str | None = None
    if probe.position_status == "open":
        if gross <= -contract.stop_loss_pct:
            exit_reason = "stop_loss"
        elif duration >= contract.max_hold_minutes:
            exit_reason = "max_hold"
    return ProbeQuoteEvaluation(
        duration_minutes=duration,
        performance=performance,
        favorable_return_pct=max(probe.max_favorable_return_pct or 0.0, gross),
        adverse_return_pct=min(probe.max_adverse_return_pct or 0.0, gross),
        exit_reason=exit_reason,
    )


class MomentumFlowPaperRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._lock_connection: AsyncConnection | None = None

    @classmethod
    def from_url(cls, database_url: str) -> MomentumFlowPaperRepository:
        return cls(
            create_async_engine(
                async_database_url(database_url),
                pool_pre_ping=True,
                pool_size=2,
                max_overflow=0,
            )
        )

    async def acquire_worker_lock(self, paper_version: str) -> bool:
        if self._lock_connection is not None:
            return True
        connection = await self._engine.connect()
        result = await connection.execute(
            select(func.pg_try_advisory_lock(func.hashtext(paper_version)))
        )
        if not bool(result.scalar_one()):
            await connection.close()
            return False
        self._lock_connection = connection
        return True

    async def register_run(
        self,
        *,
        contract: PaperContract,
        contract_sha256: str,
        now: datetime,
    ) -> PaperRun:
        values = {
            "paper_version": contract.paper_version,
            "contract_sha256": contract_sha256,
            "contract_json": json.loads(contract.canonical_json()),
            "cohort_started_at": _utc(now),
            "status": "active",
        }
        statement = insert(_runs).values(values)
        async with self._engine.begin() as connection:
            await connection.execute(
                statement.on_conflict_do_nothing(index_elements=[_runs.c.paper_version])
            )
            result = await connection.execute(
                select(
                    _runs.c.paper_version,
                    _runs.c.contract_sha256,
                    _runs.c.contract_json,
                    _runs.c.cohort_started_at,
                    _runs.c.status,
                ).where(_runs.c.paper_version == contract.paper_version)
            )
            run = PaperRun(*result.one())
        if run.contract_sha256 != contract_sha256:
            raise RuntimeError("stored momentum paper contract hash does not match this binary")
        if run.contract_json != values["contract_json"]:
            raise RuntimeError("stored momentum paper contract JSON does not match this binary")
        if run.status != "active":
            raise RuntimeError(f"momentum paper run is {run.status}")
        return run

    async def abandon_interrupted_entries(self, *, contract: PaperContract, now: datetime) -> int:
        statement = (
            update(_probes)
            .where(
                _probes.c.paper_version == contract.paper_version,
                _probes.c.entry_status == "pending",
            )
            .values(
                entry_status="unresolved_interrupted",
                entry_reason="worker_interrupted_after_claim",
                last_error="worker restarted before the non-recoverable entry quote was stored",
                updated_at=_utc(now),
            )
        )
        async with self._engine.begin() as connection:
            result = await connection.execute(statement)
            return int(result.rowcount or 0)

    async def due_watches(
        self,
        *,
        contract: PaperContract,
        cohort_started_at: datetime,
        limit: int,
    ) -> tuple[WatchCandidate, ...]:
        already_claimed = exists(
            select(1).where(
                _probes.c.paper_version == contract.paper_version,
                _probes.c.watch_id == _watch_evaluations.c.watch_id,
            )
        )
        statement = (
            select(
                _watch_evaluations.c.watch_id,
                _watch_evaluations.c.episode_id,
                _watch_evaluations.c.exchange,
                _watch_evaluations.c.market_type,
                _watch_evaluations.c.symbol,
                _watch_evaluations.c.bucket_start,
                _watch_evaluations.c.decision_at,
            )
            .where(
                _watch_evaluations.c.watch_version == contract.watch_version,
                _watch_evaluations.c.exchange == contract.source_exchange,
                _watch_evaluations.c.market_type == contract.market_type,
                _watch_evaluations.c.decision_status == "watch",
                _watch_evaluations.c.decision_at >= _utc(cohort_started_at),
                _watch_evaluations.c.watch_id.is_not(None),
                _watch_evaluations.c.episode_id.is_not(None),
                ~already_claimed,
            )
            .order_by(_watch_evaluations.c.decision_at)
            .limit(limit)
        )
        result = await self._execute(statement)
        return tuple(WatchCandidate(*row) for row in result.all())

    async def claim_watch(
        self,
        candidate: WatchCandidate,
        *,
        contract: PaperContract,
        now: datetime,
    ) -> UUID | None:
        paper_id = paper_id_for(contract, candidate.watch_id)
        statement = (
            insert(_probes)
            .values(
                paper_id=paper_id,
                paper_version=contract.paper_version,
                watch_version=contract.watch_version,
                watch_id=candidate.watch_id,
                episode_id=candidate.episode_id,
                exchange=candidate.exchange,
                market_type=candidate.market_type,
                symbol=candidate.symbol,
                watch_bucket_start=_utc(candidate.bucket_start),
                watch_decision_at=_utc(candidate.decision_at),
                claimed_at=_utc(now),
                entry_status="pending",
                position_status="not_open",
            )
            .on_conflict_do_nothing(index_elements=[_probes.c.paper_version, _probes.c.watch_id])
            .returning(_probes.c.paper_id)
        )
        async with self._engine.begin() as connection:
            result = await connection.execute(statement)
            return result.scalar_one_or_none()

    async def reject_stale_entry(self, paper_id: UUID, *, now: datetime) -> None:
        await self._update_pending_entry(
            paper_id,
            {
                "entry_status": "rejected_stale",
                "entry_reason": "watch_to_quote_deadline_exceeded",
                "updated_at": _utc(now),
            },
        )

    async def reject_quote(self, paper_id: UUID, failure: QuoteFailure) -> None:
        await self._update_pending_entry(
            paper_id,
            {
                "entry_status": "rejected_quote",
                "entry_reason": failure.reason,
                "entry_quote_requested_at": failure.requested_at,
                "entry_quote_latency_ms": failure.latency_ms,
                "last_error": failure.error,
                "updated_at": failure.failed_at,
            },
        )

    async def open_entry(
        self,
        paper_id: UUID,
        quote: ExecutableQuote,
        *,
        contract: PaperContract,
    ) -> None:
        values = {
            "entry_status": "opened",
            "entry_reason": "exact_venue_executable_ask",
            "entry_quote_requested_at": quote.requested_at,
            "entry_quote_observed_at": quote.observed_at,
            "entry_exchange_event_at": quote.exchange_event_at,
            "entry_quote_latency_ms": quote.latency_ms,
            "unified_symbol": quote.unified_symbol,
            "market_id": quote.market_id,
            "contract_size": quote.contract_size,
            "entry_best_bid": quote.best_bid,
            "entry_best_ask": quote.best_ask,
            "entry_mid": quote.mid,
            "entry_spread_bps": quote.spread_bps,
            "entry_vwap": quote.vwap,
            "entry_impact_bps": quote.impact_bps,
            "entry_filled_notional_usd": quote.filled_notional_usd,
            "entry_at": quote.observed_at,
            "position_status": "open",
            "max_favorable_return_pct": 0.0,
            "max_adverse_return_pct": 0.0,
            "updated_at": quote.observed_at,
        }
        outcome_rows = [
            {
                "paper_id": paper_id,
                "horizon_minutes": horizon,
                "due_at": quote.observed_at + timedelta(minutes=horizon),
                "status": "pending",
            }
            for horizon in contract.outcome_horizons_minutes
        ]
        async with self._engine.begin() as connection:
            result = await connection.execute(
                update(_probes)
                .where(_probes.c.paper_id == paper_id, _probes.c.entry_status == "pending")
                .values(**values)
            )
            if result.rowcount != 1:
                raise RuntimeError("momentum paper entry claim is no longer pending")
            await connection.execute(insert(_outcomes).values(outcome_rows))

    async def monitored_probes(
        self,
        *,
        contract: PaperContract,
        now: datetime,
        limit: int,
    ) -> tuple[PaperProbe, ...]:
        due_outcome = exists(
            select(1).where(
                _outcomes.c.paper_id == _probes.c.paper_id,
                _outcomes.c.status == "pending",
                _outcomes.c.due_at <= _utc(now),
            )
        )
        statement = (
            select(
                _probes.c.paper_id,
                _probes.c.symbol,
                _probes.c.entry_at,
                _probes.c.entry_vwap,
                _probes.c.position_status,
                _probes.c.max_favorable_return_pct,
                _probes.c.max_adverse_return_pct,
            )
            .where(
                _probes.c.paper_version == contract.paper_version,
                _probes.c.entry_status == "opened",
                or_(_probes.c.position_status == "open", due_outcome),
            )
            .order_by(_probes.c.entry_at)
            .limit(limit)
        )
        result = await self._execute(statement)
        return tuple(PaperProbe(*row) for row in result.all())

    async def pending_horizons(self, paper_id: UUID, *, now: datetime) -> tuple[int, ...]:
        result = await self._execute(
            select(_outcomes.c.horizon_minutes)
            .where(
                _outcomes.c.paper_id == paper_id,
                _outcomes.c.status == "pending",
                _outcomes.c.due_at <= _utc(now),
            )
            .order_by(_outcomes.c.horizon_minutes)
        )
        return tuple(int(row[0]) for row in result.all())

    async def apply_quote(
        self,
        probe: PaperProbe,
        quote: ExecutableQuote,
        *,
        due_horizons: tuple[int, ...],
        contract: PaperContract,
    ) -> str | None:
        evaluation = evaluate_probe_quote(probe, quote, contract=contract)

        async with self._engine.begin() as connection:
            for horizon in due_horizons:
                horizon_performance = _accounting(
                    contract,
                    entry_price=probe.entry_vwap,
                    exit_price=quote.vwap,
                    duration_minutes=evaluation.duration_minutes,
                )
                await connection.execute(
                    update(_outcomes)
                    .where(
                        _outcomes.c.paper_id == probe.paper_id,
                        _outcomes.c.horizon_minutes == horizon,
                        _outcomes.c.status == "pending",
                    )
                    .values(
                        status="complete",
                        quote_requested_at=quote.requested_at,
                        quote_observed_at=quote.observed_at,
                        exchange_event_at=quote.exchange_event_at,
                        quote_latency_ms=quote.latency_ms,
                        best_bid=quote.best_bid,
                        best_ask=quote.best_ask,
                        mid=quote.mid,
                        spread_bps=quote.spread_bps,
                        bid_vwap=quote.vwap,
                        bid_impact_bps=quote.impact_bps,
                        filled_notional_usd=quote.filled_notional_usd,
                        **_accounting_values(horizon_performance),
                        updated_at=quote.observed_at,
                    )
                )
            probe_values: dict[str, Any] = {
                "max_favorable_return_pct": evaluation.favorable_return_pct,
                "max_adverse_return_pct": evaluation.adverse_return_pct,
                "last_error": None,
                "updated_at": quote.observed_at,
            }
            if evaluation.exit_reason is not None:
                probe_values.update(
                    {
                        "position_status": "closed",
                        "exit_reason": evaluation.exit_reason,
                        "exit_quote_requested_at": quote.requested_at,
                        "exit_quote_observed_at": quote.observed_at,
                        "exit_exchange_event_at": quote.exchange_event_at,
                        "exit_quote_latency_ms": quote.latency_ms,
                        "exit_best_bid": quote.best_bid,
                        "exit_best_ask": quote.best_ask,
                        "exit_mid": quote.mid,
                        "exit_spread_bps": quote.spread_bps,
                        "exit_vwap": quote.vwap,
                        "exit_impact_bps": quote.impact_bps,
                        "exit_filled_notional_usd": quote.filled_notional_usd,
                        "exit_at": quote.observed_at,
                        **_accounting_values(evaluation.performance),
                    }
                )
            await connection.execute(
                update(_probes).where(_probes.c.paper_id == probe.paper_id).values(**probe_values)
            )
        return evaluation.exit_reason

    async def record_quote_failure(
        self,
        paper_id: UUID,
        failure: QuoteFailure,
    ) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                update(_probes)
                .where(_probes.c.paper_id == paper_id)
                .values(
                    last_error=f"{failure.reason}: {failure.error}", updated_at=failure.failed_at
                )
            )

    async def expire_deadlines(
        self,
        *,
        contract: PaperContract,
        now: datetime,
    ) -> tuple[int, int]:
        deadline = _utc(now) - timedelta(seconds=contract.max_outcome_quote_lateness_seconds)
        async with self._engine.begin() as connection:
            outcomes = await connection.execute(
                update(_outcomes)
                .where(
                    _outcomes.c.paper_id.in_(
                        select(_probes.c.paper_id).where(
                            _probes.c.paper_version == contract.paper_version
                        )
                    ),
                    _outcomes.c.status == "pending",
                    _outcomes.c.due_at < deadline,
                )
                .values(
                    status="missed_deadline",
                    error="no exact executable quote before the frozen deadline",
                    updated_at=_utc(now),
                )
            )
            exits = await connection.execute(
                update(_probes)
                .where(
                    _probes.c.paper_version == contract.paper_version,
                    _probes.c.position_status == "open",
                    _probes.c.entry_at + timedelta(minutes=contract.max_hold_minutes) < deadline,
                )
                .values(
                    position_status="exit_unresolved",
                    last_error="max-hold executable exit quote missed its deadline",
                    updated_at=_utc(now),
                )
            )
            return int(outcomes.rowcount or 0), int(exits.rowcount or 0)

    async def health(self, *, contract: PaperContract) -> PaperHealth:
        result = await self._execute(
            select(
                func.count(_probes.c.paper_id),
                func.count().filter(_probes.c.entry_status == "opened"),
                func.count().filter(
                    _probes.c.entry_status.in_(("rejected_stale", "rejected_quote"))
                ),
                func.count().filter(_probes.c.position_status == "open"),
                func.count().filter(_probes.c.position_status == "closed"),
                func.count().filter(_probes.c.position_status == "exit_unresolved"),
                func.max(_probes.c.entry_at),
                func.max(_probes.c.exit_at),
            ).where(_probes.c.paper_version == contract.paper_version)
        )
        row = result.one()
        outcomes = await self._execute(
            select(
                func.count().filter(_outcomes.c.status == "pending"),
                func.count().filter(_outcomes.c.status == "complete"),
                func.count().filter(_outcomes.c.status == "missed_deadline"),
            )
            .select_from(_outcomes.join(_probes, _probes.c.paper_id == _outcomes.c.paper_id))
            .where(_probes.c.paper_version == contract.paper_version)
        )
        outcome_row = outcomes.one()
        return PaperHealth(
            total=int(row[0] or 0),
            opened=int(row[1] or 0),
            rejected=int(row[2] or 0),
            open_positions=int(row[3] or 0),
            closed_positions=int(row[4] or 0),
            exit_unresolved=int(row[5] or 0),
            pending_outcomes=int(outcome_row[0] or 0),
            complete_outcomes=int(outcome_row[1] or 0),
            missed_outcomes=int(outcome_row[2] or 0),
            last_entry_at=row[6],
            last_exit_at=row[7],
        )

    async def _update_pending_entry(self, paper_id: UUID, values: dict[str, Any]) -> None:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                update(_probes)
                .where(_probes.c.paper_id == paper_id, _probes.c.entry_status == "pending")
                .values(**values)
            )
            if result.rowcount != 1:
                raise RuntimeError("momentum paper entry claim is no longer pending")

    async def _execute(self, statement: Any) -> Any:
        async with self._engine.connect() as connection:
            return await connection.execute(statement)

    async def close(self) -> None:
        if self._lock_connection is not None:
            await self._lock_connection.close()
            self._lock_connection = None
        await self._engine.dispose()
