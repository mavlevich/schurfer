from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from schurfer_analytics.momentum_flow_paper_contract import (
    BINANCE_PAPER_CONTRACT,
    BINANCE_PAPER_CONTRACT_SHA256,
    FROZEN_PAPER_CONTRACT,
    PAPER_CONTRACT_SHA256,
    PaperContract,
)
from schurfer_analytics.momentum_flow_paper_market import ExecutableQuote, QuoteFailure
from schurfer_analytics.momentum_flow_paper_repository import (
    PaperHealth,
    PaperProbe,
    PaperRun,
    WatchCandidate,
)
from schurfer_analytics.momentum_flow_paper_worker import (
    PaperWorkerConfig,
    _process_entry,
    health_key,
    process_tick,
    run_paper_worker,
)

T0 = datetime(2026, 8, 14, 12, tzinfo=UTC)


def _candidate(*, decision_at: datetime = T0) -> WatchCandidate:
    return WatchCandidate(
        watch_id=uuid4(),
        episode_id=uuid4(),
        exchange="bybit",
        market_type="linear",
        symbol="ERAUSDT",
        bucket_start=decision_at - timedelta(minutes=1),
        decision_at=decision_at,
    )


def _quote(
    *, observed_at: datetime = T0 + timedelta(seconds=2), side: str = "ask"
) -> ExecutableQuote:
    return ExecutableQuote(
        symbol="ERAUSDT",
        unified_symbol="ERA/USDT:USDT",
        market_id="ERAUSDT",
        side=side,
        requested_at=observed_at - timedelta(seconds=1),
        observed_at=observed_at,
        exchange_event_at=observed_at - timedelta(milliseconds=100),
        latency_ms=1000,
        best_bid=9.9,
        best_ask=10.1,
        mid=10,
        spread_bps=200,
        vwap=10.1 if side == "ask" else 9.9,
        impact_bps=100,
        filled_notional_usd=50,
        contract_size=1,
    )


@dataclass
class FakeMarket:
    results: list[ExecutableQuote | QuoteFailure]

    async def quote(self, symbol: str, side: str) -> ExecutableQuote | QuoteFailure:
        result = self.results.pop(0)
        assert result.symbol == symbol
        assert result.side == side
        return result


class FakeStore:
    def __init__(self, candidates: tuple[WatchCandidate, ...] = ()) -> None:
        self.candidates = candidates
        self.claimed: list[UUID] = []
        self.opened: list[UUID] = []
        self.stale: list[UUID] = []
        self.rejected: list[UUID] = []
        self.probes: tuple[PaperProbe, ...] = ()
        self.applied: list[UUID] = []

    async def acquire_worker_lock(self, paper_version: str) -> bool:
        return True

    async def register_run(self, **_: Any) -> PaperRun:
        return PaperRun(FROZEN_PAPER_CONTRACT.paper_version, "hash", {}, T0, "active")

    async def abandon_interrupted_entries(self, **_: Any) -> int:
        return 0

    async def due_watches(self, **_: Any) -> tuple[WatchCandidate, ...]:
        return self.candidates

    async def claim_watch(self, candidate: WatchCandidate, **_: Any) -> UUID:
        paper_id = uuid4()
        self.claimed.append(paper_id)
        return paper_id

    async def reject_stale_entry(self, paper_id: UUID, **_: Any) -> None:
        self.stale.append(paper_id)

    async def reject_quote(self, paper_id: UUID, failure: QuoteFailure) -> None:
        self.rejected.append(paper_id)

    async def open_entry(self, paper_id: UUID, quote: ExecutableQuote, **_: Any) -> None:
        self.opened.append(paper_id)

    async def expire_deadlines(self, **_: Any) -> tuple[int, int]:
        return 0, 0

    async def monitored_probes(self, **_: Any) -> tuple[PaperProbe, ...]:
        return self.probes

    async def pending_horizons(self, paper_id: UUID, **_: Any) -> tuple[int, ...]:
        return (5,)

    async def apply_quote(self, probe: PaperProbe, quote: ExecutableQuote, **_: Any) -> str | None:
        self.applied.append(probe.paper_id)
        return None

    async def record_quote_failure(self, paper_id: UUID, failure: QuoteFailure) -> None:
        self.rejected.append(paper_id)

    async def health(self, **_: Any) -> PaperHealth:
        return PaperHealth(0, 0, 0, 0, 0, 0, 0, 0, 0, None, None)


