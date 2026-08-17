"""Real-Postgres integration test for MomentumUniverseIdentityRepository.

Every other *_repository_test.py in this package mocks the SQLAlchemy
engine/connection and asserts on the compiled SQL string -- a real gap for
THIS repository specifically: it is brand new, and a mock cannot catch a
Table() column definition that has silently drifted from the actual
migration DDL (a wrong column name, a wrong schema, a type SQLAlchemy
coerces differently than expected) -- exactly the same underlying "never
verified against the real schema" gap that produced this session's two
production NULL-scan crashes on the Go side (see apps/api-gateway/internal/
pumps/momentum_watch_integration_test.go's own doc comment), even though
the concrete failure shape differs here (SQLAlchemy raises instead of a
silent bad scan).

Matches infra/docker/docker-compose.dev.yml's local dev Postgres, same
convention as the Go integration tests. Skips (not fails) when no
Postgres is reachable so `pytest` still passes without a live database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.momentum_universe_identity_classifier import (
    MATCH_RULESET_VERSION,
    classify,
)
from schurfer_analytics.momentum_universe_identity_repository import (
    MomentumUniverseIdentityRepository,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"

_TEST_EXCHANGE_A = "test_identity_repo_a"
_TEST_EXCHANGE_B = "test_identity_repo_b"


async def _connect_or_skip() -> AsyncEngine:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"no local postgres reachable: {exc}")
    return engine


async def _seed_snapshot(
    engine: AsyncEngine, *, exchange: str, base: str, onboarded_at: datetime
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO app.momentum_universe_snapshots
                    (exchange, universe_version, catalog_version, capture_version,
                     schema_version, captured_at, instrument_count, payload_hash)
                VALUES
                    (:exchange, 'test_universe_v1', 'test_catalog_v1', 'test_capture_v1',
                     'test_schema_v1', now(), 1, decode(repeat('ab', 32), 'hex'))
                ON CONFLICT DO NOTHING
            """),
            {"exchange": exchange},
        )
        await connection.execute(
            text("""
                INSERT INTO app.momentum_universe_instruments
                    (exchange, universe_version, catalog_version, native_market_id,
                     base, quote, settle, native_market_type, canonical_market_type,
                     onboarded_at, identity_status, identity_key, metadata_hash)
                VALUES
                    (:exchange, 'test_universe_v1', 'test_catalog_v1', :native_market_id,
                     :base, 'USDT', 'USDT', 'LinearPerpetual', 'linear_usdt_perpetual',
                     :onboarded_at, 'ready', :identity_key,
                     decode(repeat('cd', 32), 'hex'))
                ON CONFLICT DO NOTHING
            """),
            {
                "exchange": exchange,
                "native_market_id": f"{base}USDT",
                "base": base,
                "onboarded_at": onboarded_at,
                "identity_key": f"{exchange}:linear_usdt_perpetual:{base}USDT:"
                f"{int(onboarded_at.timestamp() * 1000)}",
            },
        )


async def _seed_non_ready_instrument(engine: AsyncEngine, *, exchange: str, base: str) -> None:
    """Adds a second instrument row to the SAME snapshot _seed_snapshot
    already wrote for this exchange, with identity_status other than
    'ready' -- onboarded_at/identity_key must be NULL for a non-ready row
    (migration 0028's own identity_key_only_when_ready CHECK constraint).
    Exists so test_latest_ready_instruments_excludes_non_ready_rows has a
    real non-ready row to prove the WHERE identity_status = 'ready' filter
    (repository.py's own latest_ready_instruments) actually excludes --
    every other seeded row in this test file is 'ready', so without this
    a regression that dropped or weakened that filter would go uncaught.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO app.momentum_universe_instruments
                    (exchange, universe_version, catalog_version, native_market_id,
                     base, quote, settle, native_market_type, canonical_market_type,
                     onboarded_at, identity_status, identity_key, metadata_hash)
                VALUES
                    (:exchange, 'test_universe_v1', 'test_catalog_v1', :native_market_id,
                     :base, 'USDT', 'USDT', 'LinearPerpetual', 'linear_usdt_perpetual',
                     NULL, 'missing_onboarded_at', NULL,
                     decode(repeat('ef', 32), 'hex'))
                ON CONFLICT DO NOTHING
            """),
            {
                "exchange": exchange,
                "native_market_id": f"{base}NOTREADYUSDT",
                "base": base,
            },
        )


