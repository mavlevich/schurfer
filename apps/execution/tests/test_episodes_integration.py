"""Real-Postgres coverage for episodes.py's concurrency-sensitive SQL.

Only a real database proves the atomic claim (and its reclaim branch) and
the partial unique index actually behave under real concurrent access --
not just that the text() statements parse. Matches
infra/docker/docker-compose.dev.yml's local dev Postgres, mirroring the
`_connect_or_skip` convention already used by apps/analytics's own
real-Postgres tests -- adapted to psycopg directly here since
apps/execution's production code (journal.py, episodes.py) talks to
Postgres via raw psycopg, not SQLAlchemy.

Skips when no Postgres is reachable locally, unless REQUIRE_INTEGRATION_DB=1
is set (CI sets this so a broken/unprovisioned Postgres service fails the
build loudly instead of these tests silently skipping and the run still
going green -- colleague review).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid

import psycopg
import pytest
from schurfer_execution import episodes

TEST_DATABASE_URL = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"
_TEST_EXCHANGE = "test_early_momentum_v3"


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


async def test_connect_or_skip_raises_when_require_integration_db_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CI-enforcement path itself: with REQUIRE_INTEGRATION_DB=1, an
    unreachable Postgres must fail the test, never silently skip it."""

    async def _failing_connect(*_args: object, **_kwargs: object) -> psycopg.AsyncConnection:
        raise OSError("connection refused")

    monkeypatch.setenv("REQUIRE_INTEGRATION_DB", "1")
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _failing_connect)
    with pytest.raises(RuntimeError, match="REQUIRE_INTEGRATION_DB"):
        await _connect_or_skip()


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


async def test_two_concurrent_claims_only_one_wins() -> None:
    conn = await _connect_or_skip()
    native_market_id = f"CONCURRENT{uuid.uuid4().hex[:8]}"
    try:
        strategy_id = await _ensure_strategy(conn)
        ep = await _arm(conn, strategy_id=strategy_id, native_market_id=native_market_id)

        outcomes = await asyncio.gather(
            episodes.claim_episode(TEST_DATABASE_URL, episode_id=ep.episode_id),
            episodes.claim_episode(TEST_DATABASE_URL, episode_id=ep.episode_id),
        )
        claimed_count = sum(1 for o in outcomes if o.claimed)
        assert claimed_count == 1

        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT claim_attempts FROM app.early_momentum_episodes WHERE episode_id = %s",
                (ep.episode_id,),
            )
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 1
    finally:
        await _cleanup(conn, native_market_id=native_market_id)
        await conn.close()


async def test_reclaim_after_expired_lease_succeeds_with_a_new_token() -> None:
    conn = await _connect_or_skip()
    native_market_id = f"RECLAIM{uuid.uuid4().hex[:8]}"
    try:
        strategy_id = await _ensure_strategy(conn)
        ep = await _arm(conn, strategy_id=strategy_id, native_market_id=native_market_id)

        first = await episodes.claim_episode(TEST_DATABASE_URL, episode_id=ep.episode_id)
        assert first.claimed is True

        # Simulate the lease having expired (a crashed worker never
        # terminated it) -- move claim_expires_at into the past directly.
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE app.early_momentum_episodes "
                "SET claim_expires_at = now() - interval '1 second' "
                "WHERE episode_id = %s",
                (ep.episode_id,),
            )

        second = await episodes.claim_episode(TEST_DATABASE_URL, episode_id=ep.episode_id)
        assert second.claimed is True
        assert second.claim_token != first.claim_token

        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT claim_attempts FROM app.early_momentum_episodes WHERE episode_id = %s",
                (ep.episode_id,),
            )
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 2
    finally:
        await _cleanup(conn, native_market_id=native_market_id)
        await conn.close()


async def test_expired_episode_cannot_be_reclaimed_even_with_an_expired_lease() -> None:
    """Regression (colleague review): expires_at > now() must gate BOTH the
    armed and the claimed-and-expired reclaim branches -- an episode whose
    own expires_at has passed must never be reclaimed, only reaped."""
    conn = await _connect_or_skip()
    native_market_id = f"EXPIREDEP{uuid.uuid4().hex[:8]}"
    try:
        strategy_id = await _ensure_strategy(conn)
        ep = await _arm(conn, strategy_id=strategy_id, native_market_id=native_market_id)

        first = await episodes.claim_episode(TEST_DATABASE_URL, episode_id=ep.episode_id)
        assert first.claimed is True

        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE app.early_momentum_episodes "
                "SET claim_expires_at = now() - interval '1 second', "
                "    expires_at = now() - interval '1 second' "
                "WHERE episode_id = %s",
                (ep.episode_id,),
            )

        second = await episodes.claim_episode(TEST_DATABASE_URL, episode_id=ep.episode_id)
        assert second.claimed is False
    finally:
        await _cleanup(conn, native_market_id=native_market_id)
        await conn.close()