async def test_process_entry_opens_fresh_exact_quote() -> None:
    store = FakeStore()
    candidate = _candidate()

    result = await _process_entry(
        candidate,
        store=store,
        market=FakeMarket([_quote()]),
        contract=FROZEN_PAPER_CONTRACT,
        clock=lambda: T0 + timedelta(seconds=1),
    )

    assert result == "opened"
    assert store.opened == store.claimed


async def test_process_entry_rejects_stale_without_fetching_quote() -> None:
    store = FakeStore()
    candidate = _candidate()
    market = FakeMarket([])

    result = await _process_entry(
        candidate,
        store=store,
        market=market,
        contract=FROZEN_PAPER_CONTRACT,
        clock=lambda: T0 + timedelta(seconds=31),
    )

    assert result == "stale"
    assert store.stale == store.claimed
    assert market.results == []


async def test_process_tick_counts_entry_and_probe_results() -> None:
    candidate = _candidate()
    store = FakeStore((candidate,))
    probe_id = uuid4()
    store.probes = (PaperProbe(probe_id, "ERAUSDT", T0, 10, "open", 0, 0),)
    run = PaperRun(
        FROZEN_PAPER_CONTRACT.paper_version,
        "hash",
        {},
        T0 - timedelta(seconds=1),
        "active",
    )
    config = PaperWorkerConfig("postgresql://test", "redis:6379")

    result = await process_tick(
        store=store,
        market=FakeMarket([_quote(), _quote(side="bid")]),
        run=run,
        config=config,
        clock=lambda: T0 + timedelta(seconds=1),
    )

    assert result.entries_opened == 1
    assert result.probes_quoted == 1
    assert store.applied == [probe_id]


def test_runtime_cannot_override_frozen_poll_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setenv("MOMENTUM_PAPER_POLL_INTERVAL", "10")

    with pytest.raises(ValueError, match="frozen paper contract"):
        PaperWorkerConfig.from_env()


def test_health_key_scopes_by_paper_version() -> None:
    assert (
        health_key("momentum_flow_paper_v1") == "market:momentumpaper:health:momentum_flow_paper_v1"
    )
    # Regression: this is the actual "no shared/masking counters" case --
    # two contracts' own health snapshots must never collide on one key.
    assert health_key("momentum_flow_paper_v1_binance") != health_key("momentum_flow_paper_v1")


class _FakeRedis:
    """Stands in for redis.asyncio.Redis: records every hset call instead
    of touching a real (or fake) network connection."""

    def __init__(self) -> None:
        self.hset_calls: list[tuple[str, dict[str, str]]] = []

    async def hset(self, key: str, mapping: dict[str, str]) -> None:
        self.hset_calls.append((key, dict(mapping)))

    async def aclose(self) -> None:
        pass


def _install_fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fake = _FakeRedis()

    class _FakeRedisFactory:
        @staticmethod
        def from_url(url: str, **_kwargs: object) -> _FakeRedis:
            return fake

    monkeypatch.setattr("schurfer_analytics.momentum_flow_paper_worker.Redis", _FakeRedisFactory)
    return fake