async def _cleanup(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM app.momentum_universe_cluster_members WHERE exchange IN (:a, :b)"),
            {"a": _TEST_EXCHANGE_A, "b": _TEST_EXCHANGE_B},
        )
        await connection.execute(
            text("""
                DELETE FROM app.momentum_universe_asset_clusters
                WHERE cluster_key NOT IN (
                    SELECT cluster_key FROM app.momentum_universe_cluster_members
                )
            """)
        )
        await connection.execute(
            text("DELETE FROM app.momentum_universe_instruments WHERE exchange IN (:a, :b)"),
            {"a": _TEST_EXCHANGE_A, "b": _TEST_EXCHANGE_B},
        )
        await connection.execute(
            text("DELETE FROM app.momentum_universe_snapshots WHERE exchange IN (:a, :b)"),
            {"a": _TEST_EXCHANGE_A, "b": _TEST_EXCHANGE_B},
        )


async def test_latest_ready_instruments_excludes_non_ready_rows() -> None:
    engine = await _connect_or_skip()
    try:
        onboarded_at = (datetime.now(tz=UTC) - timedelta(days=900)).replace(microsecond=0)
        await _seed_snapshot(
            engine, exchange=_TEST_EXCHANGE_A, base="READYTEST", onboarded_at=onboarded_at
        )
        await _seed_non_ready_instrument(engine, exchange=_TEST_EXCHANGE_A, base="READYTEST")

        repository = MomentumUniverseIdentityRepository(engine)
        instruments = await repository.latest_ready_instruments(_TEST_EXCHANGE_A)

        assert len(instruments) == 1
        assert instruments[0].base == "READYTEST"
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_round_trip_against_real_schema() -> None:
    engine = await _connect_or_skip()
    try:
        onboarded_at = (datetime.now(tz=UTC) - timedelta(days=900)).replace(microsecond=0)
        await _seed_snapshot(
            engine, exchange=_TEST_EXCHANGE_A, base="REPOTEST", onboarded_at=onboarded_at
        )
        await _seed_snapshot(
            engine, exchange=_TEST_EXCHANGE_B, base="REPOTEST", onboarded_at=onboarded_at
        )

        repository = MomentumUniverseIdentityRepository(engine)
        instruments_a = await repository.latest_ready_instruments(_TEST_EXCHANGE_A)
        instruments_b = await repository.latest_ready_instruments(_TEST_EXCHANGE_B)
        assert len(instruments_a) == 1
        assert len(instruments_b) == 1
        assert instruments_a[0].base == "REPOTEST"
        assert instruments_a[0].onboarded_at == onboarded_at

        resolved_at = datetime.now(tz=UTC)
        clusters = classify(
            {_TEST_EXCHANGE_A: instruments_a, _TEST_EXCHANGE_B: instruments_b},
            resolved_at=resolved_at,
        )
        assert len(clusters) == 1
        assert clusters[0].base == "REPOTEST"
        assert {member.match_status for member in clusters[0].members} == {"confirmed"}

        written = await repository.persist_clusters(
            clusters, match_ruleset_version=MATCH_RULESET_VERSION, resolved_at=resolved_at
        )
        assert written == 2

        async with engine.connect() as connection:
            cluster_row = (
                (
                    await connection.execute(
                        text(
                            "SELECT base, member_count, match_ruleset_version "
                            "FROM app.momentum_universe_asset_clusters WHERE cluster_key = :key"
                        ),
                        {"key": clusters[0].cluster_key},
                    )
                )
                .mappings()
                .one()
            )
            assert cluster_row["base"] == "REPOTEST"
            assert cluster_row["member_count"] == 2
            assert cluster_row["match_ruleset_version"] == MATCH_RULESET_VERSION

            member_rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT exchange, match_status "
                            "FROM app.momentum_universe_cluster_members "
                            "WHERE cluster_key = :key ORDER BY exchange"
                        ),
                        {"key": clusters[0].cluster_key},
                    )
                )
                .mappings()
                .all()
            )
            assert [row["exchange"] for row in member_rows] == [_TEST_EXCHANGE_A, _TEST_EXCHANGE_B]
            assert all(row["match_status"] == "confirmed" for row in member_rows)

        # A second run with one exchange's instrument gone must clear the stale
        # cluster entirely, not leave a one-sided leftover (full-resync contract).
        second_run_clusters = classify({_TEST_EXCHANGE_A: instruments_a}, resolved_at=resolved_at)
        assert second_run_clusters == ()
        await repository.persist_clusters(
            second_run_clusters,
            match_ruleset_version=MATCH_RULESET_VERSION,
            resolved_at=resolved_at,
        )
        async with engine.connect() as connection:
            remaining = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM app.momentum_universe_asset_clusters "
                        "WHERE cluster_key = :key"
                    ),
                    {"key": clusters[0].cluster_key},
                )
            ).scalar_one()
            assert remaining == 0
    finally:
        await _cleanup(engine)
        await engine.dispose()
