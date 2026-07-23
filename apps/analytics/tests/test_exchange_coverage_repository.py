from datetime import UTC, datetime

from schurfer_analytics.exchange_coverage_report import CoverageFilters
from schurfer_analytics.exchange_coverage_repository import (
    source_observations_statement,
    total_episodes_statement,
)
from sqlalchemy.dialects import postgresql


def _sql(statement: object) -> str:
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
    )


def test_total_episodes_filters_by_episode_first_seen() -> None:
    filters = CoverageFilters(
        since=datetime(2026, 7, 23, tzinfo=UTC),
        until=datetime(2026, 7, 24, tzinfo=UTC),
    )

    sql = _sql(total_episodes_statement(filters))

    assert "count(app.pump_events.id)" in sql
    assert "pump_events.first_seen_at >=" in sql
    assert "pump_events.first_seen_at <" in sql


def test_source_observations_join_episode_scope() -> None:
    sql = _sql(source_observations_statement(CoverageFilters()))

    assert "pump_event_sources" in sql
    assert "JOIN app.pump_events" in sql
    assert "pump_event_sources.event_id" in sql
    assert "pump_event_sources.first_seen_at" in sql