class RecordingStore:
    """A PaperStore that records exactly which contract each method was
    called with, for asserting run_paper_worker threads its own contract
    parameter through end to end instead of silently falling back to a
    hardcoded default anywhere along the way. due_watches/monitored_probes
    always return empty so _process_entry/_process_probe are never
    reached -- this class tests contract plumbing, not entry/exit
    business logic (see FakeStore's own tests above for that)."""

    def __init__(self) -> None:
        self.lock_calls: list[str] = []
        self.register_run_calls: list[PaperContract] = []
        self.abandon_calls: list[PaperContract] = []
        self.due_watches_calls: list[PaperContract] = []
        self.expire_deadlines_calls: list[PaperContract] = []
        self.monitored_probes_calls: list[PaperContract] = []
        self.health_calls: list[PaperContract] = []

    async def acquire_worker_lock(self, paper_version: str) -> bool:
        self.lock_calls.append(paper_version)
        return True

    async def register_run(
        self, *, contract: PaperContract, contract_sha256: str, now: datetime
    ) -> PaperRun:
        self.register_run_calls.append(contract)
        return PaperRun(contract.paper_version, contract_sha256, {}, now, "active")

    async def abandon_interrupted_entries(self, *, contract: PaperContract, now: datetime) -> int:
        self.abandon_calls.append(contract)
        return 0

    async def due_watches(
        self, *, contract: PaperContract, cohort_started_at: datetime, limit: int
    ) -> tuple[WatchCandidate, ...]:
        self.due_watches_calls.append(contract)
        return ()

    async def claim_watch(self, candidate: WatchCandidate, **_: Any) -> UUID | None:
        raise AssertionError("no candidate should ever be claimed: due_watches returns none")

    async def reject_stale_entry(self, paper_id: UUID, **_: Any) -> None:
        raise AssertionError("unreachable")

    async def reject_quote(self, paper_id: UUID, failure: QuoteFailure) -> None:
        raise AssertionError("unreachable")

    async def open_entry(self, paper_id: UUID, quote: ExecutableQuote, **_: Any) -> None:
        raise AssertionError("unreachable")

    async def expire_deadlines(self, *, contract: PaperContract, now: datetime) -> tuple[int, int]:
        self.expire_deadlines_calls.append(contract)
        return 0, 0

    async def monitored_probes(
        self, *, contract: PaperContract, now: datetime, limit: int
    ) -> tuple[PaperProbe, ...]:
        self.monitored_probes_calls.append(contract)
        return ()

    async def pending_horizons(self, paper_id: UUID, **_: Any) -> tuple[int, ...]:
        raise AssertionError("unreachable")

    async def apply_quote(self, probe: PaperProbe, quote: ExecutableQuote, **_: Any) -> str | None:
        raise AssertionError("unreachable")

    async def record_quote_failure(self, paper_id: UUID, failure: QuoteFailure) -> None:
        raise AssertionError("unreachable")

    async def health(self, *, contract: PaperContract) -> PaperHealth:
        self.health_calls.append(contract)
        return PaperHealth(0, 0, 0, 0, 0, 0, 0, 0, 0, None, None)