async def test_live_instrument_partial_index_rejects_a_second_armed_episode() -> None:
    conn = await _connect_or_skip()
    native_market_id = f"LIVEIDX{uuid.uuid4().hex[:8]}"
    try:
        strategy_id = await _ensure_strategy(conn)
        first = await _arm(conn, strategy_id=strategy_id, native_market_id=native_market_id)
        assert first is not None

        second = await episodes.create_episode(
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
            ttl_seconds=60,
        )
        assert second is None

        # Once the first episode goes terminal, a new one can arm again.
        await episodes.terminate_episode(
            TEST_DATABASE_URL,
            episode_id=first.episode_id,
            reason=episodes.REASON_EXPIRED_WITHOUT_BREAKOUT,
            status=episodes.STATUS_EXPIRED,
        )
        third = await episodes.create_episode(
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
            ttl_seconds=60,
        )
        assert third is not None
    finally:
        await _cleanup(conn, native_market_id=native_market_id)
        await conn.close()


async def test_create_rejected_episode_dedups_repeat_rejections_within_the_window() -> None:
    """A still-disqualified candidate re-evaluated on every scanner tick
    must not insert a fresh 'rejected' row every time -- only the first one
    within the dedup window persists (colleague review)."""
    conn = await _connect_or_skip()
    source_native_id = f"DEDUP{uuid.uuid4().hex[:8]}"
    try:
        strategy_id = await _ensure_strategy(conn)
        for _ in range(3):
            recorded = await episodes.create_rejected_episode(
                TEST_DATABASE_URL,
                strategy_id=strategy_id,
                contract_sha256=hashlib.sha256(source_native_id.encode()).digest(),
                source_exchange=_TEST_EXCHANGE,
                source_native_id=source_native_id,
                exchange=_TEST_EXCHANGE,
                native_market_id="",
                ceiling=1.0,
                features={},
                reason=episodes.REASON_IDENTITY_UNRESOLVED,
                dedup_window_seconds=3600,
            )
            assert recorded is True

        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM app.early_momentum_episodes "
                "WHERE source_exchange = %s AND source_native_id = %s AND terminal_reason = %s",
                (_TEST_EXCHANGE, source_native_id, episodes.REASON_IDENTITY_UNRESOLVED),
            )
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 1

        # A different reason for the same instrument is not deduped against
        # the first -- it's genuinely new information.
        recorded = await episodes.create_rejected_episode(
            TEST_DATABASE_URL,
            strategy_id=strategy_id,
            contract_sha256=hashlib.sha256(source_native_id.encode()).digest(),
            source_exchange=_TEST_EXCHANGE,
            source_native_id=source_native_id,
            exchange=_TEST_EXCHANGE,
            native_market_id="",
            ceiling=1.0,
            features={},
            reason=episodes.REASON_ROUTE_INVALIDATED,
            dedup_window_seconds=3600,
        )
        assert recorded is True

        # Outside the dedup window (a window of 0 seconds), the same reason
        # inserts again.
        recorded = await episodes.create_rejected_episode(
            TEST_DATABASE_URL,
            strategy_id=strategy_id,
            contract_sha256=hashlib.sha256(source_native_id.encode()).digest(),
            source_exchange=_TEST_EXCHANGE,
            source_native_id=source_native_id,
            exchange=_TEST_EXCHANGE,
            native_market_id="",
            ceiling=1.0,
            features={},
            reason=episodes.REASON_IDENTITY_UNRESOLVED,
            dedup_window_seconds=0,
        )
        assert recorded is True

        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM app.early_momentum_episodes "
                "WHERE source_exchange = %s AND source_native_id = %s",
                (_TEST_EXCHANGE, source_native_id),
            )
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 3
    finally:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM app.early_momentum_episodes "
                "WHERE source_exchange = %s AND source_native_id = %s",
                (_TEST_EXCHANGE, source_native_id),
            )
        await conn.close()


