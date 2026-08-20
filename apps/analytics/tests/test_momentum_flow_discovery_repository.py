from datetime import UTC, datetime

from schurfer_analytics.momentum_flow_discovery_repository import (
    Pump,
    pump_observability_statement,
)
from sqlalchemy.dialects import postgresql


def test_pump_observability_query_is_exact_venue_instrument_and_point_in_time() -> None:
    statement = pump_observability_statement(
        (
            Pump(
                pump_id=1,
                exchange="bybit",
                symbol="EDGEUSDT",
                trigger_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
                watch_version="momentum_flow_watch_v1",
            ),
        ),
        pump_lead_minutes=240,
    )

    assert statement is not None
    sql = str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]
    assert "timeseries.momentum_flow_watch_evaluations_1m" in sql
    assert "momentum_flow_watch_evaluations_1m.exchange = discovery_pumps_" in sql
    assert "momentum_flow_watch_evaluations_1m.symbol = discovery_pumps_" in sql
    assert "momentum_flow_watch_evaluations_1m.watch_version" in sql
    assert "momentum_flow_watch_evaluations_1m.decision_at <= discovery_pumps_" in sql
    assert "momentum_flow_watch_evaluations_1m.quality_ready IS true" in sql
    assert "momentum_flow_watch_evaluations_1m.decision_status" in sql


def test_pump_observability_query_skips_empty_cohort() -> None:
    assert pump_observability_statement((), pump_lead_minutes=240) is None