async def test_run_paper_worker_defaults_to_the_live_bybit_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: run_paper_worker gained contract/contract_sha256
    parameters after already being in production for the Bybit worker.
    Every existing caller (main(), and the live deploy) must see
    byte-identical behavior when it does not pass them explicitly."""
    fake_redis = _install_fake_redis(monkeypatch)
    store = RecordingStore()
    market = FakeMarket([])
    config = PaperWorkerConfig("postgresql://example", "redis:6379")

    await run_paper_worker(config, once=True, store=store, market=market)

    assert store.lock_calls == [FROZEN_PAPER_CONTRACT.paper_version]
    assert store.register_run_calls == [FROZEN_PAPER_CONTRACT]
    assert store.due_watches_calls == [FROZEN_PAPER_CONTRACT]
    assert store.monitored_probes_calls == [FROZEN_PAPER_CONTRACT]
    assert fake_redis.hset_calls
    key, mapping = fake_redis.hset_calls[-1]
    assert key == health_key(FROZEN_PAPER_CONTRACT.paper_version)
    assert mapping["paper_version"] == FROZEN_PAPER_CONTRACT.paper_version


async def test_run_paper_worker_threads_a_different_contract_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual regression this PR exists to prevent: passing
    BINANCE_PAPER_CONTRACT must reach every store call, the market
    factory's own source_exchange selection, AND the health key, not
    silently fall back to the hardcoded Bybit default anywhere."""
    fake_redis = _install_fake_redis(monkeypatch)
    store = RecordingStore()
    market = FakeMarket([])
    config = PaperWorkerConfig("postgresql://example", "redis:6379")

    await run_paper_worker(
        config,
        once=True,
        store=store,
        market=market,
        contract=BINANCE_PAPER_CONTRACT,
        contract_sha256=BINANCE_PAPER_CONTRACT_SHA256,
    )

    assert store.lock_calls == [BINANCE_PAPER_CONTRACT.paper_version]
    assert store.register_run_calls == [BINANCE_PAPER_CONTRACT]
    assert store.due_watches_calls == [BINANCE_PAPER_CONTRACT]
    assert store.monitored_probes_calls == [BINANCE_PAPER_CONTRACT]
    key, mapping = fake_redis.hset_calls[-1]
    assert key == health_key(BINANCE_PAPER_CONTRACT.paper_version)
    assert key != health_key(FROZEN_PAPER_CONTRACT.paper_version)
    assert mapping["paper_version"] == BINANCE_PAPER_CONTRACT.paper_version


def test_binance_paper_contract_reuses_every_frozen_threshold_verbatim() -> None:
    """Regression for ROADMAP's own "frozen v1 logic" instruction (same
    convention as BINANCE_WATCH_CONTRACT's own test): only identity fields
    may differ from the live Bybit contract -- every threshold this
    contract is frozen on (position size, stop, hold window, outcome
    horizons, timing/latency bounds, cost model) must be byte-identical,
    not independently retuned for Binance."""
    identity_fields = {"paper_version", "watch_version", "watch_contract_sha256", "source_exchange"}
    for field in fields(FROZEN_PAPER_CONTRACT):
        if field.name in identity_fields:
            continue
        bybit_value = getattr(FROZEN_PAPER_CONTRACT, field.name)
        binance_value = getattr(BINANCE_PAPER_CONTRACT, field.name)
        assert binance_value == bybit_value, f"{field.name} drifted from the frozen v1 contract"
    assert BINANCE_PAPER_CONTRACT.paper_version != FROZEN_PAPER_CONTRACT.paper_version
    assert BINANCE_PAPER_CONTRACT.watch_version != FROZEN_PAPER_CONTRACT.watch_version
    assert BINANCE_PAPER_CONTRACT.source_exchange != FROZEN_PAPER_CONTRACT.source_exchange
    assert BINANCE_PAPER_CONTRACT.sha256_hex() == BINANCE_PAPER_CONTRACT_SHA256
    assert BINANCE_PAPER_CONTRACT.sha256_hex() != PAPER_CONTRACT_SHA256


async def test_run_paper_worker_rejects_a_mismatched_contract_and_hash_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the same class of finding run_watch_worker's own
    identical check guards against: register_run's own equality check
    only compares contract_sha256 against the DB row it just wrote from
    that same value, tautological on the very first run -- it can never
    catch a caller passing a contract_sha256 that does not actually match
    contract.sha256_hex(). run_paper_worker must catch this itself,
    before ever reaching the store."""
    _install_fake_redis(monkeypatch)
    store = RecordingStore()
    market = FakeMarket([])
    config = PaperWorkerConfig("postgresql://example", "redis:6379")

    with pytest.raises(RuntimeError, match="contract_sha256 does not match"):
        await run_paper_worker(
            config,
            once=True,
            store=store,
            market=market,
            contract=BINANCE_PAPER_CONTRACT,
            contract_sha256=PAPER_CONTRACT_SHA256,  # the Bybit hash, wrong for this contract
        )
    assert store.lock_calls == []
