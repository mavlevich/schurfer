"""A worker's session advisory lock must not pin an open PostgreSQL transaction."""

from __future__ import annotations

from uuid import uuid4

import pytest
from schurfer_analytics.momentum_flow_paper_repository import MomentumFlowPaperRepository
from schurfer_analytics.momentum_flow_watch_repository import MomentumFlowWatchRepository
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repository_class", (MomentumFlowPaperRepository, MomentumFlowWatchRepository)
)
async def test_session_lock_survives_commit_without_idle_transaction(
    repository_class: type[MomentumFlowPaperRepository] | type[MomentumFlowWatchRepository],
) -> None:
    probe = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with probe.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"no local PostgreSQL reachable: {exc}")
    finally:
        await probe.dispose()

    version = f"test_worker_lock_{uuid4()}"
    owner = repository_class.from_url(TEST_DATABASE_URL)
    competitor = repository_class.from_url(TEST_DATABASE_URL)
    try:
        assert await owner.acquire_worker_lock(version)
        await owner.assert_worker_lock()
        assert owner._lock_connection is not None
        # On the original code, the SELECT implicitly opened a transaction
        # that stayed idle for the worker's entire lifetime.
        assert not owner._lock_connection.in_transaction()
        raw_connection = await owner._lock_connection.get_raw_connection()
        driver_connection = raw_connection.driver_connection
        assert driver_connection is not None
        backend_pid = driver_connection.info.backend_pid
        observer = create_async_engine(TEST_DATABASE_URL)
        try:
            async with observer.connect() as connection:
                state, xact_start = (
                    await connection.execute(
                        text("SELECT state, xact_start FROM pg_stat_activity WHERE pid = :pid"),
                        {"pid": backend_pid},
                    )
                ).one()
            assert state == "idle"
            assert xact_start is None
        finally:
            await observer.dispose()
        assert not await competitor.acquire_worker_lock(version)

        await owner.close()
        assert await competitor.acquire_worker_lock(version)
        assert competitor._lock_connection is not None
        assert not competitor._lock_connection.in_transaction()
    finally:
        await owner.close()
        await competitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repository_class", (MomentumFlowPaperRepository, MomentumFlowWatchRepository)
)
async def test_lost_backend_cannot_silently_keep_worker_lock(
    repository_class: type[MomentumFlowPaperRepository] | type[MomentumFlowWatchRepository],
) -> None:
    version = f"test_lost_worker_lock_{uuid4()}"
    owner = repository_class.from_url(TEST_DATABASE_URL)
    successor = repository_class.from_url(TEST_DATABASE_URL)
    observer = create_async_engine(TEST_DATABASE_URL)
    try:
        try:
            async with observer.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception as exc:
            pytest.skip(f"no local PostgreSQL reachable: {exc}")
        assert await owner.acquire_worker_lock(version)
        assert owner._lock_connection is not None
        raw_connection = await owner._lock_connection.get_raw_connection()
        driver_connection = raw_connection.driver_connection
        assert driver_connection is not None
        backend_pid = driver_connection.info.backend_pid
        async with observer.begin() as connection:
            terminated = (
                await connection.execute(
                    text("SELECT pg_terminate_backend(:pid)"), {"pid": backend_pid}
                )
            ).scalar_one()
        assert terminated
        with pytest.raises(RuntimeError, match=r"worker lock session (was lost|changed)"):
            await owner.assert_worker_lock()
        assert await successor.acquire_worker_lock(version)
    finally:
        await owner.close()
        await successor.close()
        await observer.dispose()
