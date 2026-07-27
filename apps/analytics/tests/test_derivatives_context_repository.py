from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from schurfer_analytics.derivatives_context_repository import (
    DerivativesContextRepository,
    latest_targets_statement,
)
from sqlalchemy.dialects import postgresql

SINCE = datetime(2026, 7, 20, tzinfo=UTC)
UNTIL = datetime(2026, 7, 27, tzinfo=UTC)


def _sql(statement: object) -> str:
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
    )


def test_latest_target_query_is_point_in_time_and_fail_closed() -> None:
    sql = _sql(
        latest_targets_statement(
            ("binance", "bybit"),
            since=SINCE,
            until=UNTIL,
            after_minutes=480,
        )
    )

    assert "row_number() OVER (PARTITION BY app.pump_event_sources.exchange" in sql
    assert "coalesce(app.pump_events.entry_qualified_at, app.pump_events.first_seen_at)" in sql
    assert "pump_event_sources.unified_symbol IS NOT NULL" in sql
    assert "pump_event_sources.identity_conflict IS false" in sql
    assert "2026-07-26 16:00:00+00:00" in sql
    assert "target_rank = 1" in sql


def test_repository_factory_uses_bounded_pre_ping_pool() -> None:
    engine = MagicMock()
    with patch(
        "schurfer_analytics.derivatives_context_repository.create_async_engine",
        return_value=engine,
    ) as create:
        repository = DerivativesContextRepository.from_url("postgresql://user:password@db/schurfer")

    assert repository is not None
    create.assert_called_once()
    assert create.call_args.kwargs == {
        "pool_pre_ping": True,
        "pool_size": 1,
        "max_overflow": 0,
    }


async def test_repository_maps_targets_in_read_only_repeatable_read() -> None:
    engine = MagicMock()
    raw_connection = MagicMock()
    connection = MagicMock()
    connection.execute = AsyncMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    connection.begin.return_value = transaction
    raw_connection.execution_options = AsyncMock(return_value=connection)
    connect_context = MagicMock()
    connect_context.__aenter__ = AsyncMock(return_value=raw_connection)
    connect_context.__aexit__ = AsyncMock(return_value=None)
    engine.connect.return_value = connect_context
    result = MagicMock()
    result.mappings.return_value.all.return_value = [
        {
            "event_id": 42,
            "exchange": "binance",
            "base": "ERA",
            "unified_symbol": "ERA/USDT:USDT",
            "market_id": "ERAUSDT",
            "identity_key": None,
            "anchor_at": SINCE,
        }
    ]
    connection.execute.return_value = result
    repository = DerivativesContextRepository(engine)

    targets = await repository.load_latest_targets(
        ("binance",),
        since=SINCE,
        until=UNTIL,
        after_minutes=480,
    )

    assert len(targets) == 1
    assert targets[0].event_id == 42
    assert targets[0].identity_key is None
    raw_connection.execution_options.assert_awaited_once_with(
        isolation_level="REPEATABLE READ",
        postgresql_readonly=True,
    )
    connection.execute.assert_awaited_once()


async def test_repository_disposes_engine() -> None:
    engine = MagicMock()
    engine.dispose = AsyncMock()
    repository = DerivativesContextRepository(engine)

    await repository.close()

    engine.dispose.assert_awaited_once_with()
