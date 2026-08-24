from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_analytics.outcome_models import Outcome
from schurfer_analytics.outcome_repository import (
    OutcomeRepository,
    async_database_url,
    due_decisions_statement,
    upsert_outcomes_statement,
)
from sqlalchemy.dialects import postgresql


def _outcome() -> Outcome:
    return Outcome(
        decision_id="00000000-0000-0000-0000-000000000001",
        horizon_minutes=15,
        anchor_exchange="binance",
        source_exchange="binance",
        entry_price=100.0,
        forward_price=90.0,
        mfe_pct=12.0,
        mae_pct=3.0,
        short_return_pct=10.0,
        bars_count=3,
        expected_bars=3,
        coverage_ratio=1.0,
        status="complete",
    )


def _engine_context(method: str) -> tuple[MagicMock, AsyncMock]:
    engine = MagicMock()
    connection = AsyncMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=connection)
    context.__aexit__ = AsyncMock(return_value=None)
    getattr(engine, method).return_value = context
    return engine, connection


def test_async_database_url_selects_psycopg3_dialect() -> None:
    url = async_database_url("postgresql://user:password@db/schurfer")

    assert url.drivername == "postgresql+psycopg"
    assert url.database == "schurfer"


def test_repository_factory_uses_bounded_pre_ping_pool() -> None:
    engine = MagicMock()

    with patch(
        "schurfer_analytics.outcome_repository.create_async_engine",
        return_value=engine,
    ) as create:
        repository = OutcomeRepository.from_url("postgresql://user:password@db/schurfer")

    assert repository is not None
    create.assert_called_once()
    assert create.call_args.kwargs == {
        "pool_pre_ping": True,
        "pool_size": 1,
        "max_overflow": 0,
    }


def test_due_statement_is_parameterized_and_contains_retry_guards() -> None:
    statement = due_decisions_statement(
        horizons=(15, 60),
        resolver_version="forward_v1",
        retryable_statuses=("partial", "fetch_failed"),
        max_attempts=8,
        retry_after_seconds=900,
        batch_size=50,
    )

    compiled = statement.compile(
        dialect=postgresql.dialect()  # type: ignore[no-untyped-call]
    )
    sql = str(compiled)

    assert "trade_decisions" in sql
    assert "trade_decision_outcomes" in sql
    assert "VALUES" in sql
    assert "attempt_count" in sql
    assert "updated_at" in sql
    assert "forward_v1" in compiled.params.values()


def test_due_statement_prioritizes_decisions_with_a_usable_price() -> None:
    statement = due_decisions_statement(
        horizons=(15, 60),
        resolver_version="forward_v1",
        retryable_statuses=("partial",),
        max_attempts=8,
        retry_after_seconds=900,
        batch_size=50,
    )

    sql = str(
        statement.compile(
            dialect=postgresql.dialect()  # type: ignore[no-untyped-call]
        )
    )
    order_by = sql.split("ORDER BY", maxsplit=1)[1]

    assert order_by.lstrip().startswith(
        "app.trade_decisions.price IS NULL OR app.trade_decisions.price <="
    )


def test_due_statement_scopes_extended_paths_to_the_registered_strategy() -> None:
    statement = due_decisions_statement(
        horizons=(10_080, 20_160, 30_240, 40_320),
        resolver_version="forward_v1",
        retryable_statuses=("partial",),
        max_attempts=8,
        retry_after_seconds=900,
        batch_size=50,
        extended_horizons=(20_160, 30_240, 40_320),
        extended_strategy_versions=("pump_short_v1_market_quality",),
    )
    compiled = statement.compile(
        dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
        compile_kwargs={"literal_binds": True},
    )
    sql = str(compiled)

    assert "horizons.horizon_minutes NOT IN (20160, 30240, 40320)" in sql
    assert "trade_decisions.strategy_version IN ('pump_short_v1_market_quality')" in sql


