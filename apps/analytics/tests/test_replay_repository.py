from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_analytics import replay_repository
from schurfer_analytics.replay import ReplayFilters
from schurfer_analytics.replay_repository import (
    ReplayRepository,
    map_replay_row_stream,
    map_replay_rows,
    replay_inputs_statement,
)
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _filters() -> ReplayFilters:
    return ReplayFilters(
        since=datetime(2026, 7, 26, tzinfo=UTC),
        until=datetime(2026, 7, 27, tzinfo=UTC),
        required_horizons=(60, 480),
    )


def _row(horizon: int) -> dict[str, object]:
    ts = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    return {
        "row_id": 1,
        "decision_id": "00000000-0000-0000-0000-000000000001",
        "pump_event_id": 42,
        "event_base": "ERA",
        "event_first_seen_at": datetime(2026, 7, 26, 11, 55, tzinfo=UTC),
        "event_closed_at": datetime(2026, 7, 26, 13, 0, tzinfo=UTC),
        "ts": ts,
        "base": "ERA",
        "exchange": "binance",
        "action": "skipped",
        "reason": "score 5 < threshold 6",
        "score": 5,
        "pump_pct": 40,
        "price": 100,
        "strategy_version": "pump_short_v1_market_quality",
        "features": {
            "signal": {"computed_at": ts.timestamp()},
            "config": {"score_threshold": 6},
        },
        "liquidity": {"status": "sampled"},
        "horizon_minutes": horizon,
        "outcome_status": "complete",
        "anchor_exchange": "binance",
        "source_exchange": "binance",
        "entry_price": 100,
        "forward_price": 90,
        "mfe_pct": 12,
        "mae_pct": 3,
        "short_return_pct": 10,
        "coverage_ratio": 1,
    }


async def _stream_rows(*rows: dict[str, object]) -> AsyncIterator[dict[str, object]]:
    for row in rows:
        yield row


def test_statement_uses_shared_tables_parameterized_scope_and_stable_order() -> None:
    compiled = replay_inputs_statement(_filters()).compile(
        dialect=postgresql.dialect()  # type: ignore[no-untyped-call]
    )
    sql = str(compiled)

    assert "app.trade_decisions" in sql
    assert "LEFT OUTER JOIN app.pump_events" in sql
    assert "LEFT OUTER JOIN app.trade_decision_outcomes" in sql
    assert "CASE WHEN" in sql
    assert "measurement_only" in compiled.params.values()
    assert "coalesce(app.pump_events.entry_qualified_at, app.pump_events.first_seen_at)" in sql
    assert "ORDER BY app.trade_decisions.ts, app.trade_decisions.id" in sql
    assert datetime(2026, 7, 26, tzinfo=UTC) in compiled.params.values()
    assert "pump_short_v1_market_quality" not in compiled.params.values()
    assert "pump_short_measurement_v1" in compiled.params.values()
    assert "IS DISTINCT FROM" in sql
    assert " @> " in sql
    assert "IS NOT true" in sql


def test_statement_keeps_measurement_rows_when_they_are_requested() -> None:
    filters = replace(_filters(), strategy_versions=("pump_short_measurement_v1",))
    compiled = replay_inputs_statement(filters).compile(
        dialect=postgresql.dialect()  # type: ignore[no-untyped-call]
    )

    assert "IS DISTINCT FROM" not in str(compiled)


def test_row_mapper_groups_and_sorts_outcomes_for_one_decision() -> None:
    with patch.object(
        replay_repository,
        "_builder",
        wraps=replay_repository._builder,
    ) as build:
        decisions = map_replay_rows([_row(480), _row(60)])

    assert len(decisions) == 1
    assert build.call_count == 1
    assert decisions[0].pump_event_id == 42
    assert [outcome.horizon_minutes for outcome in decisions[0].outcomes] == [60, 480]
    assert decisions[0].price == 100.0


async def test_streaming_row_mapper_freezes_ordered_decisions_incrementally() -> None:
    second = {
        **_row(60),
        "row_id": 2,
        "decision_id": "00000000-0000-0000-0000-000000000002",
        "pump_event_id": 43,
    }

    decisions = await map_replay_row_stream(_stream_rows(_row(480), _row(60), second))

    assert [decision.row_id for decision in decisions] == [1, 2]
    assert [outcome.horizon_minutes for outcome in decisions[0].outcomes] == [60, 480]


async def test_streaming_row_mapper_rejects_non_contiguous_decisions() -> None:
    second = {
        **_row(60),
        "row_id": 2,
        "decision_id": "00000000-0000-0000-0000-000000000002",
    }

    with pytest.raises(ValueError, match="contiguous"):
        await map_replay_row_stream(_stream_rows(_row(60), second, _row(480)))


def test_repository_factory_uses_bounded_read_pool() -> None:
    engine = MagicMock()

    with patch(
        "schurfer_analytics.replay_repository.create_async_engine",
        return_value=engine,
    ) as create:
        repository = ReplayRepository.from_url("postgresql://user:password@db/schurfer")

    assert repository is not None
    create.assert_called_once()
    assert create.call_args.kwargs == {
        "pool_pre_ping": True,
        "pool_size": 1,
        "max_overflow": 0,
    }


async def test_repository_reads_one_repeatable_read_snapshot() -> None:
    result = MagicMock()
    result.mappings.return_value = _stream_rows(_row(60), _row(480))
    connection = MagicMock()
    connection.stream = AsyncMock(return_value=result)
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    connection.begin.return_value = transaction
    raw_connection = MagicMock()
    raw_connection.execution_options = AsyncMock(return_value=connection)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=raw_connection)
    context.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.connect.return_value = context

    decisions = await ReplayRepository(engine).load(_filters())

    raw_connection.execution_options.assert_awaited_once_with(
        isolation_level="REPEATABLE READ",
        postgresql_readonly=True,
    )
    connection.stream.assert_awaited_once()
    assert connection.stream.call_args.kwargs == {"execution_options": {"yield_per": 500}}
    assert len(decisions) == 1


async def test_repository_disposes_engine() -> None:
    engine = MagicMock()
    engine.dispose = AsyncMock()

    await ReplayRepository(engine).close()

    engine.dispose.assert_awaited_once()
