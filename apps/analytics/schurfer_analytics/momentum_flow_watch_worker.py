"""Prospective worker for ``momentum_flow_watch_v1``."""

from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

import structlog
from redis.asyncio import Redis

from .momentum_flow_watch_contract import (
    FROZEN_WATCH_CONTRACT,
    WATCH_CONTRACT_SHA256,
    WatchContract,
)
from .momentum_flow_watch_evaluator import (
    SymbolWatchState,
    WatchEvaluation,
    build_cross_section_thresholds,
    evaluate_prepared,
    prepare_symbol_evaluation,
)
from .momentum_flow_watch_repository import (
    EvaluationWrite,
    MomentumFlowWatchRepository,
    WatchBucketInput,
    WatchRun,
)

if TYPE_CHECKING:
    from collections.abc import Callable

log = structlog.get_logger()
HEALTH_KEY = "market:momentumwatch:health"


class WatchStore(Protocol):
    async def acquire_worker_lock(self, watch_version: str) -> bool: ...

    async def register_run(
        self,
        *,
        contract: WatchContract,
        contract_sha256: str,
        now: datetime,
    ) -> WatchRun: ...

    async def due_buckets(
        self,
        *,
        contract: WatchContract,
        cohort_started_at: datetime,
        limit: int,
    ) -> tuple[datetime, ...]: ...

    async def load_bucket(
        self,
        *,
        contract: WatchContract,
        bucket_start: datetime,
    ) -> WatchBucketInput | None: ...

    async def load_states(
        self,
        *,
        contract: WatchContract,
    ) -> dict[str, SymbolWatchState]: ...

    async def persist_bucket(
        self,
        writes: tuple[EvaluationWrite, ...],
        *,
        contract: WatchContract,
    ) -> None: ...


