"""Prospective exact-venue paper-long worker for momentum WATCH decisions."""

from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

import structlog
from redis.asyncio import Redis

from .exchange_registry import EXCHANGE_FACTORIES
from .momentum_flow_paper_contract import (
    FROZEN_PAPER_CONTRACT,
    PAPER_CONTRACT_SHA256,
    PaperContract,
)
from .momentum_flow_paper_market import (
    BybitPaperMarket,
    ExecutableQuote,
    QuoteFailure,
    QuoteResult,
)
from .momentum_flow_paper_repository import (
    MomentumFlowPaperRepository,
    PaperHealth,
    PaperProbe,
    PaperRun,
    WatchCandidate,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

log = structlog.get_logger()
HEALTH_KEY = "market:momentumpaper:health"


class PaperStore(Protocol):
    async def acquire_worker_lock(self, paper_version: str) -> bool: ...

    async def register_run(
        self,
        *,
        contract: PaperContract,
        contract_sha256: str,
        now: datetime,
    ) -> PaperRun: ...

    async def abandon_interrupted_entries(
        self, *, contract: PaperContract, now: datetime
    ) -> int: ...

    async def due_watches(
        self,
        *,
        contract: PaperContract,
        cohort_started_at: datetime,
        limit: int,
    ) -> tuple[WatchCandidate, ...]: ...

    async def claim_watch(
        self,
        candidate: WatchCandidate,
        *,
        contract: PaperContract,
        now: datetime,
    ) -> UUID | None: ...

    async def reject_stale_entry(self, paper_id: UUID, *, now: datetime) -> None: ...

    async def reject_quote(self, paper_id: UUID, failure: QuoteFailure) -> None: ...

    async def open_entry(
        self,
        paper_id: UUID,
        quote: ExecutableQuote,
        *,
        contract: PaperContract,
    ) -> None: ...

    async def expire_deadlines(
        self,
        *,
        contract: PaperContract,
        now: datetime,
    ) -> tuple[int, int]: ...

    async def monitored_probes(
        self,
        *,
        contract: PaperContract,
        now: datetime,
        limit: int,
    ) -> tuple[PaperProbe, ...]: ...

    async def pending_horizons(self, paper_id: UUID, *, now: datetime) -> tuple[int, ...]: ...

    async def apply_quote(
        self,
        probe: PaperProbe,
        quote: ExecutableQuote,
        *,
        due_horizons: tuple[int, ...],
        contract: PaperContract,
    ) -> str | None: ...

    async def record_quote_failure(self, paper_id: UUID, failure: QuoteFailure) -> None: ...

    async def health(self, *, contract: PaperContract) -> PaperHealth: ...


class PaperMarket(Protocol):
    async def quote(self, symbol: str, side: str) -> QuoteResult: ...


@dataclass(frozen=True)
class PaperWorkerConfig:
    database_url: str
    redis_addr: str
    poll_interval_seconds: float = FROZEN_PAPER_CONTRACT.poll_interval_seconds
    watch_batch_size: int = 20
    probe_batch_size: int = 100

    def __post_init__(self) -> None:
        if not self.database_url:
            raise ValueError("DATABASE_URL is required")
        if min(self.poll_interval_seconds, self.watch_batch_size, self.probe_batch_size) <= 0:
            raise ValueError("momentum paper worker limits must be positive")

    @classmethod
    def from_env(cls) -> PaperWorkerConfig:
        config = cls(
            database_url=os.getenv("DATABASE_URL", ""),
            redis_addr=os.getenv("REDIS_ADDR", "redis:6379"),
            poll_interval_seconds=float(
                os.getenv(
                    "MOMENTUM_PAPER_POLL_INTERVAL",
                    str(FROZEN_PAPER_CONTRACT.poll_interval_seconds),
                )
            ),
            watch_batch_size=int(os.getenv("MOMENTUM_PAPER_WATCH_BATCH_SIZE", "20")),
            probe_batch_size=int(os.getenv("MOMENTUM_PAPER_PROBE_BATCH_SIZE", "100")),
        )
        if config.poll_interval_seconds != FROZEN_PAPER_CONTRACT.poll_interval_seconds:
            raise ValueError(
                "MOMENTUM_PAPER_POLL_INTERVAL cannot override the frozen paper contract"
            )
        return config


@dataclass(frozen=True)
class TickResult:
    watches_seen: int
    entries_opened: int
    entries_stale: int
    entries_quote_rejected: int
    probes_quoted: int
    quote_failures: int
    positions_closed: int
    outcomes_expired: int
    exits_unresolved: int


def _redis_url(redis_addr: str) -> str:
    if redis_addr.startswith(("redis://", "rediss://")):
        return redis_addr
    return f"redis://{redis_addr}"


async def _process_entry(
    candidate: WatchCandidate,
    *,
    store: PaperStore,
    market: PaperMarket,
    contract: PaperContract,
    clock: Callable[[], datetime],
) -> str:
    claimed_at = clock()
    paper_id = await store.claim_watch(
        candidate,
        contract=contract,
        now=claimed_at,
    )
    if paper_id is None:
        return "already_claimed"
    if (claimed_at - candidate.decision_at).total_seconds() > contract.max_watch_to_quote_seconds:
        await store.reject_stale_entry(paper_id, now=claimed_at)
        return "stale"
    result = await market.quote(candidate.symbol, contract.entry_quote_side)
    if isinstance(result, QuoteFailure):
        await store.reject_quote(paper_id, result)
        return "quote_rejected"
    if (
        result.requested_at - candidate.decision_at
    ).total_seconds() > contract.max_watch_to_quote_seconds:
        await store.reject_stale_entry(paper_id, now=result.observed_at)
        return "stale"
    await store.open_entry(paper_id, result, contract=contract)
    log.info(
        "momentum_paper.opened",
        paper_version=contract.paper_version,
        paper_id=str(paper_id),
        watch_id=str(candidate.watch_id),
        episode_id=str(candidate.episode_id),
        symbol=candidate.symbol,
        entry_vwap=result.vwap,
        spread_bps=result.spread_bps,
        entry_impact_bps=result.impact_bps,
        watch_to_quote_ms=round(
            (result.requested_at - candidate.decision_at).total_seconds() * 1000
        ),
    )
    return "opened"


async def _process_probe(
    probe: PaperProbe,
    *,
    store: PaperStore,
    market: PaperMarket,
    contract: PaperContract,
) -> tuple[bool, bool]:
    result = await market.quote(probe.symbol, contract.exit_quote_side)
    if isinstance(result, QuoteFailure):
        await store.record_quote_failure(probe.paper_id, result)
        return False, False
    due = await store.pending_horizons(probe.paper_id, now=result.observed_at)
    exit_reason = await store.apply_quote(
        probe,
        result,
        due_horizons=due,
        contract=contract,
    )
    if exit_reason is not None:
        log.info(
            "momentum_paper.closed",
            paper_version=contract.paper_version,
            paper_id=str(probe.paper_id),
            symbol=probe.symbol,
            exit_reason=exit_reason,
            exit_vwap=result.vwap,
            gross_return_pct=(result.vwap / probe.entry_vwap - 1.0) * 100.0,
        )
    return True, exit_reason is not None


async def process_tick(
    *,
    store: PaperStore,
    market: PaperMarket,
    run: PaperRun,
    config: PaperWorkerConfig,
    contract: PaperContract = FROZEN_PAPER_CONTRACT,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> TickResult:
    candidates = await store.due_watches(
        contract=contract,
        cohort_started_at=run.cohort_started_at,
        limit=config.watch_batch_size,
    )
    entry_results: list[str] = []
    for candidate in candidates:
        entry_results.append(
            await _process_entry(
                candidate,
                store=store,
                market=market,
                contract=contract,
                clock=clock,
            )
        )
    expired_outcomes, unresolved_exits = await store.expire_deadlines(
        contract=contract,
        now=clock(),
    )
    probes = await store.monitored_probes(
        contract=contract,
        now=clock(),
        limit=config.probe_batch_size,
    )
    probes_quoted = 0
    quote_failures = 0
    positions_closed = 0
    for probe in probes:
        quoted, closed = await _process_probe(
            probe,
            store=store,
            market=market,
            contract=contract,
        )
        probes_quoted += int(quoted)
        quote_failures += int(not quoted)
        positions_closed += int(closed)
    return TickResult(
        watches_seen=len(candidates),
        entries_opened=entry_results.count("opened"),
        entries_stale=entry_results.count("stale"),
        entries_quote_rejected=entry_results.count("quote_rejected"),
        probes_quoted=probes_quoted,
        quote_failures=quote_failures,
        positions_closed=positions_closed,
        outcomes_expired=expired_outcomes,
        exits_unresolved=unresolved_exits,
    )


async def _write_health(
    redis: Redis,
    *,
    run: PaperRun,
    health: PaperHealth,
    tick: TickResult | None,
    status: str,
    error: str = "",
) -> None:
    def health_value(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    mapping: dict[str | int | float, str | int | float] = {
        "status": status,
        "paper_version": run.paper_version,
        "contract_sha256": run.contract_sha256,
        "cohort_started_at": run.cohort_started_at.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "last_error": error,
        **{key: health_value(value) for key, value in asdict(health).items()},
    }
    if tick is not None:
        mapping.update({f"last_tick_{key}": str(value) for key, value in asdict(tick).items()})
    await redis.hset(HEALTH_KEY, mapping=cast("Any", mapping))


async def _try_write_health(
    redis: Redis,
    *,
    run: PaperRun,
    health: PaperHealth,
    tick: TickResult | None,
    status: str,
    error: str = "",
) -> None:
    try:
        await _write_health(
            redis,
            run=run,
            health=health,
            tick=tick,
            status=status,
            error=error,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("momentum_paper.health_write_failed", error=str(exc))


async def run_paper_worker(
    config: PaperWorkerConfig,
    *,
    once: bool = False,
    store: PaperStore | None = None,
    market: PaperMarket | None = None,
) -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
    owned_store = (
        MomentumFlowPaperRepository.from_url(config.database_url) if store is None else None
    )
    active_store = owned_store or store
    if active_store is None:
        raise RuntimeError("momentum paper store is unavailable")
    owned_market = (
        BybitPaperMarket(
            EXCHANGE_FACTORIES[FROZEN_PAPER_CONTRACT.source_exchange](), FROZEN_PAPER_CONTRACT
        )
        if market is None
        else None
    )
    active_market = owned_market or market
    if active_market is None:
        raise RuntimeError("momentum paper market is unavailable")
    redis = Redis.from_url(_redis_url(config.redis_addr), decode_responses=True)
    try:
        if not await active_store.acquire_worker_lock(FROZEN_PAPER_CONTRACT.paper_version):
            raise RuntimeError("another momentum paper worker already holds the version lock")
        run = await active_store.register_run(
            contract=FROZEN_PAPER_CONTRACT,
            contract_sha256=PAPER_CONTRACT_SHA256,
            now=datetime.now(UTC),
        )
        interrupted = await active_store.abandon_interrupted_entries(
            contract=FROZEN_PAPER_CONTRACT,
            now=datetime.now(UTC),
        )
        log.info(
            "momentum_paper.starting",
            paper_version=run.paper_version,
            contract_sha256=run.contract_sha256,
            cohort_started_at=run.cohort_started_at.isoformat(),
            interrupted_entries=interrupted,
        )
        starting_health = await active_store.health(contract=FROZEN_PAPER_CONTRACT)
        await _try_write_health(
            redis,
            run=run,
            health=starting_health,
            tick=None,
            status="starting",
        )
        last_tick: TickResult | None = None
        while True:
            try:
                last_tick = await process_tick(
                    store=active_store,
                    market=active_market,
                    run=run,
                    config=config,
                )
                health = await active_store.health(contract=FROZEN_PAPER_CONTRACT)
                await _try_write_health(
                    redis,
                    run=run,
                    health=health,
                    tick=last_tick,
                    status="ok",
                )
                if any(asdict(last_tick).values()):
                    log.info("momentum_paper.tick_completed", **asdict(last_tick))
                if once:
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("momentum_paper.tick_failed", error=str(exc))
                try:
                    health = await active_store.health(contract=FROZEN_PAPER_CONTRACT)
                except Exception as health_exc:
                    log.warning("momentum_paper.health_read_failed", error=str(health_exc))
                else:
                    await _try_write_health(
                        redis,
                        run=run,
                        health=health,
                        tick=last_tick,
                        status="degraded",
                        error=str(exc),
                    )
                if once:
                    raise
            await asyncio.sleep(config.poll_interval_seconds)
    finally:
        await redis.aclose()
        if owned_market is not None:
            await owned_market.close()
        if owned_store is not None:
            await owned_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run prospective momentum-flow paper probe")
    parser.add_argument("--once", action="store_true", help="Process one paper tick")
    args = parser.parse_args()
    asyncio.run(run_paper_worker(PaperWorkerConfig.from_env(), once=args.once))
