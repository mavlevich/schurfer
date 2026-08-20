"""Bounded, read-only inputs for the momentum-flow discovery read.

The repository deliberately returns compact decision/probe rows and SQL-aggregated
pump observability. It never loads the full per-symbol, per-minute WATCH table into
Python: four weeks across the complete universe is millions of rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from schurfer_journal.models import PumpEventSource
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Double,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    case,
    column,
    func,
    or_,
    select,
    values,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime


@dataclass(frozen=True)
class VenueVersions:
    exchange: str
    watch_version: str
    watch_contract_sha256: str
    paper_version: str
    paper_contract_sha256: str


@dataclass(frozen=True)
class RunContract:
    version: str
    contract_sha256: str
    cohort_started_at: datetime


@dataclass(frozen=True)
class WatchDecision:
    exchange: str
    symbol: str
    bucket_start: datetime
    decision_at: datetime
    source_event_at: datetime | None
    source_received_at: datetime | None
    bucket_ready_at: datetime | None
    evaluator_started_at: datetime
    evaluator_completed_at: datetime


@dataclass(frozen=True)
class Pump:
    pump_id: int
    exchange: str
    symbol: str
    trigger_at: datetime
    watch_version: str


@dataclass(frozen=True)
class PumpObservability:
    pump_id: int
    operational_minutes: int
    quality_minutes: int
    earliest_watch_at: datetime | None


@dataclass(frozen=True)
class PaperProbe:
    paper_id: str
    paper_version: str
    exchange: str
    symbol: str
    watch_decision_at: datetime
    entry_status: str
    entry_reason: str | None
    entry_quote_latency_ms: int | None
    entry_filled_notional_usd: float | None
    entry_spread_bps: float | None
    entry_impact_bps: float | None
    entry_at: datetime | None
    position_status: str
    exit_reason: str | None
    exit_quote_latency_ms: int | None
    exit_spread_bps: float | None
    exit_impact_bps: float | None
    exit_at: datetime | None
    max_favorable_return_pct: float | None
    max_adverse_return_pct: float | None
    net_return_pct: float | None
    net_pnl_usd: float | None
    fees_usd: float | None
    funding_usd: float | None
    accounting_status: str | None


@dataclass(frozen=True)
class DiscoveryDataset:
    watch_runs: Mapping[str, RunContract]
    paper_runs: Mapping[str, RunContract]
    available_minutes: Mapping[str, tuple[datetime, ...]]
    watches: tuple[WatchDecision, ...]
    pumps: tuple[Pump, ...]
    pump_observability: Mapping[int, PumpObservability]
    probes: tuple[PaperProbe, ...]


_metadata = MetaData()

_watch_runs = Table(
    "momentum_flow_watch_runs",
    _metadata,
    Column("watch_version", String),
    Column("contract_sha256", String),
    Column("cohort_started_at", DateTime(timezone=True)),
    schema="app",
)

_evaluations = Table(
    "momentum_flow_watch_evaluations_1m",
    _metadata,
    Column("exchange", String),
    Column("market_type", String),
    Column("symbol", String),
    Column("watch_version", String),
    Column("bucket_start", DateTime(timezone=True)),
    Column("quality_ready", Boolean),
    Column("decision_status", String),
    Column("source_event_at", DateTime(timezone=True)),
    Column("source_received_at", DateTime(timezone=True)),
    Column("bucket_ready_at", DateTime(timezone=True)),
    Column("evaluator_started_at", DateTime(timezone=True)),
    Column("evaluator_completed_at", DateTime(timezone=True)),
    Column("decision_at", DateTime(timezone=True)),
    schema="timeseries",
)

_paper_runs = Table(
    "momentum_flow_paper_runs",
    _metadata,
    Column("paper_version", String),
    Column("contract_sha256", String),
    Column("cohort_started_at", DateTime(timezone=True)),
    schema="app",
)

_probes = Table(
    "momentum_flow_paper_probes",
    _metadata,
    Column("paper_id", String),
    Column("paper_version", String),
    Column("exchange", String),
    Column("symbol", String),
    Column("watch_decision_at", DateTime(timezone=True)),
    Column("entry_status", String),
    Column("entry_reason", String),
    Column("entry_quote_latency_ms", Integer),
    Column("entry_filled_notional_usd", Double),
    Column("entry_spread_bps", Double),
    Column("entry_impact_bps", Double),
    Column("entry_at", DateTime(timezone=True)),
    Column("position_status", String),
    Column("exit_reason", String),
    Column("exit_quote_latency_ms", Integer),
    Column("exit_spread_bps", Double),
    Column("exit_impact_bps", Double),
    Column("exit_at", DateTime(timezone=True)),
    Column("max_favorable_return_pct", Double),
    Column("max_adverse_return_pct", Double),
    Column("net_return_pct", Double),
    Column("net_pnl_usd", Double),
    Column("fees_usd", Double),
    Column("funding_usd", Double),
    Column("accounting_status", String),
    schema="app",
)


def _venue_clause(versions: Sequence[VenueVersions]) -> Any:
    return or_(
        *(
            and_(
                _evaluations.c.exchange == item.exchange,
                _evaluations.c.watch_version == item.watch_version,
            )
            for item in versions
        )
    )


def pump_observability_statement(pumps: Sequence[Pump], *, pump_lead_minutes: int) -> Any | None:
    """Aggregate exact-instrument pre-pump WATCH observability in PostgreSQL."""
    if not pumps:
        return None
    pump_values = (
        values(
            column("pump_id", BigInteger),
            column("exchange", String),
            column("symbol", String),
            column("trigger_at", DateTime(timezone=True)),
            column("watch_version", String),
            name="discovery_pumps",
        )
        .data(
            [
                (
                    pump.pump_id,
                    pump.exchange,
                    pump.symbol,
                    pump.trigger_at,
                    pump.watch_version,
                )
                for pump in pumps
            ]
        )
        .cte()
    )
    actionable = _evaluations.c.decision_at <= pump_values.c.trigger_at
    joined = pump_values.outerjoin(
        _evaluations,
        and_(
            _evaluations.c.exchange == pump_values.c.exchange,
            _evaluations.c.market_type == "linear",
            _evaluations.c.symbol == pump_values.c.symbol,
            _evaluations.c.watch_version == pump_values.c.watch_version,
            _evaluations.c.bucket_start
            >= pump_values.c.trigger_at - timedelta(minutes=pump_lead_minutes),
            _evaluations.c.bucket_start <= pump_values.c.trigger_at,
            actionable,
        ),
    )
    return (
        select(
            pump_values.c.pump_id,
            func.count(func.distinct(_evaluations.c.bucket_start)),
            func.count(
                func.distinct(
                    case(
                        (_evaluations.c.quality_ready.is_(True), _evaluations.c.bucket_start),
                        else_=None,
                    )
                )
            ),
            func.min(
                case(
                    (_evaluations.c.decision_status == "watch", _evaluations.c.decision_at),
                    else_=None,
                )
            ),
        )
        .select_from(joined)
        .group_by(pump_values.c.pump_id)
    )


class MomentumFlowDiscoveryRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, database_url: str) -> MomentumFlowDiscoveryRepository:
        return cls(
            create_async_engine(
                async_database_url(database_url),
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
            )
        )

    async def load(
        self,
        *,
        since: datetime,
        until: datetime,
        versions: Sequence[VenueVersions],
        pump_lead_minutes: int,
    ) -> DiscoveryDataset:
        watch_versions = tuple(item.watch_version for item in versions)
        paper_versions = tuple(item.paper_version for item in versions)
        exchange_to_watch = {item.exchange: item.watch_version for item in versions}
        venue_clause = _venue_clause(versions)

        async with self._engine.connect() as connection:
            watch_run_rows = (
                await connection.execute(
                    select(
                        _watch_runs.c.watch_version,
                        _watch_runs.c.contract_sha256,
                        _watch_runs.c.cohort_started_at,
                    ).where(_watch_runs.c.watch_version.in_(watch_versions))
                )
            ).all()
            paper_run_rows = (
                await connection.execute(
                    select(
                        _paper_runs.c.paper_version,
                        _paper_runs.c.contract_sha256,
                        _paper_runs.c.cohort_started_at,
                    ).where(_paper_runs.c.paper_version.in_(paper_versions))
                )
            ).all()

            minute_rows = (
                await connection.execute(
                    select(_evaluations.c.exchange, _evaluations.c.bucket_start)
                    .where(
                        venue_clause,
                        _evaluations.c.bucket_start >= since,
                        _evaluations.c.bucket_start < until,
                    )
                    .group_by(_evaluations.c.exchange, _evaluations.c.bucket_start)
                    .order_by(_evaluations.c.exchange, _evaluations.c.bucket_start)
                )
            ).all()

            watch_rows = (
                await connection.execute(
                    select(
                        _evaluations.c.exchange,
                        _evaluations.c.symbol,
                        _evaluations.c.bucket_start,
                        _evaluations.c.decision_at,
                        _evaluations.c.source_event_at,
                        _evaluations.c.source_received_at,
                        _evaluations.c.bucket_ready_at,
                        _evaluations.c.evaluator_started_at,
                        _evaluations.c.evaluator_completed_at,
                    ).where(
                        venue_clause,
                        _evaluations.c.decision_status == "watch",
                        _evaluations.c.bucket_start >= since,
                        _evaluations.c.bucket_start < until,
                    )
                )
            ).all()

            sources = PumpEventSource.__table__
            pump_rows = (
                await connection.execute(
                    select(
                        sources.c.id,
                        sources.c.exchange,
                        sources.c.market_id,
                        sources.c.first_seen_at,
                    ).where(
                        sources.c.exchange.in_(tuple(exchange_to_watch)),
                        sources.c.market_id.is_not(None),
                        sources.c.identity_conflict.is_(False),
                        sources.c.first_seen_at >= since,
                        sources.c.first_seen_at < until,
                    )
                )
            ).all()

            probe_rows = (
                (
                    await connection.execute(
                        select(*_probes.c).where(
                            _probes.c.paper_version.in_(paper_versions),
                            _probes.c.watch_decision_at >= since,
                            _probes.c.watch_decision_at < until,
                        )
                    )
                )
                .mappings()
                .all()
            )

            pumps = tuple(
                Pump(
                    pump_id=int(row[0]),
                    exchange=str(row[1]),
                    symbol=str(row[2]),
                    trigger_at=row[3],
                    watch_version=exchange_to_watch[str(row[1])],
                )
                for row in pump_rows
            )
            observability = await self._load_pump_observability(
                connection,
                pumps=pumps,
                pump_lead_minutes=pump_lead_minutes,
            )

        minutes: dict[str, list[datetime]] = {item.exchange: [] for item in versions}
        for exchange, bucket_start in minute_rows:
            minutes[str(exchange)].append(bucket_start)

        return DiscoveryDataset(
            watch_runs={
                str(version): RunContract(str(version), str(contract_hash), cohort_started_at)
                for version, contract_hash, cohort_started_at in watch_run_rows
            },
            paper_runs={
                str(version): RunContract(str(version), str(contract_hash), cohort_started_at)
                for version, contract_hash, cohort_started_at in paper_run_rows
            },
            available_minutes={key: tuple(value) for key, value in minutes.items()},
            watches=tuple(WatchDecision(*row) for row in watch_rows),
            pumps=pumps,
            pump_observability=observability,
            probes=tuple(
                PaperProbe(
                    **{
                        **dict(row),
                        # PostgreSQL returns UUID objects for the real column even
                        # though this read-only Table intentionally needs no UUID
                        # operators. Normalize it for deterministic JSON/fingerprints.
                        "paper_id": str(row["paper_id"]),
                    }
                )
                for row in probe_rows
            ),
        )

    async def _load_pump_observability(
        self,
        connection: Any,
        *,
        pumps: Sequence[Pump],
        pump_lead_minutes: int,
    ) -> dict[int, PumpObservability]:
        statement = pump_observability_statement(pumps, pump_lead_minutes=pump_lead_minutes)
        if statement is None:
            return {}
        rows = (await connection.execute(statement)).all()
        return {
            int(pump_id): PumpObservability(
                pump_id=int(pump_id),
                operational_minutes=int(operational_minutes),
                quality_minutes=int(quality_minutes),
                earliest_watch_at=earliest_watch_at,
            )
            for pump_id, operational_minutes, quality_minutes, earliest_watch_at in rows
        }

    async def close(self) -> None:
        await self._engine.dispose()