@dataclass(frozen=True)
class WatchWorkerConfig:
    database_url: str
    redis_addr: str
    poll_interval_seconds: float = 10.0
    bucket_batch_size: int = 5

    def __post_init__(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if self.bucket_batch_size <= 0:
            raise ValueError("bucket_batch_size must be positive")

    @classmethod
    def from_env(cls) -> WatchWorkerConfig:
        database_url = os.getenv("DATABASE_URL", "")
        if not database_url:
            raise ValueError("DATABASE_URL is required")
        return cls(
            database_url=database_url,
            redis_addr=os.getenv("REDIS_ADDR", "redis:6379"),
            poll_interval_seconds=float(os.getenv("MOMENTUM_WATCH_POLL_INTERVAL", "10")),
            bucket_batch_size=int(os.getenv("MOMENTUM_WATCH_BUCKET_BATCH_SIZE", "5")),
        )


@dataclass(frozen=True)
class BucketResult:
    bucket_start: datetime
    symbols_total: int
    quality_ready: int
    raw_qualified: int
    watches: int
    rejected_quality: int
    rejected_signal: int
    suppressed: int
    evaluator_duration_ms: int


async def evaluate_bucket(
    *,
    store: WatchStore,
    contract: WatchContract,
    bucket_start: datetime,
    states: dict[str, SymbolWatchState],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BucketResult | None:
    evaluator_started_at = clock()
    bucket = await store.load_bucket(contract=contract, bucket_start=bucket_start)
    if bucket is None:
        return None
    prepared = tuple(
        prepare_symbol_evaluation(
            symbol=symbol,
            bucket_start=bucket.bucket_start,
            bars=bucket.bars_by_symbol.get(symbol, ()),
            evaluator_started_at=evaluator_started_at,
            expected_universe_version=bucket.universe_version,
            contract=contract,
        )
        for symbol in bucket.symbols
    )
    thresholds = build_cross_section_thresholds(prepared, contract=contract)
    decision_at = clock()
    evaluated: list[tuple[WatchEvaluation, SymbolWatchState]] = []
    next_states: dict[str, SymbolWatchState] = {}
    for row in prepared:
        state_before = states.get(row.symbol, SymbolWatchState())
        evaluation, state_after = evaluate_prepared(
            row,
            thresholds=thresholds,
            state=state_before,
            decision_at=decision_at,
            contract=contract,
        )
        next_states[row.symbol] = state_after
        evaluated.append((evaluation, state_after))
    evaluator_completed_at = clock()
    writes = tuple(
        EvaluationWrite(
            evaluation=evaluation,
            state_after=state_after,
            state_changed=state_after != states.get(evaluation.symbol, SymbolWatchState()),
            evaluator_started_at=evaluator_started_at,
            evaluator_completed_at=evaluator_completed_at,
            decision_at=decision_at,
        )
        for evaluation, state_after in evaluated
    )
    await store.persist_bucket(writes, contract=contract)
    states.update(next_states)
    watches = [write.evaluation for write in writes if write.evaluation.decision_status == "watch"]
    for watch in watches:
        log.info(
            "momentum_watch.qualified",
            watch_version=contract.watch_version,
            watch_id=str(watch.watch_id),
            episode_id=str(watch.episode_id),
            symbol=watch.symbol,
            bucket_start=watch.bucket_start.isoformat(),
            features=asdict(watch.features) if watch.features is not None else None,
            cross_section_size=watch.thresholds.sample_size,
        )
    duration_ms = int((evaluator_completed_at - evaluator_started_at).total_seconds() * 1000)
    return BucketResult(
        bucket_start=bucket.bucket_start,
        symbols_total=len(writes),
        quality_ready=sum(write.evaluation.quality_ready for write in writes),
        raw_qualified=sum(write.evaluation.raw_qualified for write in writes),
        watches=len(watches),
        rejected_quality=sum(
            write.evaluation.decision_status == "rejected_quality" for write in writes
        ),
        rejected_signal=sum(
            write.evaluation.decision_status == "rejected_signal" for write in writes
        ),
        suppressed=sum(
            write.evaluation.decision_status.startswith("suppressed_") for write in writes
        ),
        evaluator_duration_ms=duration_ms,
    )


def _redis_url(redis_addr: str) -> str:
    if redis_addr.startswith(("redis://", "rediss://")):
        return redis_addr
    return f"redis://{redis_addr}"


async def _write_health(
    redis: Redis,
    *,
    run: WatchRun,
    result: BucketResult | None,
    status: str,
    error: str = "",
) -> None:
    now = datetime.now(UTC)
    mapping: dict[str | int | float, str | int | float] = {
        "status": status,
        "watch_version": run.watch_version,
        "contract_sha256": run.contract_sha256,
        "cohort_started_at": run.cohort_started_at.isoformat(),
        "generated_at": now.isoformat(),
        "last_error": error,
        "last_bucket_start": result.bucket_start.isoformat() if result else "",
        "symbols_total": str(result.symbols_total if result else 0),
        "quality_ready": str(result.quality_ready if result else 0),
        "raw_qualified": str(result.raw_qualified if result else 0),
        "watches": str(result.watches if result else 0),
        "rejected_quality": str(result.rejected_quality if result else 0),
        "rejected_signal": str(result.rejected_signal if result else 0),
        "suppressed": str(result.suppressed if result else 0),
        "evaluator_duration_ms": str(result.evaluator_duration_ms if result else 0),
    }
    await redis.hset(HEALTH_KEY, mapping=cast("Any", mapping))


async def _try_write_health(
    redis: Redis,
    *,
    run: WatchRun,
    result: BucketResult | None,
    status: str,
    error: str = "",
) -> None:
    try:
        await _write_health(
            redis,
            run=run,
            result=result,
            status=status,
            error=error,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("momentum_watch.health_write_failed", error=str(exc))


async def run_watch_worker(
    config: WatchWorkerConfig,
    *,
    once: bool = False,
    store: WatchStore | None = None,
) -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
    owned_store = (
        MomentumFlowWatchRepository.from_url(config.database_url) if store is None else None
    )
    active_store = owned_store or store
    if active_store is None:
        raise RuntimeError("momentum WATCH store is unavailable")
    redis = Redis.from_url(_redis_url(config.redis_addr), decode_responses=True)
    try:
        if not await active_store.acquire_worker_lock(FROZEN_WATCH_CONTRACT.watch_version):
            raise RuntimeError("another momentum WATCH worker already holds the version lock")
        run = await active_store.register_run(
            contract=FROZEN_WATCH_CONTRACT,
            contract_sha256=WATCH_CONTRACT_SHA256,
            now=datetime.now(UTC),
        )
        states = await active_store.load_states(contract=FROZEN_WATCH_CONTRACT)
        await _try_write_health(redis, run=run, result=None, status="starting")
        log.info(
            "momentum_watch.starting",
            watch_version=run.watch_version,
            contract_sha256=run.contract_sha256,
            cohort_started_at=run.cohort_started_at.isoformat(),
            restored_states=len(states),
        )
        last_result: BucketResult | None = None
        while True:
            try:
                due = await active_store.due_buckets(
                    contract=FROZEN_WATCH_CONTRACT,
                    cohort_started_at=run.cohort_started_at,
                    limit=config.bucket_batch_size,
                )
                for bucket_start in due:
                    evaluated = await evaluate_bucket(
                        store=active_store,
                        contract=FROZEN_WATCH_CONTRACT,
                        bucket_start=bucket_start,
                        states=states,
                    )
                    if evaluated is not None:
                        last_result = evaluated
                        log.info("momentum_watch.bucket_completed", **asdict(evaluated))
                await _try_write_health(redis, run=run, result=last_result, status="ok")
                if once:
                    return
                if len(due) >= config.bucket_batch_size:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("momentum_watch.tick_failed", error=str(exc))
                await _try_write_health(
                    redis,
                    run=run,
                    result=last_result,
                    status="degraded",
                    error=str(exc),
                )
                if once:
                    raise
            await asyncio.sleep(config.poll_interval_seconds)
    finally:
        await redis.aclose()
        if owned_store is not None:
            await owned_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run prospective momentum-flow WATCH")
    parser.add_argument("--once", action="store_true", help="Process due buckets once")
    args = parser.parse_args()
    asyncio.run(run_watch_worker(WatchWorkerConfig.from_env(), once=args.once))
