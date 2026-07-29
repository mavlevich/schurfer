from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from schurfer_analytics.derivatives_context import (
    DerivativesContextObservation,
    DerivativesContextProbeResult,
    DerivativesContextSample,
)
from schurfer_analytics.derivatives_context_repository import (
    DerivativesContextRepository,
    due_context_work_statement,
    latest_targets_statement,
    upsert_context_run_statement,
    upsert_context_samples_statement,
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
    assert "pump_event_sources.market_id IS NOT NULL" in sql
    assert "pump_event_sources.identity_key IS NOT NULL" in sql
    assert "pump_event_sources.identity_conflict IS false" in sql
    assert "2026-07-26 16:00:00+00:00" in sql
    assert "target_rank = 1" in sql


def _observation() -> DerivativesContextObservation:
    return DerivativesContextObservation(
        result=DerivativesContextProbeResult(
            exchange="binance",
            method="funding_rate_history",
            capability="fetchFundingRateHistory",
            declared_support=True,
            status="sampled",
            event_id=42,
            base="ERA",
            unified_symbol="ERA/USDT:USDT",
            market_id="ERAUSDT",
            identity_key="era",
            anchor_at=SINCE,
            requested_since=SINCE,
            requested_until=UNTIL,
            fetched_at=UNTIL,
            returned_rows=1,
            valid_timestamp_rows=1,
            in_window_rows=1,
            invalid_rows=0,
            first_source_at=SINCE,
            last_source_at=SINCE,
            effective_limit=200,
            request_count=1,
        ),
        samples=(
            DerivativesContextSample(
                source_at=SINCE,
                sample_key="a" * 64,
                payload={"timestamp": int(SINCE.timestamp() * 1000), "fundingRate": 0.01},
            ),
        ),
    )


def test_due_work_query_is_bounded_retryable_and_identity_safe() -> None:
    sql = _sql(
        due_context_work_statement(
            supported_pairs=(
                ("binance", "funding_rate_history"),
                ("htx", "liquidations"),
            ),
            resolver_version="derivatives_context_v1",
            cohort_start=SINCE,
            after_minutes=480,
            retryable_statuses=("fetch_failed", "no_data"),
            max_attempts=8,
            retry_after_seconds=900,
            batch_size=8,
        )
    )

    assert "supported_derivatives_pairs" in sql
    assert "pump_event_sources.unified_symbol IS NOT NULL" in sql
    assert "pump_event_sources.market_id IS NOT NULL" in sql
    assert "pump_event_sources.identity_key IS NOT NULL" in sql
    assert "pump_event_sources.identity_conflict IS false" in sql
    assert "pump_derivatives_context_runs.status IN ('fetch_failed', 'no_data')" in sql
    assert "pump_derivatives_context_runs.attempt_count < 8" in sql
    assert "LIMIT 8" in sql


def test_due_work_query_can_anchor_long_horizon_funding_at_episode_close() -> None:
    sql = _sql(
        due_context_work_statement(
            supported_pairs=(("binance", "funding_rate_history"),),
            resolver_version="long_horizon_funding_v1",
            cohort_start=SINCE,
            after_minutes=10_080,
            retryable_statuses=("fetch_failed",),
            max_attempts=8,
            retry_after_seconds=900,
            batch_size=8,
            anchor_mode="closed",
        )
    )

    assert "app.pump_events.closed_at AS anchor_at" in sql
    assert "app.pump_events.closed_at + make_interval(secs=>604800.0) <= now()" in sql
    assert "app.pump_events.closed_at >= '2026-07-20 00:00:00+00:00'" in sql


def test_run_and_sample_upserts_use_stable_constraints() -> None:
    run_sql = _sql(
        upsert_context_run_statement(
            _observation(),
            resolver_version="derivatives_context_v1",
            ccxt_version="4.5.68",
        )
    )
    sample_statement = upsert_context_samples_statement(7, _observation())
    sample_sql = str(
        sample_statement.compile(
            dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
        )
    )

    assert "ON CONFLICT ON CONSTRAINT uq_pump_derivatives_context_run DO UPDATE" in run_sql
    assert "attempt_count = (app.pump_derivatives_context_runs.attempt_count + 1)" in run_sql
    assert "RETURNING app.pump_derivatives_context_runs.id" in run_sql
    assert "ON CONFLICT ON CONSTRAINT uq_pump_derivatives_context_sample DO UPDATE" in sample_sql
    assert "a" * 64 in sample_statement.compile().params.values()


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


async def test_repository_persists_run_and_samples_in_one_transaction() -> None:
    engine = MagicMock()
    connection = MagicMock()
    connection.execute = AsyncMock()
    run_result = MagicMock()
    run_result.scalar_one.return_value = 7
    connection.execute.side_effect = [run_result, MagicMock()]
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=connection)
    transaction.__aexit__ = AsyncMock(return_value=None)
    engine.begin.return_value = transaction
    repository = DerivativesContextRepository(engine)

    await repository.persist_observations(
        (_observation(),),
        resolver_version="derivatives_context_v1",
        ccxt_version="4.5.68",
    )

    assert connection.execute.await_count == 2
    transaction.__aenter__.assert_awaited_once()
    transaction.__aexit__.assert_awaited_once()


async def test_repository_disposes_engine() -> None:
    engine = MagicMock()
    engine.dispose = AsyncMock()
    repository = DerivativesContextRepository(engine)

    await repository.close()

    engine.dispose.assert_awaited_once_with()