async def test_open_trade_for_episode_commits_trade_and_episode_together() -> None:
    conn = await _connect_or_skip()
    native_market_id = f"OPENTX{uuid.uuid4().hex[:8]}"
    try:
        from schurfer_execution import journal

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
        assert outcome.created is True

        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status FROM app.early_momentum_episodes WHERE episode_id = %s",
                (ep.episode_id,),
            )
            episode_row = await cur.fetchone()
            await cur.execute(
                "SELECT episode_id, entry_idempotency_key FROM app.trades WHERE id = %s",
                (outcome.trade_id,),
            )
            trade_row = await cur.fetchone()
        assert episode_row is not None
        assert episode_row[0] == "opened"
        assert trade_row is not None
        assert str(trade_row[0]) == ep.episode_id
        assert trade_row[1] == f"{ep.episode_id}:entry:base"

        # Idempotent retry with the same claim token must recover, not
        # duplicate.
        retry = await journal.open_trade_for_episode(
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
        assert retry.trade_id == outcome.trade_id
        assert retry.recovered is True
    finally:
        await _cleanup(conn, native_market_id=native_market_id)
        await conn.close()


async def test_open_trade_for_episode_rejects_lease_expired_between_claim_and_commit() -> None:
    """A slow quote/liquidity check can eat the whole claim lease between
    claim_episode() and open_trade_for_episode()'s commit. Artificially
    expire the lease in that gap and confirm no trade row is created --
    the freshness check must be re-evaluated at commit time, not trusted
    from claim time."""
    conn = await _connect_or_skip()
    native_market_id = f"LEASEEXP{uuid.uuid4().hex[:8]}"
    try:
        from schurfer_execution import journal

        strategy_id = await _ensure_strategy(conn)
        ep = await _arm(conn, strategy_id=strategy_id, native_market_id=native_market_id)
        claim = await episodes.claim_episode(
            TEST_DATABASE_URL, episode_id=ep.episode_id, lease_seconds=30
        )
        assert claim.claimed is True
        assert claim.claim_token is not None

        # Simulate the lease lapsing underneath the claim -- e.g. a slow
        # quote/liquidity check ate the whole lease window.
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE app.early_momentum_episodes "
                "SET claim_expires_at = now() - interval '1 second' "
                "WHERE episode_id = %s",
                (ep.episode_id,),
            )

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
        assert outcome.trade_id is None
        assert outcome.claim_valid is False

        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status FROM app.early_momentum_episodes WHERE episode_id = %s",
                (ep.episode_id,),
            )
            episode_row = await cur.fetchone()
            await cur.execute(
                "SELECT id FROM app.trades WHERE episode_id = %s",
                (ep.episode_id,),
            )
            trade_row = await cur.fetchone()
        # The episode row itself is left alone (still 'claimed') -- it's
        # reap_overdue's job to terminate it, not open_trade_for_episode's.
        assert episode_row is not None
        assert episode_row[0] == "claimed"
        assert trade_row is None
    finally:
        await _cleanup(conn, native_market_id=native_market_id)
        await conn.close()


async def test_health_metrics_reports_real_overdue_armed_and_expired_claim_ages() -> None:
    """health_metrics() is otherwise only exercised through mocked cursor
    results (test_episodes.py) -- this is the one place its actual SQL runs
    against a real table (colleague review). health_metrics has no
    exchange/strategy scope (it's a global operational read), so this
    can't assert exact counts against a shared table other tests/processes
    may also be touching -- it asserts our own rows are correctly reflected
    in a before/after delta instead, and that the returned ages are
    positive and in the right ballpark for what we just backdated."""
    conn = await _connect_or_skip()
    armed_native_id = f"TSHMARM{uuid.uuid4().hex[:8]}"
    claimed_native_id = f"TSHMCLM{uuid.uuid4().hex[:8]}"
    try:
        strategy_id = await _ensure_strategy(conn)
        baseline = await episodes.health_metrics(TEST_DATABASE_URL)
        assert baseline["overdue_armed"] is not None
        assert baseline["expired_claims"] is not None
        baseline_overdue_armed = baseline["overdue_armed"]
        baseline_expired_claims = baseline["expired_claims"]

        overdue_armed_ep = await _arm(
            conn, strategy_id=strategy_id, native_market_id=armed_native_id
        )
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE app.early_momentum_episodes "
                "SET expires_at = now() - interval '30 seconds' WHERE episode_id = %s",
                (overdue_armed_ep.episode_id,),
            )

        expired_claim_ep = await _arm(
            conn, strategy_id=strategy_id, native_market_id=claimed_native_id
        )
        claim = await episodes.claim_episode(
            TEST_DATABASE_URL, episode_id=expired_claim_ep.episode_id
        )
        assert claim.claimed is True
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE app.early_momentum_episodes "
                "SET claim_expires_at = now() - interval '45 seconds' WHERE episode_id = %s",
                (expired_claim_ep.episode_id,),
            )

        metrics = await episodes.health_metrics(TEST_DATABASE_URL)

        assert metrics["overdue_armed"] == baseline_overdue_armed + 1
        assert metrics["expired_claims"] == baseline_expired_claims + 1
        assert metrics["oldest_overdue_armed_age_seconds"] is not None
        assert metrics["oldest_expired_claim_age_seconds"] is not None
        # Loose bounds, not exact equality -- other overdue rows (from a
        # concurrent test run, or a genuinely older stray row) can only
        # make the oldest age *larger* than what we just backdated, never
        # smaller, so a lower bound is the only side that's actually safe
        # to assert here.
        assert metrics["oldest_overdue_armed_age_seconds"] >= 25.0
        assert metrics["oldest_expired_claim_age_seconds"] >= 40.0
    finally:
        await _cleanup(conn, native_market_id=armed_native_id)
        await _cleanup(conn, native_market_id=claimed_native_id)
        await conn.close()
