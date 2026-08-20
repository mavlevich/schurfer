from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from typing import Any, cast
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
    _upstream_watch_block,
    health_key,
    process_tick,
    run_paper_worker,
)
from schurfer_analytics.momentum_flow_producer_readiness import (
    BLOCKED_STATUS,
    DEPENDENCY_UNAVAILABLE_STATUS,
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
        self.bulk_stale: list[WatchCandidate] = []
        self.rejected: list[UUID] = []
        self.probes: tuple[PaperProbe, ...] = ()
        self.applied: list[UUID] = []

    async def acquire_worker_lock(self, paper_version: str) -> bool:
        return True

    async def register_run(self, **_: Any) -> PaperRun:
        return PaperRun(FROZEN_PAPER_CONTRACT.paper_version, "hash", {}, T0, "active")

    async def abandon_interrupted_entries(self, **_: Any) -> int:
        return 0

    async def due_fresh_watches(self, **_: Any) -> tuple[WatchCandidate, ...]:
        return self.candidates

    async def due_expired_watches(self, **_: Any) -> tuple[WatchCandidate, ...]:
        return ()

    async def claim_watch(self, candidate: WatchCandidate, **_: Any) -> UUID:
        paper_id = uuid4()
        self.claimed.append(paper_id)
        return paper_id

    async def reject_stale_entry(self, paper_id: UUID, **_: Any) -> None:
        self.stale.append(paper_id)

    async def bulk_reject_stale_watches(
        self, candidates: tuple[WatchCandidate, ...], **_: Any
    ) -> int:
        self.bulk_stale.extend(candidates)
        return len(candidates)

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

    async def health(self, *, contract: PaperContract, cohort_started_at: datetime) -> PaperHealth:
        return PaperHealth(
            0, 0, 0, 0, 0, 0, 0, 0, 0, None, None, 0, 0, None, None, None, 0, 0, 0, 0
        )


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


async def test_process_entry_accepts_exact_deadline_boundary() -> None:
    store = FakeStore()

    result = await _process_entry(
        _candidate(),
        store=store,
        market=FakeMarket([_quote()]),
        contract=FROZEN_PAPER_CONTRACT,
        clock=lambda: T0 + timedelta(seconds=30),
    )

    assert result == "opened"
    assert store.opened == store.claimed


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


async def test_process_tick_with_entries_disallowed_still_services_existing_probes() -> None:
    """The P1 fix a colleague review's own finding on the first version of
    this readiness gate demanded: blocking new entries must never also
    stop stop/max-hold/horizon-outcome bookkeeping for an ALREADY-open
    position. A candidate is present but must not be claimed; a probe is
    present and must still be quoted/processed exactly as if entries were
    allowed."""
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
        market=FakeMarket([_quote(side="bid")]),
        run=run,
        config=config,
        clock=lambda: T0 + timedelta(seconds=1),
        allow_new_entries=False,
    )

    assert result.watches_seen == 0
    assert result.entries_opened == 0
    assert store.claimed == []
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

    def __init__(
        self,
        *,
        upstream_watch_status: str | None = "ok",
        upstream_watch_generated_at: str | None = None,
    ) -> None:
        self.hset_calls: list[tuple[str, dict[str, str]]] = []
        # Defaults to "ok" / a fresh generated_at (computed lazily at read
        # time, see hmget below): every existing test in this file
        # exercises the happy path, where the upstream WATCH worker this
        # paper worker depends on is healthy and recently reported so.
        # Tests for the blocked-upstream gate (see _upstream_watch_block)
        # construct their own _FakeRedis with a different status/
        # generated_at, or None for "key never written".
        self.upstream_watch_status = upstream_watch_status
        self.upstream_watch_generated_at = upstream_watch_generated_at
        # When set, hmget pops one (status, generated_at) pair per call
        # (holding the last entry once exhausted) -- lets a test simulate
        # "upstream was ok on the first tick, degraded on the next".
        self.upstream_watch_status_sequence: list[tuple[str | None, str | None]] | None = None

    async def hset(self, key: str, mapping: dict[str, str]) -> None:
        self.hset_calls.append((key, dict(mapping)))

    async def hmget(self, key: str, keys: list[str]) -> list[str | None]:
        assert keys == ["status", "generated_at"]
        if self.upstream_watch_status_sequence:
            if len(self.upstream_watch_status_sequence) > 1:
                status, generated_at = self.upstream_watch_status_sequence.pop(0)
            else:
                status, generated_at = self.upstream_watch_status_sequence[0]
            return [status, generated_at]
        generated_at = self.upstream_watch_generated_at
        if generated_at is None and self.upstream_watch_status is not None:
            generated_at = datetime.now(UTC).isoformat()
        return [self.upstream_watch_status, generated_at]

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
    hardcoded default anywhere along the way. due_fresh_watches/monitored_probes
    always return empty so _process_entry/_process_probe are never
    reached -- this class tests contract plumbing, not entry/exit
    business logic (see FakeStore's own tests above for that)."""

    def __init__(self) -> None:
        self.lock_calls: list[str] = []
        self.register_run_calls: list[PaperContract] = []
        self.abandon_calls: list[PaperContract] = []
        self.due_fresh_watches_calls: list[PaperContract] = []
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

    async def due_fresh_watches(
        self, *, contract: PaperContract, cohort_started_at: datetime, now: datetime, limit: int
    ) -> tuple[WatchCandidate, ...]:
        self.due_fresh_watches_calls.append(contract)
        return ()

    async def due_expired_watches(
        self, *, contract: PaperContract, cohort_started_at: datetime, now: datetime, limit: int
    ) -> tuple[WatchCandidate, ...]:
        return ()

    async def claim_watch(self, candidate: WatchCandidate, **_: Any) -> UUID | None:
        raise AssertionError("no candidate should ever be claimed: due_watches returns none")

    async def bulk_reject_stale_watches(
        self, candidates: tuple[WatchCandidate, ...], **_: Any
    ) -> int:
        raise AssertionError("no stale candidate should be returned")

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

    async def health(self, *, contract: PaperContract, cohort_started_at: datetime) -> PaperHealth:
        self.health_calls.append(contract)
        return PaperHealth(
            0, 0, 0, 0, 0, 0, 0, 0, 0, None, None, 0, 0, None, None, None, 0, 0, 0, 0
        )


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
    assert store.due_fresh_watches_calls == [FROZEN_PAPER_CONTRACT]
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
    assert store.due_fresh_watches_calls == [BINANCE_PAPER_CONTRACT]
    assert store.monitored_probes_calls == [BINANCE_PAPER_CONTRACT]
    key, mapping = fake_redis.hset_calls[-1]
    assert key == health_key(BINANCE_PAPER_CONTRACT.paper_version)
    assert key != health_key(FROZEN_PAPER_CONTRACT.paper_version)
    assert mapping["paper_version"] == BINANCE_PAPER_CONTRACT.paper_version


async def test_run_paper_worker_reports_blocked_instead_of_crashing_when_upstream_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The paper-side half of the same regression run_watch_worker's own
    test guards: momentum_paper_binance reported "ok, total=0" for 32+
    hours while its own upstream WATCH worker could never produce a
    decision for it to claim. Must report BLOCKED_STATUS for that tick --
    and, per a colleague review's own finding on the first version of this
    fix, must NOT crash/raise to do it (a paper worker that refuses to
    even start also stops servicing its own already-open positions'
    stops/exits, a materially worse failure than the one being fixed;
    see process_tick's own allow_new_entries tests above for that half).
    due_fresh_watches must not even be called for that tick -- there is nothing
    real to claim."""
    fake_redis = _install_fake_redis(monkeypatch)
    fake_redis.upstream_watch_status = BLOCKED_STATUS
    store = RecordingStore()
    market = FakeMarket([])
    config = PaperWorkerConfig("postgresql://example", "redis:6379")

    await run_paper_worker(config, once=True, store=store, market=market)

    key, mapping = fake_redis.hset_calls[-1]
    assert key == health_key(FROZEN_PAPER_CONTRACT.paper_version)
    assert mapping["status"] == BLOCKED_STATUS
    assert mapping["last_error"]
    assert store.due_fresh_watches_calls == []
    # Existing positions are still serviced even while blocked.
    assert store.expire_deadlines_calls == [FROZEN_PAPER_CONTRACT]
    assert store.monitored_probes_calls == [FROZEN_PAPER_CONTRACT]


async def test_run_paper_worker_reports_blocked_when_upstream_watch_health_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing upstream health key (that WATCH worker has never run at
    all) must not be silently treated as ready by omission."""
    fake_redis = _install_fake_redis(monkeypatch)
    fake_redis.upstream_watch_status = None
    store = RecordingStore()
    market = FakeMarket([])
    config = PaperWorkerConfig("postgresql://example", "redis:6379")

    await run_paper_worker(config, once=True, store=store, market=market)

    _, mapping = fake_redis.hset_calls[-1]
    assert mapping["status"] == BLOCKED_STATUS


async def test_run_paper_worker_treats_a_stale_ok_status_as_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A colleague review's own finding on the first version of this fix:
    if WATCH is hard-killed (OOM, host reboot -- anything that skips its
    own graceful health-write paths entirely), its last-written status=ok
    sits in Redis forever. Reading status alone, with no freshness check,
    would let paper believe a WATCH process that has not ticked in hours
    (or ever again) is still healthy."""
    fake_redis = _install_fake_redis(monkeypatch)
    fake_redis.upstream_watch_status = "ok"
    fake_redis.upstream_watch_generated_at = (
        datetime.now(tz=UTC) - timedelta(minutes=10)
    ).isoformat()
    store = RecordingStore()
    market = FakeMarket([])
    config = PaperWorkerConfig("postgresql://example", "redis:6379")

    await run_paper_worker(config, once=True, store=store, market=market)

    assert store.due_fresh_watches_calls == []
    _, mapping = fake_redis.hset_calls[-1]
    assert mapping["status"] == BLOCKED_STATUS


async def test_run_paper_worker_allows_entries_when_upstream_ok_is_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = _install_fake_redis(monkeypatch)
    fake_redis.upstream_watch_status = "ok"
    fake_redis.upstream_watch_generated_at = datetime.now(tz=UTC).isoformat()
    store = RecordingStore()
    market = FakeMarket([])
    config = PaperWorkerConfig("postgresql://example", "redis:6379")

    await run_paper_worker(config, once=True, store=store, market=market)

    assert store.due_fresh_watches_calls == [FROZEN_PAPER_CONTRACT]
    _, mapping = fake_redis.hset_calls[-1]
    assert mapping["status"] == "ok"


async def test_run_paper_worker_reports_blocked_when_upstream_watch_degrades_mid_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upstream WATCH was fresh "ok" on the first tick, but degrades
    before a later tick's own re-check -- must not report "ok" for that
    tick either. The gap this specific test closes: an earlier version of
    this fix only checked upstream readiness once, at startup, and then
    unconditionally wrote status="ok" on every following tick."""
    fake_redis = _install_fake_redis(monkeypatch)
    now = datetime.now(tz=UTC).isoformat()
    fake_redis.upstream_watch_status_sequence = [("ok", now), ("degraded", now)]
    store = RecordingStore()
    market = FakeMarket([])
    config = PaperWorkerConfig("postgresql://example", "redis:6379")

    await run_paper_worker(config, once=True, store=store, market=market)
    await run_paper_worker(config, once=True, store=store, market=market)

    _, mapping = fake_redis.hset_calls[-1]
    assert mapping["status"] == BLOCKED_STATUS
    assert mapping["last_error"]


async def test_run_paper_worker_recovers_from_blocked_to_ready_without_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct test of the "no crash loop" fix: the SAME running process,
    across consecutive ticks, must go from BLOCKED_STATUS back to "ok" the
    moment upstream recovers -- no restart, no re-registration."""
    fake_redis = _install_fake_redis(monkeypatch)
    now = datetime.now(tz=UTC).isoformat()
    fake_redis.upstream_watch_status_sequence = [(None, None), ("ok", now)]
    store = RecordingStore()
    market = FakeMarket([])
    config = PaperWorkerConfig("postgresql://example", "redis:6379")

    await run_paper_worker(config, once=True, store=store, market=market)
    first_status = fake_redis.hset_calls[-1][1]["status"]
    await run_paper_worker(config, once=True, store=store, market=market)
    second_status = fake_redis.hset_calls[-1][1]["status"]

    assert first_status == BLOCKED_STATUS
    assert second_status == "ok"
    assert store.lock_calls.count(FROZEN_PAPER_CONTRACT.paper_version) == 2


async def test_upstream_watch_block_reports_dependency_unavailable_on_a_redis_error() -> None:
    """A Redis error while checking must not propagate as a bare
    exception -- fail-closed, same reasoning as run_watch_worker's own
    readiness-check error handling. Reported distinctly from BLOCKED_
    STATUS: an infra blip is a different situation from a confirmed
    incompatible upstream."""

    class _RaisingRedis:
        async def hmget(self, key: str, keys: list[str]) -> list[str | None]:
            raise ConnectionError("redis unavailable")

    block = await _upstream_watch_block(
        cast("Any", _RaisingRedis()), watch_version="momentum_flow_watch_v1"
    )

    assert block is not None
    status, error = block
    assert status == DEPENDENCY_UNAVAILABLE_STATUS
    assert "could not read upstream WATCH health" in error


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


async def test_process_tick_prevents_starvation_and_prioritizes_fresh() -> None:
    class StarvationStore(FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.action_log: list[str] = []
            self.expired = tuple(
                _candidate(decision_at=T0 - timedelta(seconds=31 + offset)) for offset in range(100)
            )

        async def due_fresh_watches(self, **kwargs: Any) -> tuple[WatchCandidate, ...]:
            self.action_log.append("due_fresh_watches")
            return (_candidate(decision_at=T0),)

        async def due_expired_watches(self, **kwargs: Any) -> tuple[WatchCandidate, ...]:
            self.action_log.append("due_expired_watches")
            limit = cast("int", kwargs["limit"])
            assert limit == config.watch_batch_size
            return self.expired[:limit]

        async def monitored_probes(self, **kwargs: Any) -> tuple[PaperProbe, ...]:
            self.action_log.append("monitored_probes")
            return (
                PaperProbe(
                    uuid4(),
                    "ERAUSDT",
                    T0,
                    10,
                    "open",
                    0,
                    0,
                ),
            )

        async def claim_watch(self, candidate: WatchCandidate, **_: Any) -> UUID:
            self.action_log.append("claim_watch")
            return uuid4()

        async def expire_deadlines(self, **_: Any) -> tuple[int, int]:
            self.action_log.append("expire_deadlines")
            return 0, 0

        async def bulk_reject_stale_watches(
            self, candidates: tuple[WatchCandidate, ...], **_: Any
        ) -> int:
            self.action_log.append("bulk_reject_stale")
            return len(candidates)

    store = StarvationStore()
    market = FakeMarket([_quote(), _quote(side="bid")])
    run = PaperRun("version", "hash", {}, T0 - timedelta(hours=1), "active")
    config = PaperWorkerConfig("postgresql://fake", "redis://fake")

    result = await process_tick(
        store=store,
        market=market,
        run=run,
        config=config,
        clock=lambda: T0 + timedelta(seconds=1),
        allow_new_entries=True,
    )

    # Order must be: fresh -> claim -> expire -> monitor -> due_expired -> bulk_reject
    assert store.action_log == [
        "due_fresh_watches",
        "claim_watch",
        "expire_deadlines",
        "monitored_probes",
        "due_expired_watches",
        "bulk_reject_stale",
    ]
    assert result.watches_seen == 1 + config.watch_batch_size
    assert result.entries_stale_cleaned == config.watch_batch_size
