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

from .momentum_flow_producer_readiness import (
    BLOCKED_STATUS,
    DEPENDENCY_UNAVAILABLE_STATUS,
    PRICE_READINESS_LOOKBACK_MINUTES,
)
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
HEALTH_KEY_PREFIX = "market:momentumwatch:health"


def health_key(watch_version: str) -> str:
    """Scope the Redis health key per watch_version, so a second contract's
    worker (e.g. Binance's own, see momentum_flow_watch_contract.
    BINANCE_WATCH_CONTRACT) never overwrites the first's snapshot -- same
    "no shared/masking counters" fix as momentumcapture.HealthKey on the Go
    side (see docs/research/momentum-canary-multivenue-v1.md). watch_version
    is already the row-identity key acquire_worker_lock/register_run use, so
    reusing it here needs no new parameter."""
    return f"{HEALTH_KEY_PREFIX}:{watch_version}"


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

    async def has_any_recent_valid_price(
        self,
        *,
        contract: WatchContract,
        lookback_minutes: int,
    ) -> bool: ...

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


async def _price_readiness_block(
    store: WatchStore, *, contract: WatchContract
) -> tuple[str, str] | None:
    """Returns None if ready. Otherwise (status, error): BLOCKED_STATUS if
    the check ran and genuinely found no valid recent price, or
    DEPENDENCY_UNAVAILABLE_STATUS if the check itself could not run (a
    transient DB error) -- see momentum_flow_producer_readiness's own doc
    comment for why these are kept distinct rather than both collapsing
    to BLOCKED_STATUS. Either way this never raises: the caller is
    expected to keep looping and recheck next tick, not crash (see
    run_watch_worker's own doc comment on why a startup-only gate that
    raises was replaced with this)."""
    try:
        ready = await store.has_any_recent_valid_price(
            contract=contract, lookback_minutes=PRICE_READINESS_LOOKBACK_MINUTES
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return DEPENDENCY_UNAVAILABLE_STATUS, f"price readiness check itself failed: {exc}"
    if ready:
        return None
    return BLOCKED_STATUS, (
        f"no valid recent bar (close_price > 0, complete) in the last "
        f"{PRICE_READINESS_LOOKBACK_MINUTES} minutes for exchange="
        f"{contract.source_exchange!r} capture_version={contract.capture_version!r} "
        "-- this venue's own capture producer does not (yet) satisfy what "
        "momentum_flow_watch needs. Note: this only confirms SOME symbol "
        "has a valid price, not that the whole cross-section/OI is ready "
        "-- see docs/research/binance-watch-input-readiness-v1.md."
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
    await redis.hset(health_key(run.watch_version), mapping=cast("Any", mapping))


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
    contract: WatchContract = FROZEN_WATCH_CONTRACT,
    contract_sha256: str = WATCH_CONTRACT_SHA256,
) -> None:
    """Run one WATCH worker for contract (defaults to the live Bybit
    FROZEN_WATCH_CONTRACT: every existing caller that does not pass
    contract/contract_sha256 gets the exact same acquire_worker_lock/
    register_run/due_buckets/load_states arguments as before these
    parameters existed -- see this file's own worker tests). A second
    venue (see momentum_flow_watch_binance_worker.py) reuses this exact
    function with BINANCE_WATCH_CONTRACT instead of forking it:
    evaluate_bucket, WatchStore, and MomentumFlowWatchRepository were
    already contract-parameterized from the start, so nothing here needed
    to change to serve a second venue, only to stop hardcoding one."""
    if contract.sha256_hex() != contract_sha256:
        # contract and contract_sha256 are two independently supplied
        # parameters; every call site in this repo sources both from the
        # same module-level pair in momentum_flow_watch_contract.py
        # (itself guarded by an identical check at import time), but
        # nothing forces a future caller to. Checked here too rather than
        # trusting the caller, since register_run's own equality check
        # only compares contract_sha256 against the DB row IT just wrote
        # from that same value -- tautological, not a real cross-check.
        raise RuntimeError(
            f"contract_sha256 does not match contract.sha256_hex() for watch_version="
            f"{contract.watch_version!r}: the (contract, contract_sha256) pair is inconsistent"
        )
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
        if not await active_store.acquire_worker_lock(contract.watch_version):
            raise RuntimeError("another momentum WATCH worker already holds the version lock")
        run = await active_store.register_run(
            contract=contract,
            contract_sha256=contract_sha256,
            now=datetime.now(UTC),
        )
        states = await active_store.load_states(contract=contract)
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
                # Readiness check every tick, not a startup-only gate: a
                # worker that raised here used to crash-loop under Docker's
                # own restart: unless-stopped policy the whole time its
                # producer stayed incompatible -- pure churn, since nothing
                # about restarting changes the producer. Staying in the
                # loop and reporting BLOCKED_STATUS/DEPENDENCY_UNAVAILABLE_
                # STATUS instead means recovery (once the producer becomes
                # ready) needs no restart at all. Known tradeoff: a
                # brand-new venue's very first tick, moments after its own
                # capture process starts, has no bars at all yet in the
                # lookback window either -- this cannot distinguish that
                # from a structurally incompatible producer and reports
                # blocked either way. Acceptable for now (every venue this
                # repo captures today has run for hours before its own
                # WATCH worker is ever started).
                blocked = await _price_readiness_block(active_store, contract=contract)
                if blocked is not None:
                    status, error = blocked
                    log.warning(
                        "momentum_watch.producer_not_ready",
                        watch_version=run.watch_version,
                        status=status,
                        error=error,
                    )
                    await _try_write_health(
                        redis, run=run, result=last_result, status=status, error=error
                    )
                    if once:
                        return
                    await asyncio.sleep(config.poll_interval_seconds)
                    continue
                due = await active_store.due_buckets(
                    contract=contract,
                    cohort_started_at=run.cohort_started_at,
                    limit=config.bucket_batch_size,
                )
                for bucket_start in due:
                    evaluated = await evaluate_bucket(
                        store=active_store,
                        contract=contract,
                        bucket_start=bucket_start,
                        states=states,
                    )
                    if evaluated is not None:
                        last_result = evaluated
                        log.info(
                            "momentum_watch.bucket_completed",
                            watch_version=contract.watch_version,
                            **asdict(evaluated),
                        )
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
