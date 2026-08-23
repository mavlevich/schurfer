"""Real-Postgres coverage proving updated_at actually advances on every
early_momentum_episodes mutation.

TimestampMixin's `onupdate=utcnow` (packages/journal/schurfer_journal/models/
base.py) only fires when SQLAlchemy itself executes the UPDATE -- every
mutation here goes through raw psycopg, so before this PR updated_at was
frozen at INSERT time forever, silently useless for investigating "when did
this episode last change" (see episodes.py's mutation SQL, each of which now
sets `updated_at = now()` explicitly).

No sleep() anywhere: each test backdates updated_at to a fixed point in the
past before the transition under test, then asserts the column moved forward
past that point -- a wall-clock comparison, not a timing race.

Mirrors test_episodes_integration.py's `_connect_or_skip`/REQUIRE_INTEGRATION_DB
convention and helpers.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from schurfer_execution import episodes, journal

TEST_DATABASE_URL = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"
_TEST_EXCHANGE = "test_early_momentum_v3"
_LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)


async def _connect_or_skip() -> psycopg.AsyncConnection:
    try:
        conn = await psycopg.AsyncConnection.connect(TEST_DATABASE_URL, autocommit=True)
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1")
    except Exception as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but Postgres is unreachable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres reachable: {exc}")
    return conn


async def _ensure_strategy(conn: psycopg.AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO app.strategies (name, version, description) VALUES (%s,%s,%s) "
            "ON CONFLICT (name, version) DO UPDATE SET updated_at = now() RETURNING id",
            ("early_momentum", "3_test", "integration test"),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _cleanup(conn: psycopg.AsyncConnection, *, native_market_id: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM app.trades WHERE episode_id IN "
            "(SELECT episode_id FROM app.early_momentum_episodes "
            " WHERE exchange = %s AND native_market_id = %s)",
            (_TEST_EXCHANGE, native_market_id),
        )
        await cur.execute(
            "DELETE FROM app.early_momentum_episodes WHERE exchange = %s AND native_market_id = %s",
            (_TEST_EXCHANGE, native_market_id),
        )


async def _arm(
    conn: psycopg.AsyncConnection, *, strategy_id: int, native_market_id: str, ttl_seconds: int = 60
) -> episodes.Episode:
    ep = await episodes.create_episode(
        TEST_DATABASE_URL,
        strategy_id=strategy_id,
        contract_sha256=hashlib.sha256(native_market_id.encode()).digest(),
        source_exchange=_TEST_EXCHANGE,
        source_native_id=native_market_id,
        exchange=_TEST_EXCHANGE,
        native_market_id=native_market_id,
        execution_symbol=None,
        execution_identity_key="ik",
        source_identity_key="ik",
        cluster_key=native_market_id,
        ceiling=1.0,
        features={},
        ttl_seconds=ttl_seconds,
    )
    assert ep is not None
    return ep


async def _updated_at(conn: psycopg.AsyncConnection, *, episode_id: str) -> datetime:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT updated_at FROM app.early_momentum_episodes WHERE episode_id = %s",
            (episode_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _backdate(conn: psycopg.AsyncConnection, *, episode_id: str, when: datetime) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE app.early_momentum_episodes SET updated_at = %s WHERE episode_id = %s",
            (when, episode_id),
        )


async def test_claim_advances_updated_at() -> None:
    conn = await _connect_or_skip()
    native_market_id = f"TSCLAIM{uuid.uuid4().hex[:8]}"
    try:
        strategy_id = await _ensure_strategy(conn)
        ep = await _arm(conn, strategy_id=strategy_id, native_market_id=native_market_id)
        await _backdate(conn, episode_id=ep.episode_id, when=_LONG_AGO)

        outcome = await episodes.claim_episode(TEST_DATABASE_URL, episode_id=ep.episode_id)
        assert outcome.claimed is True

        assert await _updated_at(conn, episode_id=ep.episode_id) > _LONG_AGO
    finally:
        await _cleanup(conn, native_market_id=native_market_id)
        await conn.close()


async def test_terminate_claimed_advances_updated_at() -> None:
    conn = await _connect_or_skip()
    native_market_id = f"TSTERMCLM{uuid.uuid4().hex[:8]}"
    try:
        strategy_id = await _ensure_strategy(conn)
        ep = await _arm(conn, strategy_id=strategy_id, native_market_id=native_market_id)
        claim = await episodes.claim_episode(TEST_DATABASE_URL, episode_id=ep.episode_id)
        assert claim.claimed is True
        await _backdate(conn, episode_id=ep.episode_id, when=_LONG_AGO)

        terminated = await episodes.terminate_episode(
            TEST_DATABASE_URL,
            episode_id=ep.episode_id,
            reason=episodes.REASON_QUOTE_TIMEOUT,
            claim_token=claim.claim_token,
        )
        assert terminated is True

        assert await _updated_at(conn, episode_id=ep.episode_id) > _LONG_AGO
    finally:
        await _cleanup(conn, native_market_id=native_market_id)
        await conn.close()


async def test_terminate_armed_advances_updated_at() -> None:
    conn = await _connect_or_skip()
    native_market_id = f"TSTERMARM{uuid.uuid4().hex[:8]}"
    try:
        strategy_id = await _ensure_strategy(conn)
        ep = await _arm(conn, strategy_id=strategy_id, native_market_id=native_market_id)
        await _backdate(conn, episode_id=ep.episode_id, when=_LONG_AGO)

        terminated = await episodes.terminate_episode(
            TEST_DATABASE_URL,
            episode_id=ep.episode_id,
            reason=episodes.REASON_ROUTE_INVALIDATED,
        )
        assert terminated is True

        assert await _updated_at(conn, episode_id=ep.episode_id) > _LONG_AGO
    finally:
        await _cleanup(conn, native_market_id=native_market_id)
        await conn.close()


async def test_reap_expired_armed_advances_updated_at() -> None:
    conn = await _connect_or_skip()
    native_market_id = f"TSREAPARM{uuid.uuid4().hex[:8]}"
    try:
        strategy_id = await _ensure_strategy(conn)
        ep = await _arm(conn, strategy_id=strategy_id, native_market_id=native_market_id)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE app.early_momentum_episodes "
                "SET expires_at = now() - interval '1 second' WHERE episode_id = %s",
                (ep.episode_id,),
            )
        await _backdate(conn, episode_id=ep.episode_id, when=_LONG_AGO)

        summary = await episodes.reap_overdue(TEST_DATABASE_URL)
        assert summary is not None
        assert summary.expired_armed >= 1

        assert await _updated_at(conn, episode_id=ep.episode_id) > _LONG_AGO

        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status FROM app.early_momentum_episodes WHERE episode_id = %s",
                (ep.episode_id,),
            )
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == "expired"

        # A second reaper pass must be idempotent: nothing left to reap for
        # this row, and it must not touch updated_at again.
        second_pass_updated_at = await _updated_at(conn, episode_id=ep.episode_id)
        await episodes.reap_overdue(TEST_DATABASE_URL)
        assert await _updated_at(conn, episode_id=ep.episode_id) == second_pass_updated_at
    finally:
        await _cleanup(conn, native_market_id=native_market_id)
        await conn.close()


async def test_mark_opened_advances_updated_at() -> None:
    conn = await _connect_or_skip()
    native_market_id = f"TSOPEN{uuid.uuid4().hex[:8]}"
    try:
        strategy_id = await _ensure_strategy(conn)
        ep = await _arm(conn, strategy_id=strategy_id, native_market_id=native_market_id)
        claim = await episodes.claim_episode(TEST_DATABASE_URL, episode_id=ep.episode_id)
        assert claim.claimed is True
        assert claim.claim_token is not None
        await _backdate(conn, episode_id=ep.episode_id, when=_LONG_AGO)

        outcome = await journal.open_trade_for_episode(
            TEST_DATABASE_URL,
            episode_id=ep.episode_id,
            claim_token=claim.claim_token,
            symbol=f"{native_market_id}/USDT:USDT",
            exchange=_TEST_EXCHANGE,
            side="long",
            size_usd=100.0,
            leverage=5,
            entry_price=1.0,
            entry_idempotency_key=f"{ep.episode_id}:entry:base",
            setup_context={"strategy": "early_momentum_v3", "paper": True},
        )
        assert outcome.trade_id is not None

        assert await _updated_at(conn, episode_id=ep.episode_id) > _LONG_AGO
    finally:
        await _cleanup(conn, native_market_id=native_market_id)
        await conn.close()


async def test_mark_closed_advances_updated_at() -> None:
    conn = await _connect_or_skip()
    native_market_id = f"TSCLOSE{uuid.uuid4().hex[:8]}"
    try:
        strategy_id = await _ensure_strategy(conn)
        ep = await _arm(conn, strategy_id=strategy_id, native_market_id=native_market_id)
        claim = await episodes.claim_episode(TEST_DATABASE_URL, episode_id=ep.episode_id)
        assert claim.claimed is True
        assert claim.claim_token is not None
        outcome = await journal.open_trade_for_episode(
            TEST_DATABASE_URL,
            episode_id=ep.episode_id,
            claim_token=claim.claim_token,
            symbol=f"{native_market_id}/USDT:USDT",
            exchange=_TEST_EXCHANGE,
            side="long",
            size_usd=100.0,
            leverage=5,
            entry_price=1.0,
            entry_idempotency_key=f"{ep.episode_id}:entry:base",
            setup_context={"strategy": "early_momentum_v3", "paper": True},
        )
        assert outcome.trade_id is not None
        await _backdate(conn, episode_id=ep.episode_id, when=_LONG_AGO)

        closed = await episodes.mark_closed(TEST_DATABASE_URL, episode_id=ep.episode_id)
        assert closed is True

        assert await _updated_at(conn, episode_id=ep.episode_id) > _LONG_AGO
    finally:
        await _cleanup(conn, native_market_id=native_market_id)
        await conn.close()


async def test_set_execution_symbol_advances_updated_at() -> None:
    conn = await _connect_or_skip()
    native_market_id = f"TSEXECSYM{uuid.uuid4().hex[:8]}"
    try:
        strategy_id = await _ensure_strategy(conn)
        ep = await _arm(conn, strategy_id=strategy_id, native_market_id=native_market_id)
        await _backdate(conn, episode_id=ep.episode_id, when=_LONG_AGO)

        updated = await episodes.set_execution_symbol(
            TEST_DATABASE_URL,
            episode_id=ep.episode_id,
            execution_symbol=f"{native_market_id}/USDT:USDT",
        )
        assert updated is True

        assert await _updated_at(conn, episode_id=ep.episode_id) > _LONG_AGO
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT execution_symbol FROM app.early_momentum_episodes WHERE episode_id = %s",
                (ep.episode_id,),
            )
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == f"{native_market_id}/USDT:USDT"
    finally:
        await _cleanup(conn, native_market_id=native_market_id)
        await conn.close()


async def test_reap_expired_while_claimed_advances_updated_at() -> None:
    """A claimed row whose own episode window (expires_at) ran out --
    reaped unconditionally regardless of claim_attempts, distinct from the
    infrastructure-failure branch below (see episodes._REAP_EXPIRED_WHILE_CLAIMED's
    own docstring)."""
    conn = await _connect_or_skip()
    native_market_id = f"TSREAPCLM{uuid.uuid4().hex[:8]}"
    try:
        strategy_id = await _ensure_strategy(conn)
        ep = await _arm(conn, strategy_id=strategy_id, native_market_id=native_market_id)
        claim = await episodes.claim_episode(TEST_DATABASE_URL, episode_id=ep.episode_id)
        assert claim.claimed is True
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE app.early_momentum_episodes "
                "SET expires_at = now() - interval '1 second' WHERE episode_id = %s",
                (ep.episode_id,),
            )
        await _backdate(conn, episode_id=ep.episode_id, when=_LONG_AGO)

        summary = await episodes.reap_overdue(TEST_DATABASE_URL)
        assert summary is not None
        assert summary.expired_while_claimed >= 1

        assert await _updated_at(conn, episode_id=ep.episode_id) > _LONG_AGO
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status FROM app.early_momentum_episodes WHERE episode_id = %s",
                (ep.episode_id,),
            )
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == "expired"
    finally:
        await _cleanup(conn, native_market_id=native_market_id)
        await conn.close()


async def test_reap_infrastructure_failed_claims_advances_updated_at() -> None:
    """A claimed row whose lease has expired at least max_claim_attempts
    times over while the episode window itself is still open."""
    conn = await _connect_or_skip()
    native_market_id = f"TSREAPINF{uuid.uuid4().hex[:8]}"
    max_attempts = 5
    try:
        strategy_id = await _ensure_strategy(conn)
        # A long ttl so expires_at (the episode window) stays in the
        # future -- only the claim lease has run out, and enough times to
        # trip the infrastructure-failure threshold, not the plain
        # expired-window reap path above.
        ep = await _arm(
            conn, strategy_id=strategy_id, native_market_id=native_market_id, ttl_seconds=3600
        )
        claim = await episodes.claim_episode(TEST_DATABASE_URL, episode_id=ep.episode_id)
        assert claim.claimed is True
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE app.early_momentum_episodes "
                "SET claim_expires_at = now() - interval '1 second', "
                "claim_attempts = %s WHERE episode_id = %s",
                (max_attempts, ep.episode_id),
            )
        await _backdate(conn, episode_id=ep.episode_id, when=_LONG_AGO)

        summary = await episodes.reap_overdue(TEST_DATABASE_URL, max_claim_attempts=max_attempts)
        assert summary is not None
        assert summary.infrastructure_failed_claims >= 1

        assert await _updated_at(conn, episode_id=ep.episode_id) > _LONG_AGO
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status FROM app.early_momentum_episodes WHERE episode_id = %s",
                (ep.episode_id,),
            )
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == "rejected"
    finally:
        await _cleanup(conn, native_market_id=native_market_id)
        await conn.close()
