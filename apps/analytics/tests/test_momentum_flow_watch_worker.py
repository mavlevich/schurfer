from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.momentum_flow_watch_contract import WatchContract
from schurfer_analytics.momentum_flow_watch_evaluator import SymbolWatchState, WatchBar
from schurfer_analytics.momentum_flow_watch_repository import (
    EvaluationWrite,
    WatchBucketInput,
    WatchRun,
)
from schurfer_analytics.momentum_flow_watch_worker import WatchWorkerConfig, evaluate_bucket


def _bars(symbol: str, *, strong: bool) -> tuple[WatchBar, ...]:
    start = datetime(2026, 8, 14, 0, 0, tzinfo=UTC)
    rows: list[WatchBar] = []
    for index in range(61):
        bucket = start + timedelta(minutes=index)
        rows.append(
            WatchBar(
                symbol=symbol,
                universe_version="universe-v1",
                bucket_start=bucket,
                created_at=bucket + timedelta(minutes=1, seconds=2),
                close_price=100.0 + (2.0 if strong else 1.0) * index / 60,
                buy_total_notional_usd=(1_000.0 if strong and index >= 46 else 100.0),
                sell_total_notional_usd=100.0,
                open_interest=100.0 + (10.0 if strong else 1.0) * index / 60,
                open_interest_event_at=bucket + timedelta(seconds=30),
                open_interest_observed_at=bucket + timedelta(seconds=31),
                last_trade_event_at=bucket + timedelta(seconds=55),
                last_trade_received_at=bucket + timedelta(seconds=56),
                last_ticker_event_at=bucket + timedelta(seconds=57),
                last_ticker_received_at=bucket + timedelta(seconds=58),
                unbackfilled_gap_minutes=0,
                complete=True,
            )
        )
    return tuple(rows)


class FakeStore:
    def __init__(self, bucket: WatchBucketInput) -> None:
        self.bucket = bucket
        self.writes: tuple[EvaluationWrite, ...] = ()

    async def acquire_worker_lock(self, watch_version: str) -> bool:
        return True

    async def register_run(
        self, *, contract: WatchContract, contract_sha256: str, now: datetime
    ) -> WatchRun:
        return WatchRun(
            contract.watch_version,
            contract_sha256,
            {},
            now,
            None,
            "active",
        )

    async def due_buckets(
        self, *, contract: WatchContract, cohort_started_at: datetime, limit: int
    ) -> tuple[datetime, ...]:
        return (self.bucket.bucket_start,)

    async def load_bucket(
        self, *, contract: WatchContract, bucket_start: datetime
    ) -> WatchBucketInput | None:
        return self.bucket

    async def load_states(self, *, contract: WatchContract) -> dict[str, SymbolWatchState]:
        return {}

    async def persist_bucket(
        self, writes: tuple[EvaluationWrite, ...], *, contract: WatchContract
    ) -> None:
        self.writes = writes


class FailingStore(FakeStore):
    async def persist_bucket(
        self, writes: tuple[EvaluationWrite, ...], *, contract: WatchContract
    ) -> None:
        raise RuntimeError("database unavailable")


async def test_evaluate_bucket_persists_watch_and_rejection_denominator() -> None:
    strong = _bars("STRONGUSDT", strong=True)
    weak = _bars("WEAKUSDT", strong=False)
    bucket = WatchBucketInput(
        bucket_start=strong[-1].bucket_start,
        universe_version="universe-v1",
        symbols=("STRONGUSDT", "WEAKUSDT"),
        bars_by_symbol={"STRONGUSDT": strong, "WEAKUSDT": weak},
    )
    store = FakeStore(bucket)
    contract = replace(
        WatchContract(),
        min_cross_section_size=2,
        min_flow_notional_usd_15m=1_000.0,
    )
    now = strong[-1].bucket_start + timedelta(minutes=1, seconds=10)

    result = await evaluate_bucket(
        store=store,
        contract=contract,
        bucket_start=bucket.bucket_start,
        states={},
        clock=lambda: now,
    )

    assert result is not None
    assert result.symbols_total == 2
    assert result.quality_ready == 2
    assert result.watches == 1
    assert result.rejected_signal == 1
    assert [write.evaluation.decision_status for write in store.writes] == [
        "watch",
        "rejected_signal",
    ]
    assert [write.state_changed for write in store.writes] == [True, False]


async def test_failed_persist_does_not_advance_in_memory_state() -> None:
    strong = _bars("STRONGUSDT", strong=True)
    bucket = WatchBucketInput(
        bucket_start=strong[-1].bucket_start,
        universe_version="universe-v1",
        symbols=("STRONGUSDT",),
        bars_by_symbol={"STRONGUSDT": strong},
    )
    store = FailingStore(bucket)
    states: dict[str, SymbolWatchState] = {}
    contract = replace(
        WatchContract(),
        min_cross_section_size=1,
        min_flow_notional_usd_15m=1_000.0,
    )
    now = strong[-1].bucket_start + timedelta(minutes=1, seconds=10)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await evaluate_bucket(
            store=store,
            contract=contract,
            bucket_start=bucket.bucket_start,
            states=states,
            clock=lambda: now,
        )

    assert states == {}


def test_worker_config_rejects_non_positive_poll_and_batch() -> None:
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        WatchWorkerConfig("postgresql://example", "redis:6379", poll_interval_seconds=0)
    with pytest.raises(ValueError, match="bucket_batch_size"):
        WatchWorkerConfig("postgresql://example", "redis:6379", bucket_batch_size=0)
