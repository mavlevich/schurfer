from datetime import UTC, datetime

from schurfer_analytics.source_lead_repository import (
    map_source_lead_rows,
    source_lead_statement,
)
from sqlalchemy.dialects import postgresql


def _sql(statement: object) -> str:
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
    )


def test_source_lead_statement_applies_event_and_source_cutoffs() -> None:
    since = datetime(2026, 7, 24, tzinfo=UTC)
    until = datetime(2026, 8, 1, tzinfo=UTC)

    sql = _sql(source_lead_statement(since, until))

    assert "LEFT OUTER JOIN app.pump_event_sources" in sql
    assert "pump_event_sources.first_seen_at <" in sql
    assert "pump_events.first_seen_at >=" in sql
    assert "pump_events.first_seen_at <" in sql


def test_source_lead_mapping_preserves_zero_price_and_empty_source_scope() -> None:
    at = datetime(2026, 7, 24, tzinfo=UTC)
    common = {
        "event_id": 42,
        "base": "EDGE",
        "episode": 1,
        "event_first_seen_at": at,
        "closed_at": None,
    }
    rows = [
        {
            **common,
            "exchange": "mexc",
            "symbol": "EDGEUSDT",
            "identity_key": "edge:usdt:swap",
            "unified_symbol": "EDGE/USDT:USDT",
            "market_type": "swap",
            "base_asset": "EDGE",
            "quote_asset": "USDT",
            "settle_asset": "USDT",
            "onboarded_at": None,
            "identity_conflict": False,
            "source_first_seen_at": at,
            "first_change_pct": 20,
            "first_price": 0,
            "first_volume_24h_usd": None,
        },
        {
            **{**common, "event_id": 43, "base": "NONE"},
            "exchange": None,
        },
    ]

    events = map_source_lead_rows(rows)

    assert events[0].observations[0].first_price == 0
    assert events[1].observations == ()
