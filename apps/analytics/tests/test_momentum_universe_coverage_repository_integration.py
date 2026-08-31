"""Real-Postgres integration tests for MomentumUniverseIdentityRepository's
instruments_as_of/universe_seen_in_window (research/token-universe-
coverage-v1).

Same rationale as test_momentum_universe_identity_repository_integration.py:
a mock cannot catch a Table()/join column mismatch against the real
migration DDL. These two methods specifically need MULTIPLE snapshots per
exchange, spread over time -- the existing repository test file's own
_seed_snapshot only ever writes one (fixed universe_version/catalog_version,
ON CONFLICT DO NOTHING), so this file has its own seed helper rather than
importing that one.

Colleague review, round 3: this file's own first version copied
test_momentum_universe_identity_repository_integration.py's own
_connect_or_skip, which always skips on an unreachable Postgres -- but
that is NOT this package's actual established convention (see e.g.
test_pump_recurrence_integrity_repository_integration.py's own
_connect_or_skip): CI sets REQUIRE_INTEGRATION_DB=1 specifically so a
broken/unprovisioned Postgres service fails the build loudly instead of
these new SQL-correctness tests silently skipping and the run still going
green. Follows that established pattern here instead.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.momentum_universe_identity_repository import (
    MomentumUniverseIdentityRepository,
)
from schurfer_analytics.token_universe_coverage import delisted, mark_currently_ready
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"

_TEST_EXCHANGE = "test_coverage_repo"


async def _connect_or_skip() -> AsyncEngine:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but Postgres is unreachable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres reachable: {exc}")
    return engine


async def test_connect_or_skip_raises_when_require_integration_db_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CI-enforcement path itself: with REQUIRE_INTEGRATION_DB=1, an
    unreachable Postgres must fail the test, never silently skip it --
    same enforcement test as test_pump_recurrence_integrity_repository_
    integration.py's own, reproduced here since each integration-test file
    in this package owns its own _connect_or_skip rather than sharing one."""

    class _FailingConnection:
        async def __aenter__(self) -> _FailingConnection:
            raise OSError("connection refused")

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _FailingEngine:
        def connect(self) -> _FailingConnection:
            return _FailingConnection()

        async def dispose(self) -> None:
            return None

    monkeypatch.setenv("REQUIRE_INTEGRATION_DB", "1")
    monkeypatch.setattr(
        sys.modules[__name__],
        "create_async_engine",
        lambda *_args, **_kwargs: _FailingEngine(),
    )
    with pytest.raises(RuntimeError, match="REQUIRE_INTEGRATION_DB"):
        await _connect_or_skip()


def _identity_key(exchange: str, base: str, onboarded_at: datetime) -> str:
    return f"{exchange}:linear_usdt_perpetual:{base}USDT:{int(onboarded_at.timestamp() * 1000)}"


