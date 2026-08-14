from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import pytest
from schurfer_analytics.momentum_flow_watch_contract import WatchContract
from schurfer_analytics.momentum_flow_watch_evaluator import (
    CrossSectionThresholds,
    SymbolWatchState,
    WatchEvaluation,
    WatchFeatures,
)
from schurfer_analytics.momentum_flow_watch_repository import (
    EvaluationWrite,
    MomentumFlowWatchRepository,
    evaluation_row,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


def _write(*, started_at: datetime) -> EvaluationWrite:
    bucket = datetime(2026, 8, 14, 1, 0, tzinfo=UTC)
    evaluation = WatchEvaluation(
        symbol="TESTUSDT",
        bucket_start=bucket,
        universe_version="universe-v1",
        source_event_at=bucket + timedelta(seconds=55),
        source_received_at=bucket + timedelta(seconds=56),
        bucket_ready_at=bucket + timedelta(minutes=1, seconds=2),
        quality_ready=True,
        raw_qualified=True,
        decision_status="watch",
        reason_codes=(),
        features=WatchFeatures(
            price_return_60m_pct=2.0,
            price_return_15m_pct=1.0,
            oi_growth_60m_pct=8.0,
            buy_notional_15m_usd=20_000.0,
            sell_notional_15m_usd=2_000.0,
            flow_notional_15m_usd=22_000.0,
            buy_imbalance_15m=0.8,
            flow_acceleration_15m_vs_prior_45m=3.0,
        ),
        thresholds=CrossSectionThresholds(300, 5.0, 0.5, 2.0),
        episode_id=UUID("00000000-0000-0000-0000-000000000001"),
        watch_id=UUID("00000000-0000-0000-0000-000000000002"),
    )
    state = SymbolWatchState(
        active_episode=True,
        last_watch_at=bucket,
        episode_id=evaluation.episode_id,
    )
    return EvaluationWrite(
        evaluation=evaluation,
        state_after=state,
        state_changed=True,
        evaluator_started_at=started_at,
        evaluator_completed_at=started_at + timedelta(milliseconds=50),
        decision_at=started_at + timedelta(milliseconds=50),
    )


def test_evaluation_hash_ignores_runtime_timing() -> None:
    contract = WatchContract()
    first = evaluation_row(_write(started_at=datetime(2026, 8, 14, 1, 1, tzinfo=UTC)), contract)
    second = evaluation_row(_write(started_at=datetime(2026, 8, 14, 1, 2, tzinfo=UTC)), contract)

    assert len(first["input_hash"]) == 32
    assert first["input_hash"] == second["input_hash"]


def test_evaluation_row_preserves_features_and_state() -> None:
    row = evaluation_row(
        _write(started_at=datetime(2026, 8, 14, 1, 1, tzinfo=UTC)), WatchContract()
    )

    assert row["symbol"] == "TESTUSDT"
    assert row["oi_growth_60m_pct"] == 8.0
    assert row["cross_section_size"] == 300
    assert row["state_active_after"] is True


class _Result:
    def __init__(self, rows: list[tuple[str, bytes]] | None = None) -> None:
        self._rows = rows or []

    def all(self) -> list[tuple[str, bytes]]:
        return self._rows


class _Connection:
    def __init__(self, hash_rows: list[tuple[str, bytes]]) -> None:
        self.hash_rows = hash_rows
        self.calls: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.calls.append(statement)
        if len(self.calls) == 2:
            return _Result(self.hash_rows)
        return _Result()


class _Begin:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def begin(self) -> _Begin:
        return _Begin(self.connection)


async def test_persist_bucket_updates_changed_state_and_run_cursor() -> None:
    contract = WatchContract()
    write = _write(started_at=datetime(2026, 8, 14, 1, 1, tzinfo=UTC))
    expected = evaluation_row(write, contract)
    connection = _Connection([("TESTUSDT", bytes(expected["input_hash"]))])
    repository = MomentumFlowWatchRepository(cast("AsyncEngine", _Engine(connection)))

    await repository.persist_bucket((write,), contract=contract)

    assert len(connection.calls) == 4
    assert "momentum_flow_watch_states" in str(connection.calls[2])
    assert "UPDATE app.momentum_flow_watch_runs" in str(connection.calls[3])


async def test_persist_bucket_skips_unchanged_state_write() -> None:
    contract = WatchContract()
    write = replace(
        _write(started_at=datetime(2026, 8, 14, 1, 1, tzinfo=UTC)),
        state_changed=False,
    )
    expected = evaluation_row(write, contract)
    connection = _Connection([("TESTUSDT", bytes(expected["input_hash"]))])
    repository = MomentumFlowWatchRepository(cast("AsyncEngine", _Engine(connection)))

    await repository.persist_bucket((write,), contract=contract)

    assert len(connection.calls) == 3
    assert "UPDATE app.momentum_flow_watch_runs" in str(connection.calls[2])


async def test_persist_bucket_rolls_back_before_state_on_hash_mismatch() -> None:
    contract = WatchContract()
    write = _write(started_at=datetime(2026, 8, 14, 1, 1, tzinfo=UTC))
    connection = _Connection([("TESTUSDT", b"x" * 32)])
    repository = MomentumFlowWatchRepository(cast("AsyncEngine", _Engine(connection)))

    with pytest.raises(RuntimeError, match="idempotency hash mismatch"):
        await repository.persist_bucket((write,), contract=contract)

    assert len(connection.calls) == 2