def test_due_statement_excludes_shadow_decisions() -> None:
    """compute_outcome's return/MFE/MAE math is short-only -- a LONG shadow
    decision must never reach the resolver until a directional version
    exists (colleague review)."""
    statement = due_decisions_statement(
        horizons=(15, 60),
        resolver_version="forward_v1",
        retryable_statuses=("partial",),
        max_attempts=8,
        retry_after_seconds=900,
        batch_size=50,
    )

    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "trade_decisions.trading_mode IS DISTINCT FROM 'shadow'" in sql


def test_due_statement_shadow_exclusion_is_null_safe() -> None:
    """Every pump_short row has trading_mode=NULL, never 'shadow' -- the
    exclusion must use IS DISTINCT FROM, not != (SQL's != against NULL is
    NULL/falsy, which would have silently excluded every pump_short row
    too, not just shadow ones)."""
    statement = due_decisions_statement(
        horizons=(15,),
        resolver_version="forward_v1",
        retryable_statuses=("partial",),
        max_attempts=8,
        retry_after_seconds=900,
        batch_size=50,
    )

    sql = str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]

    assert "trading_mode !=" not in sql
    assert "trading_mode <>" not in sql
    assert "IS DISTINCT FROM" in sql


def test_due_statement_rejects_unscoped_extended_paths() -> None:
    with pytest.raises(ValueError, match="strategy scope"):
        due_decisions_statement(
            horizons=(20_160,),
            resolver_version="forward_v1",
            retryable_statuses=("partial",),
            max_attempts=8,
            retry_after_seconds=900,
            batch_size=50,
            extended_horizons=(20_160,),
        )


def test_upsert_statement_uses_idempotency_constraint_and_increments_attempts() -> None:
    statement = upsert_outcomes_statement(
        [_outcome()],
        resolver_version="forward_v1",
        timeframe_minutes=5,
    )

    sql = str(
        statement.compile(
            dialect=postgresql.dialect()  # type: ignore[no-untyped-call]
        )
    )

    assert "ON CONFLICT ON CONSTRAINT" in sql
    assert "uq_trade_decision_outcomes_decision_horizon_version" in sql
    assert "attempt_count = (app.trade_decision_outcomes.attempt_count +" in sql


async def test_repository_groups_due_horizons_into_one_decision() -> None:
    engine, connection = _engine_context("connect")
    result = MagicMock()
    ts = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    result.all.return_value = [
        ("00000000-0000-0000-0000-000000000001", ts, "ERA", "binance", 100, {}, 60),
        ("00000000-0000-0000-0000-000000000001", ts, "ERA", "binance", 100, {}, 15),
    ]
    connection.execute.return_value = result
    repository = OutcomeRepository(engine)

    decisions = await repository.load_due_decisions(
        horizons=(15, 60),
        resolver_version="forward_v1",
        retryable_statuses=("partial",),
        max_attempts=8,
        retry_after_seconds=900,
        batch_size=50,
    )

    assert len(decisions) == 1
    assert decisions[0].horizons == (15, 60)
    assert decisions[0].price == 100.0
    connection.execute.assert_awaited_once()


async def test_repository_does_not_open_transaction_for_empty_batch() -> None:
    engine = MagicMock()
    repository = OutcomeRepository(engine)

    await repository.persist_outcomes([], resolver_version="forward_v1", timeframe_minutes=5)

    engine.begin.assert_not_called()


async def test_repository_executes_upsert_in_transaction() -> None:
    engine, connection = _engine_context("begin")
    repository = OutcomeRepository(engine)

    await repository.persist_outcomes(
        [_outcome()], resolver_version="forward_v1", timeframe_minutes=5
    )

    connection.execute.assert_awaited_once()


async def test_repository_disposes_owned_engine() -> None:
    engine = MagicMock()
    engine.dispose = AsyncMock()
    repository = OutcomeRepository(engine)

    await repository.close()

    engine.dispose.assert_awaited_once()