async def _seed_snapshot(
    engine: AsyncEngine,
    *,
    exchange: str,
    universe_version: str,
    captured_at: datetime,
    ready_bases: tuple[str, ...],
    onboarded_at_by_base: dict[str, datetime] | None = None,
) -> None:
    """Writes one full snapshot (own universe_version/catalog_version pair,
    so multiple calls for the same exchange never collide) with one
    'ready' instrument row per base in ready_bases. onboarded_at_by_base
    lets a test control a base's own onboarded_at (and therefore its own
    identity_key) explicitly -- needed to simulate a delist-then-relist
    under the same native_market_id with a genuinely different identity."""
    onboarded_at_by_base = onboarded_at_by_base or {}
    catalog_version = f"{universe_version}_catalog"
    async with engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO app.momentum_universe_snapshots
                    (exchange, universe_version, catalog_version, capture_version,
                     schema_version, captured_at, instrument_count, payload_hash)
                VALUES
                    (:exchange, :universe_version, :catalog_version, 'test_capture_v1',
                     'test_schema_v1', :captured_at, :instrument_count,
                     decode(repeat('ab', 32), 'hex'))
            """),
            {
                "exchange": exchange,
                "universe_version": universe_version,
                "catalog_version": catalog_version,
                "captured_at": captured_at,
                "instrument_count": len(ready_bases),
            },
        )
        for base in ready_bases:
            onboarded_at = onboarded_at_by_base.get(base, captured_at - timedelta(days=200))
            await connection.execute(
                text("""
                    INSERT INTO app.momentum_universe_instruments
                        (exchange, universe_version, catalog_version, native_market_id,
                         base, quote, settle, native_market_type, canonical_market_type,
                         onboarded_at, identity_status, identity_key, metadata_hash)
                    VALUES
                        (:exchange, :universe_version, :catalog_version, :native_market_id,
                         :base, 'USDT', 'USDT', 'LinearPerpetual', 'linear_usdt_perpetual',
                         :onboarded_at, 'ready', :identity_key,
                         decode(repeat('cd', 32), 'hex'))
                """),
                {
                    "exchange": exchange,
                    "universe_version": universe_version,
                    "catalog_version": catalog_version,
                    "native_market_id": f"{base}USDT",
                    "base": base,
                    "onboarded_at": onboarded_at,
                    "identity_key": _identity_key(exchange, base, onboarded_at),
                },
            )


async def _cleanup(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM app.momentum_universe_instruments WHERE exchange = :ex"),
            {"ex": _TEST_EXCHANGE},
        )
        await connection.execute(
            text("DELETE FROM app.momentum_universe_snapshots WHERE exchange = :ex"),
            {"ex": _TEST_EXCHANGE},
        )


async def test_instruments_as_of_uses_nearest_snapshot_at_or_before() -> None:
    engine = await _connect_or_skip()
    try:
        t1 = datetime(2026, 8, 1, tzinfo=UTC)
        t2 = datetime(2026, 8, 15, tzinfo=UTC)
        await _seed_snapshot(
            engine,
            exchange=_TEST_EXCHANGE,
            universe_version="v1",
            captured_at=t1,
            ready_bases=("EARLYCOIN",),
        )
        await _seed_snapshot(
            engine,
            exchange=_TEST_EXCHANGE,
            universe_version="v2",
            captured_at=t2,
            ready_bases=("LATECOIN",),
        )

        repository = MomentumUniverseIdentityRepository(engine)

        # Asking about an instant between the two snapshots must return the
        # EARLIER one (t1), never peek at t2's own future listing state.
        mid = await repository.instruments_as_of(_TEST_EXCHANGE, datetime(2026, 8, 8, tzinfo=UTC))
        assert mid.snapshot_captured_at == t1
        assert mid.native_market_ids == frozenset({"EARLYCOINUSDT"})
        assert mid.identity_keys == frozenset(
            {_identity_key(_TEST_EXCHANGE, "EARLYCOIN", t1 - timedelta(days=200))}
        )

        # Asking about an instant before ANY snapshot exists must say so
        # honestly, not silently fall back to the earliest one.
        before_any = await repository.instruments_as_of(
            _TEST_EXCHANGE, datetime(2026, 7, 1, tzinfo=UTC)
        )
        assert before_any.snapshot_captured_at is None
        assert before_any.native_market_ids == frozenset()
        assert before_any.identity_keys == frozenset()
        assert before_any.is_usable(max_staleness=timedelta(days=3650)) is False

        # Asking as of (or after) the later snapshot returns it.
        after_both = await repository.instruments_as_of(_TEST_EXCHANGE, t2)
        assert after_both.snapshot_captured_at == t2
        assert after_both.native_market_ids == frozenset({"LATECOINUSDT"})
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_universe_seen_in_window_carries_forward_state_before_window() -> None:
    """Colleague review, round 2: the P1 this test exists to catch. A
    window containing ZERO capture-process restarts must not report an
    empty universe -- the last snapshot before window_start still
    describes what was ready for the whole window. Without the carry-in
    fix, this test would see an empty `seen` for SURVIVOR even though it
    was ready the entire time."""
    engine = await _connect_or_skip()
    try:
        snapshot_at = datetime(2026, 7, 20, tzinfo=UTC)
        window_start = datetime(2026, 8, 1, tzinfo=UTC)
        window_end = datetime(2026, 8, 10, tzinfo=UTC)
        # No snapshot at all inside [window_start, window_end) -- only this
        # one, 12 days before window_start.
        await _seed_snapshot(
            engine,
            exchange=_TEST_EXCHANGE,
            universe_version="carry0",
            captured_at=snapshot_at,
            ready_bases=("SURVIVOR",),
        )

        repository = MomentumUniverseIdentityRepository(engine)

        # Generous tolerance: carry-in is within it, coverage is reliable,
        # and SURVIVOR must appear despite no in-window snapshot.
        generous = await repository.universe_seen_in_window(
            _TEST_EXCHANGE, window_start, window_end, max_carry_in_staleness=timedelta(days=30)
        )
        assert generous.carry_in_snapshot_captured_at == snapshot_at
        assert generous.carry_in_within_tolerance is True
        assert generous.has_reliable_coverage is True
        assert [entry.native_market_id for entry in generous.seen] == ["SURVIVORUSDT"]

        # Tight tolerance: the SAME carry-in snapshot is now too stale to
        # trust (12 days old vs. a 1-day tolerance) -- it is excluded from
        # `seen` entirely (not silently mixed in as if still
        # representative), and the window is flagged unreliable.
        # carry_in_snapshot_captured_at is still reported for diagnosis.
        tight = await repository.universe_seen_in_window(
            _TEST_EXCHANGE, window_start, window_end, max_carry_in_staleness=timedelta(days=1)
        )
        assert tight.carry_in_snapshot_captured_at == snapshot_at
        assert tight.carry_in_within_tolerance is False
        assert tight.has_reliable_coverage is False
        assert tight.seen == ()

        # No snapshot at all before window_start: seen is empty AND flagged
        # unreliable, never silently "no coverage = nothing was listed".
        no_history = await repository.universe_seen_in_window(
            _TEST_EXCHANGE,
            window_start - timedelta(days=365),
            window_start - timedelta(days=300),
            max_carry_in_staleness=timedelta(days=30),
        )
        assert no_history.carry_in_snapshot_captured_at is None
        assert no_history.has_reliable_coverage is False
        assert no_history.seen == ()
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_universe_seen_in_window_distinguishes_relisted_identity() -> None:
    """Colleague review, round 2: the other P1. A native_market_id
    delisted and later relisted under the SAME ticker must produce TWO
    separate SeenInstrument entries (different identity_key, different
    onboarded_at) -- never merged into one continuous "life", and the old
    life must never be marked currently_ready just because the new one
    reused its native_market_id."""
    engine = await _connect_or_skip()
    try:
        old_onboarded_at = datetime(2026, 1, 1, tzinfo=UTC)
        t_old = datetime(2026, 8, 1, tzinfo=UTC)
        new_onboarded_at = datetime(2026, 8, 18, tzinfo=UTC)
        t_new = datetime(2026, 8, 20, tzinfo=UTC)
        window_start = datetime(2026, 7, 25, tzinfo=UTC)
        window_end = datetime(2026, 8, 25, tzinfo=UTC)

        await _seed_snapshot(
            engine,
            exchange=_TEST_EXCHANGE,
            universe_version="relist_old",
            captured_at=t_old,
            ready_bases=("ABC",),
            onboarded_at_by_base={"ABC": old_onboarded_at},
        )
        # ABC delisted in between (no snapshot needed to represent that --
        # its absence from the NEXT snapshot below is what proves it).
        await _seed_snapshot(
            engine,
            exchange=_TEST_EXCHANGE,
            universe_version="relist_new",
            captured_at=t_new,
            ready_bases=("ABC",),
            onboarded_at_by_base={"ABC": new_onboarded_at},
        )

        repository = MomentumUniverseIdentityRepository(engine)
        window = await repository.universe_seen_in_window(
            _TEST_EXCHANGE, window_start, window_end, max_carry_in_staleness=timedelta(days=30)
        )
        # Two distinct lives, not one merged entry.
        assert len(window.seen) == 2
        keys = {entry.identity_key for entry in window.seen}
        old_key = _identity_key(_TEST_EXCHANGE, "ABC", old_onboarded_at)
        new_key = _identity_key(_TEST_EXCHANGE, "ABC", new_onboarded_at)
        assert keys == {old_key, new_key}

        current = await repository.instruments_as_of(_TEST_EXCHANGE, t_new)
        assert current.identity_keys == {new_key}

        marked = mark_currently_ready(window.seen, current.identity_keys)
        by_key = {entry.identity_key: entry for entry in marked}
        assert by_key[new_key].currently_ready is True
        assert by_key[old_key].currently_ready is False

        gone = delisted(marked)
        assert [entry.identity_key for entry in gone] == [old_key]
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_universe_seen_in_window_excludes_stale_out_of_window_snapshot() -> None:
    engine = await _connect_or_skip()
    try:
        window_start = datetime(2026, 8, 1, tzinfo=UTC)
        t1 = datetime(2026, 8, 5, tzinfo=UTC)
        t2 = datetime(2026, 8, 20, tzinfo=UTC)
        window_end = datetime(2026, 9, 1, tzinfo=UTC)
        # OUTSIDEWINDOW is seeded far enough before window_start that it
        # falls outside any reasonable carry-in tolerance used below, and
        # is superseded by the in-window snapshots anyway (this exchange
        # never lists OUTSIDEWINDOW again).
        # SURVIVOR must carry the SAME onboarded_at (and therefore the same
        # identity_key) across both snapshots -- it is meant to represent
        # one continuous life, not a relisting -- so it is passed explicitly
        # rather than relying on _seed_snapshot's own default (captured_at
        # - 200 days), which would otherwise drift per snapshot and split
        # SURVIVOR into two distinct identity_keys purely as a seeding
        # artifact.
        survivor_onboarded_at = t1 - timedelta(days=200)
        await _seed_snapshot(
            engine,
            exchange=_TEST_EXCHANGE,
            universe_version="w0",
            captured_at=window_start - timedelta(days=400),
            ready_bases=("OUTSIDEWINDOW",),
        )
        await _seed_snapshot(
            engine,
            exchange=_TEST_EXCHANGE,
            universe_version="w1",
            captured_at=t1,
            ready_bases=("SURVIVOR", "GONECOIN"),
            onboarded_at_by_base={"SURVIVOR": survivor_onboarded_at},
        )
        await _seed_snapshot(
            engine,
            exchange=_TEST_EXCHANGE,
            universe_version="w2",
            captured_at=t2,
            ready_bases=("SURVIVOR",),
            onboarded_at_by_base={"SURVIVOR": survivor_onboarded_at},
        )

        repository = MomentumUniverseIdentityRepository(engine)
        window = await repository.universe_seen_in_window(
            _TEST_EXCHANGE, window_start, window_end, max_carry_in_staleness=timedelta(days=10)
        )
        seen_ids = {entry.native_market_id for entry in window.seen}
        assert seen_ids == {"SURVIVORUSDT", "GONECOINUSDT"}
        assert "OUTSIDEWINDOWUSDT" not in seen_ids
        # w0 (400 days before window_start) is the nearest carry-in
        # candidate but far outside the 10-day tolerance used here.
        assert window.carry_in_within_tolerance is False

        # Every entry starts currently_ready=None from the window read
        # itself -- the "current" answer must come from a separate,
        # unwindowed call, never leak in from this query.
        assert all(entry.currently_ready is None for entry in window.seen)

        current = await repository.instruments_as_of(_TEST_EXCHANGE, t2)
        marked = mark_currently_ready(window.seen, current.identity_keys)
        by_id = {entry.native_market_id: entry for entry in marked}
        assert by_id["SURVIVORUSDT"].currently_ready is True
        assert by_id["GONECOINUSDT"].currently_ready is False

        gone = delisted(marked)
        assert [entry.native_market_id for entry in gone] == ["GONECOINUSDT"]
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_universe_seen_in_window_rejects_reversed_or_empty_window() -> None:
    """Colleague review, round 3: window_end is never consulted when
    deciding the carry-in, so an empty or reversed interval could
    otherwise still return a non-empty `seen` purely from a valid
    carry-in -- a denominator for an interval that does not exist. Fails
    fast instead, before ever touching the database."""
    engine = await _connect_or_skip()
    try:
        repository = MomentumUniverseIdentityRepository(engine)
        instant = datetime(2026, 8, 1, tzinfo=UTC)
        with pytest.raises(ValueError, match="window_start"):
            await repository.universe_seen_in_window(
                _TEST_EXCHANGE, instant, instant, max_carry_in_staleness=timedelta(days=1)
            )
        with pytest.raises(ValueError, match="window_start"):
            await repository.universe_seen_in_window(
                _TEST_EXCHANGE,
                instant,
                instant - timedelta(days=1),
                max_carry_in_staleness=timedelta(days=1),
            )
    finally:
        await engine.dispose()


async def test_universe_seen_in_window_rejects_negative_max_carry_in_staleness() -> None:
    engine = await _connect_or_skip()
    try:
        repository = MomentumUniverseIdentityRepository(engine)
        window_start = datetime(2026, 8, 1, tzinfo=UTC)
        window_end = datetime(2026, 8, 10, tzinfo=UTC)
        with pytest.raises(ValueError, match="max_carry_in_staleness"):
            await repository.universe_seen_in_window(
                _TEST_EXCHANGE, window_start, window_end, max_carry_in_staleness=timedelta(days=-1)
            )
    finally:
        await engine.dispose()


async def test_universe_seen_in_window_rejects_naive_timestamps() -> None:
    engine = await _connect_or_skip()
    try:
        repository = MomentumUniverseIdentityRepository(engine)
        naive_start = datetime(2026, 8, 1)  # deliberately naive, testing the guard
        aware_end = datetime(2026, 8, 10, tzinfo=UTC)
        with pytest.raises(ValueError, match="timezone-aware"):
            await repository.universe_seen_in_window(
                _TEST_EXCHANGE, naive_start, aware_end, max_carry_in_staleness=timedelta(days=1)
            )
    finally:
        await engine.dispose()


async def test_instruments_as_of_rejects_naive_timestamp() -> None:
    engine = await _connect_or_skip()
    try:
        repository = MomentumUniverseIdentityRepository(engine)
        naive = datetime(2026, 8, 1)  # deliberately naive, testing the guard
        with pytest.raises(ValueError, match="timezone-aware"):
            await repository.instruments_as_of(_TEST_EXCHANGE, naive)
    finally:
        await engine.dispose()
