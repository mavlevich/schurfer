from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics import momentum_flow_watch_worker
from schurfer_analytics.momentum_flow_watch_contract import (
    BINANCE_WATCH_CONTRACT,
    BINANCE_WATCH_CONTRACT_SHA256,
    FROZEN_WATCH_CONTRACT,
    WATCH_CONTRACT_SHA256,
    WatchContract,
)
from schurfer_analytics.momentum_flow_watch_evaluator import SymbolWatchState, WatchBar
from schurfer_analytics.momentum_flow_watch_repository import (
    EvaluationWrite,
    WatchBucketInput,
    WatchRun,
)
from schurfer_analytics.momentum_flow_watch_worker import (
    WatchWorkerConfig,
    evaluate_bucket,
    health_key,
    run_watch_worker,
)


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


def test_health_key_scopes_by_watch_version() -> None:
    assert (
        health_key("momentum_flow_watch_v1") == "market:momentumwatch:health:momentum_flow_watch_v1"
    )
    # Regression: this is the actual "no shared/masking counters" case --
    # two contracts' own health snapshots must never collide on one key.
    assert health_key("momentum_flow_watch_v1_binance") != health_key("momentum_flow_watch_v1")


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
        def from_url(url: str, decode_responses: bool = True) -> _FakeRedis:
            return fake

    monkeypatch.setattr(momentum_flow_watch_worker, "Redis", _FakeRedisFactory)
    return fake


class RecordingStore:
    """A WatchStore that records exactly which contract each method was
    called with, for asserting run_watch_worker threads its own contract
    parameter through end to end instead of silently falling back to a
    hardcoded default anywhere along the way. due_buckets always returns
    empty so load_bucket/persist_bucket are never reached -- this file
    tests contract plumbing, not bucket evaluation (see evaluate_bucket's
    own tests above for that)."""

    def __init__(self) -> None:
        self.lock_calls: list[str] = []
        self.register_run_calls: list[WatchContract] = []
        self.due_buckets_calls: list[WatchContract] = []
        self.load_states_calls: list[WatchContract] = []

    async def acquire_worker_lock(self, watch_version: str) -> bool:
        self.lock_calls.append(watch_version)
        return True

    async def register_run(
        self, *, contract: WatchContract, contract_sha256: str, now: datetime
    ) -> WatchRun:
        self.register_run_calls.append(contract)
        return WatchRun(contract.watch_version, contract_sha256, {}, now, None, "active")

    async def due_buckets(
        self, *, contract: WatchContract, cohort_started_at: datetime, limit: int
    ) -> tuple[datetime, ...]:
        self.due_buckets_calls.append(contract)
        return ()

    async def load_bucket(
        self, *, contract: WatchContract, bucket_start: datetime
    ) -> WatchBucketInput | None:
        raise AssertionError("no bucket should be loaded: due_buckets returns none")

    async def load_states(self, *, contract: WatchContract) -> dict[str, SymbolWatchState]:
        self.load_states_calls.append(contract)
        return {}

    async def persist_bucket(
        self, writes: tuple[EvaluationWrite, ...], *, contract: WatchContract
    ) -> None:
        raise AssertionError("no bucket should ever be persisted in this test")


async def test_run_watch_worker_defaults_to_the_live_bybit_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: run_watch_worker gained contract/contract_sha256
    parameters after already being in production for the Bybit worker.
    Every existing caller (main(), and the live deploy) must see
    byte-identical behavior when it does not pass them explicitly."""
    fake_redis = _install_fake_redis(monkeypatch)
    store = RecordingStore()
    config = WatchWorkerConfig("postgresql://example", "redis:6379")

    await run_watch_worker(config, once=True, store=store)

    assert store.lock_calls == [FROZEN_WATCH_CONTRACT.watch_version]
    assert store.register_run_calls == [FROZEN_WATCH_CONTRACT]
    assert store.due_buckets_calls == [FROZEN_WATCH_CONTRACT]
    assert store.load_states_calls == [FROZEN_WATCH_CONTRACT]
    assert fake_redis.hset_calls
    key, mapping = fake_redis.hset_calls[-1]
    assert key == health_key(FROZEN_WATCH_CONTRACT.watch_version)
    assert mapping["watch_version"] == FROZEN_WATCH_CONTRACT.watch_version


async def test_run_watch_worker_threads_a_different_contract_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual regression this PR exists to prevent: passing
    BINANCE_WATCH_CONTRACT must reach every store call AND the health key,
    not silently fall back to the hardcoded Bybit default anywhere."""
    fake_redis = _install_fake_redis(monkeypatch)
    store = RecordingStore()
    config = WatchWorkerConfig("postgresql://example", "redis:6379")

    await run_watch_worker(
        config,
        once=True,
        store=store,
        contract=BINANCE_WATCH_CONTRACT,
        contract_sha256=BINANCE_WATCH_CONTRACT_SHA256,
    )

    assert store.lock_calls == [BINANCE_WATCH_CONTRACT.watch_version]
    assert store.register_run_calls == [BINANCE_WATCH_CONTRACT]
    assert store.due_buckets_calls == [BINANCE_WATCH_CONTRACT]
    assert store.load_states_calls == [BINANCE_WATCH_CONTRACT]
    key, mapping = fake_redis.hset_calls[-1]
    assert key == health_key(BINANCE_WATCH_CONTRACT.watch_version)
    assert key != health_key(FROZEN_WATCH_CONTRACT.watch_version)
    assert mapping["watch_version"] == BINANCE_WATCH_CONTRACT.watch_version


def test_binance_watch_contract_reuses_every_frozen_threshold_verbatim() -> None:
    """Regression for ROADMAP's own "frozen v1 logic" instruction: only
    identity fields (watch_version, source_exchange) may differ from the
    live Bybit contract -- every threshold this contract is frozen on must
    be byte-identical, not independently retuned for Binance's smaller
    universe."""
    identity_fields = {"watch_version", "source_exchange"}
    for field in fields(FROZEN_WATCH_CONTRACT):
        if field.name in identity_fields:
            continue
        bybit_value = getattr(FROZEN_WATCH_CONTRACT, field.name)
        binance_value = getattr(BINANCE_WATCH_CONTRACT, field.name)
        assert binance_value == bybit_value, f"{field.name} drifted from the frozen v1 contract"
    assert BINANCE_WATCH_CONTRACT.watch_version != FROZEN_WATCH_CONTRACT.watch_version
    assert BINANCE_WATCH_CONTRACT.source_exchange != FROZEN_WATCH_CONTRACT.source_exchange
    assert BINANCE_WATCH_CONTRACT.sha256_hex() == BINANCE_WATCH_CONTRACT_SHA256
    assert BINANCE_WATCH_CONTRACT.sha256_hex() != WATCH_CONTRACT_SHA256


async def test_run_watch_worker_rejects_a_mismatched_contract_and_hash_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for a code-review finding: register_run's own equality
    check only compares contract_sha256 against the DB row it just wrote
    from that same value, which is tautological on the very first run --
    it can never catch a caller passing a contract_sha256 that does not
    actually match contract.sha256_hex(). run_watch_worker must catch this
    itself, before ever reaching the store."""
    _install_fake_redis(monkeypatch)
    store = RecordingStore()
    config = WatchWorkerConfig("postgresql://example", "redis:6379")

    with pytest.raises(RuntimeError, match="contract_sha256 does not match"):
        await run_watch_worker(
            config,
            once=True,
            store=store,
            contract=BINANCE_WATCH_CONTRACT,
            contract_sha256=WATCH_CONTRACT_SHA256,  # the Bybit hash, wrong for this contract
        )
    assert store.lock_calls == []
