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
    CcxtPaperMarket,
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
from .momentum_flow_producer_readiness import (
    BLOCKED_STATUS,
    DEPENDENCY_UNAVAILABLE_STATUS,
    upstream_health_is_ready,
)
from .momentum_flow_watch_worker import health_key as watch_health_key

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

log = structlog.get_logger()
HEALTH_KEY_PREFIX = "market:momentumpaper:health"


def health_key(paper_version: str) -> str:
    """Scope the Redis health key per paper_version, so a second contract's
    worker (e.g. Binance's own, see momentum_flow_paper_contract.
    BINANCE_PAPER_CONTRACT) never overwrites the first's snapshot -- same
    "no shared/masking counters" fix as momentum_flow_watch_worker.
    health_key and momentumcapture.HealthKey on the Go side (see
    docs/research/momentum-canary-multivenue-v1.md). paper_version is
    already the row-identity key acquire_worker_lock/register_run use, so
    reusing it here needs no new parameter."""
    return f"{HEALTH_KEY_PREFIX}:{paper_version}"


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

    async def due_fresh_watches(
        self,
        *,
        contract: PaperContract,
        cohort_started_at: datetime,
        now: datetime,
        limit: int,
    ) -> tuple[WatchCandidate, ...]: ...

    async def due_expired_watches(
        self,
        *,
        contract: PaperContract,
        cohort_started_at: datetime,
        now: datetime,
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
    async def bulk_reject_stale_watches(
        self,
        candidates: tuple[WatchCandidate, ...],
        *,
        contract: PaperContract,
        now: datetime,
    ) -> int: ...

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

    async def health(
        self, *, contract: PaperContract, cohort_started_at: datetime
    ) -> PaperHealth: ...


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
        # PaperWorkerConfig is built once from the environment before
        # run_paper_worker's own contract argument is chosen (dev/prod
        # entrypoints construct it the same way for every venue -- see
        # momentum_flow_paper_binance_worker.py), so this cross-check can
        # only compare against ONE frozen contract's own poll_interval_
        # seconds, not whichever one actually ends up running. Pinned to
        # FROZEN_PAPER_CONTRACT (Bybit) specifically because
        # BINANCE_PAPER_CONTRACT reuses the exact same value by design
        # (every threshold does -- see its own doc comment); if a future
        # contract ever needs a genuinely different poll cadence, this
        # check needs to become contract-aware, not just get its constant
        # swapped.
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
    entries_stale_cleaned: int
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
    allow_new_entries: bool = True,
) -> TickResult:
    now = clock()

    # 1. Process fresh candidates.
    fresh_candidates = (
        await store.due_fresh_watches(
            contract=contract,
            cohort_started_at=run.cohort_started_at,
            now=now,
            limit=config.watch_batch_size,
        )
        if allow_new_entries
        else ()
    )
    entry_results: list[str] = []
    for candidate in fresh_candidates:
        entry_results.append(
            await _process_entry(
                candidate,
                store=store,
                market=market,
                contract=contract,
                clock=clock,
            )
        )

    # 2. Service existing positions before backlog cleanup. Refresh the
    # scheduling clock after entry quotes so a slow venue call cannot make
    # deadline and monitored-probe selection use the tick's stale start time.
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

    # 3. Clean up one bounded batch in one transaction.
    cleanup_now = clock()
    expired_candidates = (
        await store.due_expired_watches(
            contract=contract,
            cohort_started_at=run.cohort_started_at,
            now=cleanup_now,
            limit=config.watch_batch_size,
        )
        if allow_new_entries
        else ()
    )
    stale_cleaned = (
        await store.bulk_reject_stale_watches(
            expired_candidates,
            contract=contract,
            now=clock(),
        )
        if expired_candidates
        else 0
    )

    return TickResult(
        watches_seen=len(fresh_candidates) + len(expired_candidates),
        entries_opened=entry_results.count("opened"),
        entries_stale=entry_results.count("stale"),
        entries_stale_cleaned=stale_cleaned,
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
    await redis.hset(health_key(run.paper_version), mapping=cast("Any", mapping))


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


async def _upstream_watch_block(redis: Redis, *, watch_version: str) -> tuple[str, str] | None:
    """Returns None if the upstream WATCH worker this contract's own
    watch_version identifies is itself both reporting status "ok" AND
    reporting it recently (see momentum_flow_producer_readiness.
    upstream_health_is_ready's own doc comment for why a stale "ok" does
    not count -- a hard-crashed WATCH process leaves its last-written
    status sitting in Redis forever otherwise). Otherwise (status, error):
    BLOCKED_STATUS if WATCH's own health says it is not ready (or has
    never run at all -- a missing key), or DEPENDENCY_UNAVAILABLE_STATUS
    if reading Redis itself failed.

    Deliberately broader than just checking for momentum_flow_producer_
    readiness.BLOCKED_STATUS specifically: paper depends on WATCH's own
    decisions entirely, so there is no separate "is paper's own upstream
    ready" question to answer independently of whatever WATCH itself
    already decided about its own readiness -- "starting"/"degraded"/a
    missing health key all mean paper has nothing real to claim right
    now either, not just the specific incompatible-producer case.
    Reading a foreign worker's own health hash by key, rather than
    re-deriving readiness from bars directly here, keeps WATCH's own
    gate the single source of truth."""
    key = watch_health_key(watch_version)
    try:
        raw_status, raw_generated_at = await redis.hmget(key, ["status", "generated_at"])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return DEPENDENCY_UNAVAILABLE_STATUS, f"could not read upstream WATCH health: {exc}"
    # redis-py's own stub types hmget's return as list[bytes | str | None]
    # regardless of decode_responses -- this client is always constructed
    # with decode_responses=True (see _redis_url's own caller), so these
    # are always str | None at runtime; str(...) is a no-op then, and only
    # exists to satisfy the stub's own wider declared type.
    status = str(raw_status) if raw_status is not None else None
    generated_at = str(raw_generated_at) if raw_generated_at is not None else None
    if upstream_health_is_ready(status=status, generated_at=generated_at):
        return None
    return BLOCKED_STATUS, (
        f"upstream WATCH worker (watch_version={watch_version!r}) is not reporting a "
        f"recent status=ok (status={status!r}, generated_at={generated_at!r})"
    )


async def run_paper_worker(
    config: PaperWorkerConfig,
    *,
    once: bool = False,
    store: PaperStore | None = None,
    market: PaperMarket | None = None,
    contract: PaperContract = FROZEN_PAPER_CONTRACT,
    contract_sha256: str = PAPER_CONTRACT_SHA256,
) -> None:
    """Run one paper worker for contract (defaults to the live Bybit
    FROZEN_PAPER_CONTRACT: every existing caller that does not pass
    contract/contract_sha256 gets the exact same acquire_worker_lock/
    register_run/due_watches/... arguments as before these parameters
    existed -- see this file's own worker tests). A second venue (see
    momentum_flow_paper_binance_worker.py) reuses this exact function
    with BINANCE_PAPER_CONTRACT instead of forking it: PaperStore and
    MomentumFlowPaperRepository were already contract-parameterized from
    the start (mirrors momentum_flow_watch_worker.run_watch_worker's own
    precedent), only this function itself hardcoded one contract."""
    if contract.sha256_hex() != contract_sha256:
        # Same defense as run_watch_worker's own identical check: contract
        # and contract_sha256 are two independently supplied parameters,
        # not derived from each other at the call site.
        raise RuntimeError(
            f"contract_sha256 does not match contract.sha256_hex() for paper_version="
            f"{contract.paper_version!r}: the (contract, contract_sha256) pair is inconsistent"
        )
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
        CcxtPaperMarket(EXCHANGE_FACTORIES[contract.source_exchange](), contract)
        if market is None
        else None
    )
    active_market = owned_market or market
    if active_market is None:
        raise RuntimeError("momentum paper market is unavailable")
    redis = Redis.from_url(_redis_url(config.redis_addr), decode_responses=True)
    try:
        if not await active_store.acquire_worker_lock(contract.paper_version):
            raise RuntimeError("another momentum paper worker already holds the version lock")
        run = await active_store.register_run(
            contract=contract,
            contract_sha256=contract_sha256,
            now=datetime.now(UTC),
        )
        interrupted = await active_store.abandon_interrupted_entries(
            contract=contract,
            now=datetime.now(UTC),
        )
        log.info(
            "momentum_paper.starting",
            paper_version=run.paper_version,
            contract_sha256=run.contract_sha256,
            cohort_started_at=run.cohort_started_at.isoformat(),
            interrupted_entries=interrupted,
        )
        starting_health = await active_store.health(
            contract=contract, cohort_started_at=run.cohort_started_at
        )
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
                # Checked BEFORE process_tick, not after: this worker owns
                # more than just opening new entries (see process_tick's
                # own allow_new_entries doc comment) -- an already-open
                # position's own stop/max-hold/horizon-outcome bookkeeping
                # must run every tick regardless of upstream readiness,
                # only claiming brand-new WATCH candidates is conditional
                # on it. Checking readiness AFTER acting (the first
                # version of this fix did) would let a tick open a
                # position before the worker had even confirmed it was
                # allowed to.
                block = await _upstream_watch_block(redis, watch_version=contract.watch_version)
                last_tick = await process_tick(
                    store=active_store,
                    market=active_market,
                    run=run,
                    config=config,
                    contract=contract,
                    allow_new_entries=block is None,
                )
                health = await active_store.health(
                    contract=contract, cohort_started_at=run.cohort_started_at
                )
                if block is not None:
                    status, error = block
                    log.warning(
                        "momentum_paper.upstream_not_ready",
                        paper_version=run.paper_version,
                        status=status,
                        error=error,
                    )
                else:
                    status, error = "ok", ""
                await _try_write_health(
                    redis,
                    run=run,
                    health=health,
                    tick=last_tick,
                    status=status,
                    error=error,
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
                    health = await active_store.health(
                        contract=contract, cohort_started_at=run.cohort_started_at
                    )
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
