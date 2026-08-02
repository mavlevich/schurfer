from datetime import UTC, datetime

from schurfer_analytics.long_horizon_funding_repository import (
    funding_series_fingerprint,
    funding_series_statement,
    map_funding_series,
)
from sqlalchemy.dialects import postgresql

SINCE = datetime(2026, 7, 22, tzinfo=UTC)
UNTIL = datetime(2026, 7, 30, tzinfo=UTC)


def test_funding_series_query_is_exact_event_venue_and_version() -> None:
    statement = funding_series_statement(((42, "binance"), (43, "bybit")))
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "long_horizon_funding_keys" in sql
    assert "pump_derivatives_context_runs.event_id = long_horizon_funding_keys.event_id" in sql
    assert "pump_derivatives_context_runs.exchange = long_horizon_funding_keys.exchange" in sql
    assert "pump_derivatives_context_runs.method = 'funding_rate_history'" in sql
    assert "pump_derivatives_context_runs.resolver_version = 'long_horizon_funding_v1'" in sql
    assert "LEFT OUTER JOIN app.pump_derivatives_context_samples" in sql


def test_funding_series_query_accepts_an_independent_versioned_lane() -> None:
    statement = funding_series_statement(
        ((42, "binance"),),
        resolver_version="open_ended_margin_funding_v1",
    )
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "resolver_version = 'open_ended_margin_funding_v1'" in sql


def test_map_funding_series_preserves_empty_runs_and_sorts_samples() -> None:
    rows = [
        {
            "run_id": 1,
            "event_id": 42,
            "exchange": "binance",
            "status": "sampled",
            "requested_since": SINCE,
            "requested_until": UNTIL,
            "error": None,
            "source_at": datetime(2026, 7, 23, 8, tzinfo=UTC),
            "sample_key": "b",
            "payload": {"fundingRate": -0.001},
        },
        {
            "run_id": 1,
            "event_id": 42,
            "exchange": "binance",
            "status": "sampled",
            "requested_since": SINCE,
            "requested_until": UNTIL,
            "error": None,
            "source_at": datetime(2026, 7, 23, tzinfo=UTC),
            "sample_key": "a",
            "payload": {"fundingRate": 0.002},
        },
        {
            "run_id": 2,
            "event_id": 43,
            "exchange": "bybit",
            "status": "no_data",
            "requested_since": SINCE,
            "requested_until": UNTIL,
            "error": "no rows",
            "source_at": None,
            "sample_key": None,
            "payload": None,
        },
    ]

    mapped = map_funding_series(rows)

    assert [(item.event_id, item.exchange, item.status) for item in mapped] == [
        (42, "binance", "sampled"),
        (43, "bybit", "no_data"),
    ]
    assert [sample.sample_key for sample in mapped[0].samples] == ["a", "b"]
    assert mapped[1].samples == ()
    assert funding_series_fingerprint(mapped) == funding_series_fingerprint(mapped)
