"""SQLAlchemy query layer for the read-only decision measurement report."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from schurfer_journal.models import TradeDecision, TradeDecisionOutcome
from sqlalchemy import (
    Float,
    Integer,
    String,
    and_,
    case,
    cast,
    column,
    func,
    literal,
    select,
    true,
    values,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .measurement_report import (
    CohortRow,
    CoverageRow,
    DatasetHealth,
    MeasurementReport,
    PerformanceRow,
    QualityReasonRow,
    ReportFilters,
)
from .outcome_repository import async_database_url
from .outcomes import FALLBACK_OUTCOME_STATUSES, HORIZONS_MINUTES, MEASURABLE_OUTCOME_STATUSES

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.sql import ColumnElement, Select


_TAKEN_ACTIONS = ("opened", "opened_dry_run")


def _strategy() -> ColumnElement[str]:
    return func.coalesce(TradeDecision.__table__.c.strategy_version, "unversioned")


def _episode_id() -> ColumnElement[str | None]:
    decisions = TradeDecision.__table__
    return func.coalesce(
        # SQLAlchemy infers nullable BigInteger -> str|None here, while String is
        # typed as str; PostgreSQL CAST preserves NULL exactly as required.
        cast(decisions.c.pump_event_id, String()),  # type: ignore[arg-type]
        func.jsonb_extract_path_text(
            decisions.c.features,
            "signal",
            "episode",
            "id",
        ),
    )


def _filters(filters: ReportFilters) -> list[ColumnElement[bool]]:
    decisions = TradeDecision.__table__
    clauses: list[ColumnElement[bool]] = []
    if filters.since is not None:
        clauses.append(decisions.c.ts >= filters.since)
    if filters.until is not None:
        clauses.append(decisions.c.ts < filters.until)
    if filters.strategy_versions:
        clauses.append(decisions.c.strategy_version.in_(filters.strategy_versions))
    return clauses


def dataset_health_statement(filters: ReportFilters) -> Select[Any]:
    decisions = TradeDecision.__table__
    liquidity_status = func.jsonb_extract_path_text(decisions.c.liquidity, "status")
    contract_size = decisions.c.liquidity["contract_size"]
    signal = decisions.c.features["signal"]
    quality = decisions.c.liquidity["quality"]
    computed_at = decisions.c.features["signal"]["computed_at"]
    computed_at_text = func.jsonb_extract_path_text(
        decisions.c.features,
        "signal",
        "computed_at",
    )
    computed_epoch = case(
        (func.jsonb_typeof(computed_at) == "number", cast(computed_at_text, Float)),
        else_=None,
    )
    raw_lag = func.extract("epoch", decisions.c.ts) - computed_epoch
    valid_lag = case(
        (
            and_(computed_epoch > 0, raw_lag >= -5, raw_lag <= 3600),
            raw_lag,
        ),
        else_=None,
    )
    return select(
        func.count().label("total_decisions"),
        func.min(decisions.c.ts).label("first_decision_at"),
        func.max(decisions.c.ts).label("last_decision_at"),
        func.count(func.distinct(_episode_id())).label("unique_episodes"),
        func.count()
        .filter(decisions.c.pump_event_id.is_not(None))
        .label("direct_episode_ids_present"),
        func.count().filter(decisions.c.decision_id.is_not(None)).label("decision_ids_present"),
        func.count().filter(decisions.c.price > 0).label("prices_present"),
        func.count().filter(decisions.c.features.is_not(None)).label("features_present"),
        func.count().filter(func.jsonb_typeof(signal) == "object").label("signal_present"),
        func.count().filter(decisions.c.liquidity.is_not(None)).label("liquidity_present"),
        func.count().filter(liquidity_status == "sampled").label("liquidity_sampled"),
        func.count()
        .filter(
            liquidity_status == "sampled",
            func.jsonb_typeof(contract_size) == "number",
        )
        .label("sampled_contract_size_present"),
        func.count().filter(liquidity_status == "fetch_failed").label("liquidity_fetch_failed"),
        func.count().filter(liquidity_status == "no_exchange").label("liquidity_no_exchange"),
        func.count().filter(func.jsonb_typeof(quality) == "object").label("quality_present"),
        func.count(valid_lag).label("signal_lag_samples"),
        func.avg(valid_lag).label("signal_lag_avg_seconds"),
        func.percentile_cont(0.5).within_group(valid_lag).label("signal_lag_p50_seconds"),
        func.percentile_cont(0.95).within_group(valid_lag).label("signal_lag_p95_seconds"),
    ).where(*_filters(filters))


def cohort_statement(filters: ReportFilters) -> Select[Any]:
    decisions = TradeDecision.__table__
    strategy = _strategy().label("strategy_version")
    return (
        select(
            strategy,
            func.count().label("decisions"),
            func.count(func.distinct(_episode_id())).label("episodes"),
            func.count().filter(decisions.c.action.in_(_TAKEN_ACTIONS)).label("taken"),
            func.count().filter(~decisions.c.action.in_(_TAKEN_ACTIONS)).label("skipped"),
            func.min(decisions.c.ts).label("first_decision_at"),
            func.max(decisions.c.ts).label("last_decision_at"),
        )
        .where(*_filters(filters))
        .group_by(strategy)
        .order_by(strategy)
    )


def quality_reason_statement(filters: ReportFilters) -> Select[Any]:
    decisions = TradeDecision.__table__
    strategy = _strategy().label("strategy_version")
    quality_reason = func.jsonb_extract_path_text(decisions.c.liquidity, "quality", "reason")
    liquidity_status = func.coalesce(
        func.jsonb_extract_path_text(decisions.c.liquidity, "status"),
        "missing",
    )
    reason = func.coalesce(
        quality_reason,
        literal("not_measured:") + liquidity_status,
    ).label("reason")
    return (
        select(strategy, reason, func.count().label("decisions"))
        .where(*_filters(filters))
        .group_by(strategy, reason)
        .order_by(strategy, func.count().desc(), reason)
    )


def outcome_coverage_statement(filters: ReportFilters) -> Select[Any]:
    decisions = TradeDecision.__table__
    outcomes = TradeDecisionOutcome.__table__
    horizons = values(
        column("horizon_minutes", Integer),
        name="report_horizons",
    ).data([(horizon,) for horizon in HORIZONS_MINUTES])
    outcome_match = and_(
        outcomes.c.decision_id == decisions.c.decision_id,
        outcomes.c.horizon_minutes == horizons.c.horizon_minutes,
        outcomes.c.resolver_version == filters.resolver_version,
    )
    strategy = _strategy().label("strategy_version")
    status = func.coalesce(outcomes.c.status, "unresolved").label("status")
    due_at = decisions.c.ts + horizons.c.horizon_minutes * timedelta(minutes=1)
    return (
        select(
            strategy,
            horizons.c.horizon_minutes,
            status,
            func.count().label("decisions"),
        )
        .select_from(decisions.join(horizons, true()).outerjoin(outcomes, outcome_match))
        .where(
            *_filters(filters),
            decisions.c.decision_id.is_not(None),
            due_at <= func.now(),
        )
        .group_by(strategy, horizons.c.horizon_minutes, status)
        .order_by(strategy, horizons.c.horizon_minutes, status)
    )


def _segment() -> ColumnElement[str]:
    decisions = TradeDecision.__table__
    quality_allowed = func.jsonb_extract_path_text(
        decisions.c.liquidity,
        "quality",
        "allowed",
    )
    return case(
        (decisions.c.action.in_(_TAKEN_ACTIONS), "taken"),
        (quality_allowed == "true", "eligible_skip"),
        (quality_allowed == "false", "ineligible_skip"),
        else_="other_skip",
    )


def performance_statement(
    filters: ReportFilters,
    *,
    by_exchange: bool = False,
) -> Select[Any]:
    decisions = TradeDecision.__table__
    outcomes = TradeDecisionOutcome.__table__
    strategy = _strategy().label("strategy_version")
    segment = _segment().label("segment")
    exchange = decisions.c.exchange.label("exchange")
    win = case((outcomes.c.short_return_pct > 0, 1.0), else_=0.0)
    columns = [
        strategy,
        outcomes.c.horizon_minutes,
        segment,
    ]
    group_by = [strategy, outcomes.c.horizon_minutes, segment]
    if by_exchange:
        columns.append(exchange)
        group_by.append(exchange)
    columns.extend(
        [
            func.count().label("decisions"),
            func.count(func.distinct(_episode_id())).label("episodes"),
            func.count().filter(outcomes.c.status == "complete").label("exact_venue"),
            func.count()
            .filter(outcomes.c.status.in_(FALLBACK_OUTCOME_STATUSES))
            .label("fallback_venue"),
            func.avg(outcomes.c.short_return_pct).label("avg_short_return_pct"),
            func.percentile_cont(0.5)
            .within_group(outcomes.c.short_return_pct)
            .label("median_short_return_pct"),
            (func.avg(win) * 100).label("win_rate_pct"),
            func.avg(outcomes.c.mfe_pct).label("avg_mfe_pct"),
            func.avg(outcomes.c.mae_pct).label("avg_mae_pct"),
        ]
    )
    statement = (
        select(*columns)
        .select_from(decisions.join(outcomes, outcomes.c.decision_id == decisions.c.decision_id))
        .where(
            *_filters(filters),
            outcomes.c.resolver_version == filters.resolver_version,
            outcomes.c.status.in_(MEASURABLE_OUTCOME_STATUSES),
        )
    )
    if by_exchange:
        statement = statement.where(outcomes.c.horizon_minutes == filters.exchange_horizon)
    return statement.group_by(*group_by).order_by(*group_by)


def _float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _pct(count: int, total: int) -> float:
    return count / total * 100 if total else 0.0


def _health(row: Any) -> DatasetHealth:
    total = int(row["total_decisions"])
    first = row["first_decision_at"]
    last = row["last_decision_at"]
    observation_hours = (
        max(0.0, (last - first).total_seconds() / 3600)
        if first is not None and last is not None
        else 0.0
    )
    return DatasetHealth(
        total_decisions=total,
        first_decision_at=first,
        last_decision_at=last,
        observation_hours=observation_hours,
        decisions_per_hour=total / observation_hours if observation_hours > 0 else None,
        unique_episodes=int(row["unique_episodes"]),
        direct_episode_ids_present_pct=_pct(int(row["direct_episode_ids_present"]), total),
        decision_ids_present_pct=_pct(int(row["decision_ids_present"]), total),
        prices_present_pct=_pct(int(row["prices_present"]), total),
        features_present_pct=_pct(int(row["features_present"]), total),
        signal_present_pct=_pct(int(row["signal_present"]), total),
        liquidity_present_pct=_pct(int(row["liquidity_present"]), total),
        liquidity_sampled_pct=_pct(int(row["liquidity_sampled"]), total),
        sampled_contract_size_present_pct=_pct(
            int(row["sampled_contract_size_present"]),
            int(row["liquidity_sampled"]),
        ),
        liquidity_fetch_failed_pct=_pct(int(row["liquidity_fetch_failed"]), total),
        liquidity_no_exchange_pct=_pct(int(row["liquidity_no_exchange"]), total),
        quality_present_pct=_pct(int(row["quality_present"]), total),
        signal_lag_samples=int(row["signal_lag_samples"]),
        signal_lag_avg_seconds=_float(row["signal_lag_avg_seconds"]),
        signal_lag_p50_seconds=_float(row["signal_lag_p50_seconds"]),
        signal_lag_p95_seconds=_float(row["signal_lag_p95_seconds"]),
    )


def _performance(rows: Sequence[Any], *, exchange: bool) -> tuple[PerformanceRow, ...]:
    return tuple(
        PerformanceRow(
            strategy_version=str(row["strategy_version"]),
            horizon_minutes=int(row["horizon_minutes"]),
            segment=str(row["segment"]),
            exchange=str(row["exchange"]) if exchange and row["exchange"] else None,
            decisions=int(row["decisions"]),
            episodes=int(row["episodes"]),
            exact_venue=int(row["exact_venue"]),
            fallback_venue=int(row["fallback_venue"]),
            avg_short_return_pct=_float(row["avg_short_return_pct"]),
            median_short_return_pct=_float(row["median_short_return_pct"]),
            win_rate_pct=_float(row["win_rate_pct"]),
            avg_mfe_pct=_float(row["avg_mfe_pct"]),
            avg_mae_pct=_float(row["avg_mae_pct"]),
        )
        for row in rows
    )


class MeasurementRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, db_url: str) -> MeasurementRepository:
        return cls(
            create_async_engine(
                async_database_url(db_url),
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
            )
        )

    async def generate(self, filters: ReportFilters) -> MeasurementReport:
        statements = (
            dataset_health_statement(filters),
            cohort_statement(filters),
            quality_reason_statement(filters),
            outcome_coverage_statement(filters),
            performance_statement(filters),
            performance_statement(filters, by_exchange=True),
        )
        async with self._engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                isolation_level="REPEATABLE READ",
                postgresql_readonly=True,
            )
            async with connection.begin():
                results = [await connection.execute(statement) for statement in statements]

        health = _health(results[0].mappings().one())
        cohorts = tuple(
            CohortRow(
                strategy_version=str(row["strategy_version"]),
                decisions=int(row["decisions"]),
                episodes=int(row["episodes"]),
                taken=int(row["taken"]),
                skipped=int(row["skipped"]),
                first_decision_at=row["first_decision_at"],
                last_decision_at=row["last_decision_at"],
            )
            for row in results[1].mappings().all()
        )
        quality_reasons = tuple(
            QualityReasonRow(
                strategy_version=str(row["strategy_version"]),
                reason=str(row["reason"]),
                decisions=int(row["decisions"]),
            )
            for row in results[2].mappings().all()
        )
        coverage = tuple(
            CoverageRow(
                strategy_version=str(row["strategy_version"]),
                horizon_minutes=int(row["horizon_minutes"]),
                status=str(row["status"]),
                decisions=int(row["decisions"]),
            )
            for row in results[3].mappings().all()
        )
        return MeasurementReport(
            generated_at=datetime.now(UTC),
            filters=filters,
            health=health,
            cohorts=cohorts,
            quality_reasons=quality_reasons,
            coverage=coverage,
            performance=_performance(results[4].mappings().all(), exchange=False),
            exchange_performance=_performance(results[5].mappings().all(), exchange=True),
        )

    async def close(self) -> None:
        await self._engine.dispose()
