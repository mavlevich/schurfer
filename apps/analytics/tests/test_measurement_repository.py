from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from schurfer_analytics.measurement_report import ReportFilters
from schurfer_analytics.measurement_repository import (
    MeasurementRepository,
    _health,
    cohort_statement,
    dataset_health_statement,
    outcome_coverage_statement,
    performance_statement,
    quality_reason_statement,
)
from sqlalchemy.dialects import postgresql


class _Result:
    def __init__(
        self, *, one: dict[str, object] | None = None, rows: list[dict[str, object]] | None = None
    ) -> None:
        self._one = one
        self._rows = rows or []

    def mappings(self) -> _Result:
        return self

    def one(self) -> dict[str, object]:
        assert self._one is not None
        return self._one

    def all(self) -> list[dict[str, object]]:
        return self._rows


def _sql(statement: object) -> tuple[str, dict[str, object]]:
    compiled = statement.compile(  # type: ignore[attr-defined]
        dialect=postgresql.dialect()  # type: ignore[no-untyped-call]
    )
    return str(compiled), compiled.params


def test_measurement_statements_use_shared_models_and_parameterized_filters() -> None:
    filters = ReportFilters(
        since=datetime(2026, 7, 22, tzinfo=UTC),
        strategy_versions=("pump_short_v1_market_quality",),
    )

    for statement in (
        dataset_health_statement(filters),
        cohort_statement(filters),
        quality_reason_statement(filters),
        outcome_coverage_statement(filters),
        performance_statement(filters),
    ):
        sql, params = _sql(statement)
        assert "app.trade_decisions" in sql
        assert "2026-07-22" not in sql
        assert "pump_short_v1_market_quality" not in sql
        assert datetime(2026, 7, 22, tzinfo=UTC) in params.values()


def test_health_statement_measures_signal_lag_and_dataset_presence() -> None:
    sql, _ = _sql(dataset_health_statement(ReportFilters()))

    assert "signal_lag_p50_seconds" in sql
    assert "signal_lag_p95_seconds" in sql
    assert "direct_episode_ids_present" in sql
    assert "trade_decisions.pump_event_id IS NOT NULL" in sql
    assert "quality_present" in sql
    assert "sampled_contract_size_present" in sql
    assert "jsonb_typeof" in sql


def test_coverage_statement_includes_due_but_unresolved_decisions() -> None:
    sql, params = _sql(outcome_coverage_statement(ReportFilters()))

    assert "VALUES" in sql
    assert "LEFT OUTER JOIN app.trade_decision_outcomes" in sql
    assert "<= now()" in sql
    assert "unresolved" in params.values()


def test_performance_statement_groups_correlated_rows_by_episode_and_segment() -> None:
    sql, params = _sql(performance_statement(ReportFilters(), by_exchange=True))

    assert "count(distinct(coalesce" in sql
    assert "trade_decisions.pump_event_id" in sql
    assert "short_return_pct" in sql
    assert "mfe_pct" in sql
    assert "mae_pct" in sql
    assert "complete_fallback_unsupported" in repr(params)
    assert 60 in params.values()


def test_health_mapping_handles_empty_dataset_without_division_by_zero() -> None:
    health = _health(
        {
            "total_decisions": 0,
            "first_decision_at": None,
            "last_decision_at": None,
            "unique_episodes": 0,
            "direct_episode_ids_present": 0,
            "decision_ids_present": 0,
            "prices_present": 0,
            "features_present": 0,
            "signal_present": 0,
            "liquidity_present": 0,
            "liquidity_sampled": 0,
            "sampled_contract_size_present": 0,
            "liquidity_fetch_failed": 0,
            "liquidity_no_exchange": 0,
            "quality_present": 0,
            "signal_lag_samples": 0,
            "signal_lag_avg_seconds": None,
            "signal_lag_p50_seconds": None,
            "signal_lag_p95_seconds": None,
        }
    )

    assert health.total_decisions == 0
    assert health.decisions_per_hour is None
    assert health.quality_present_pct == 0


def test_repository_factory_uses_bounded_read_pool() -> None:
    engine = MagicMock()

    with patch(
        "schurfer_analytics.measurement_repository.create_async_engine",
        return_value=engine,
    ) as create:
        repository = MeasurementRepository.from_url("postgresql://user:password@db/schurfer")

    assert repository is not None
    create.assert_called_once()
    assert create.call_args.kwargs == {
        "pool_pre_ping": True,
        "pool_size": 1,
        "max_overflow": 0,
    }


async def test_repository_disposes_owned_engine() -> None:
    engine = MagicMock()
    engine.dispose = AsyncMock()

    await MeasurementRepository(engine).close()

    engine.dispose.assert_awaited_once()


async def test_repository_generates_report_from_one_consistent_snapshot() -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    health = {
        "total_decisions": 2,
        "first_decision_at": now,
        "last_decision_at": now,
        "unique_episodes": 1,
        "direct_episode_ids_present": 2,
        "decision_ids_present": 2,
        "prices_present": 2,
        "features_present": 2,
        "signal_present": 2,
        "liquidity_present": 2,
        "liquidity_sampled": 2,
        "sampled_contract_size_present": 2,
        "liquidity_fetch_failed": 0,
        "liquidity_no_exchange": 0,
        "quality_present": 2,
        "signal_lag_samples": 2,
        "signal_lag_avg_seconds": 2.0,
        "signal_lag_p50_seconds": 2.0,
        "signal_lag_p95_seconds": 3.0,
    }
    cohort = {
        "strategy_version": "pump_short_v1_market_quality",
        "decisions": 2,
        "episodes": 1,
        "taken": 1,
        "skipped": 1,
        "first_decision_at": now,
        "last_decision_at": now,
    }
    raw_connection = MagicMock()
    connection = MagicMock()
    connection.execute = AsyncMock(
        side_effect=[
            _Result(one=health),
            _Result(rows=[cohort]),
            _Result(),
            _Result(),
            _Result(),
            _Result(),
        ]
    )
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    connection.begin.return_value = transaction
    raw_connection.execution_options = AsyncMock(return_value=connection)
    connect_context = MagicMock()
    connect_context.__aenter__ = AsyncMock(return_value=raw_connection)
    connect_context.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.connect.return_value = connect_context

    report = await MeasurementRepository(engine).generate(ReportFilters())

    raw_connection.execution_options.assert_awaited_once_with(
        isolation_level="REPEATABLE READ",
        postgresql_readonly=True,
    )
    assert connection.execute.await_count == 6
    assert report.health.total_decisions == 2
    assert report.health.direct_episode_ids_present_pct == 100
    assert report.health.sampled_contract_size_present_pct == 100
    assert report.cohorts[0].episodes == 1
