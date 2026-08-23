"""Real-Postgres coverage for early_momentum_net_evidence_repository.py.

Follows this package's own repository integration-test convention
(test_momentum_flow_paper_repository_integration.py): skips when the local
migrated development Postgres is unavailable, real in CI and any developer
environment with `make dev` up.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.early_momentum_net_evidence import (
    EXPECTED_CONTRACT_SHA256_HEX,
    STRATEGY_NAME,
    STRATEGY_VERSION,
)
from schurfer_analytics.early_momentum_net_evidence_repository import (
    EarlyMomentumNetEvidenceRepository,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"
_TEST_EXCHANGE = "test_net_evidence"
_EXPECTED_HASH = bytes.fromhex(EXPECTED_CONTRACT_SHA256_HEX)


async def _connect_or_skip() -> AsyncEngine:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"no local postgres reachable: {exc}")
    return engine


_INSERT_STRATEGY = text("""
    INSERT INTO app.strategies (name, version, description)
    VALUES (:name, :version, 'net-evidence integration test')
    ON CONFLICT (name, version) DO UPDATE SET updated_at = now()
    RETURNING id
""")

_INSERT_EPISODE = text("""
    INSERT INTO app.early_momentum_episodes (
        episode_id, strategy_id, contract_sha256, source_exchange, source_native_id,
        exchange, native_market_id, execution_symbol, execution_identity_key,
        source_identity_key, cluster_key, ceiling, features, armed_at, expires_at, status
    ) VALUES (
        :episode_id, :strategy_id, :contract_sha256, :exchange, :native_market_id,
        :exchange, :native_market_id, NULL, :identity_key,
        :identity_key, :cluster_key, 1.0, '{}'::jsonb, :armed_at, :expires_at, :status
    )
""")

_INSERT_TRADE = text("""
    INSERT INTO app.trades (
        strategy_id, symbol, exchange, market_type, side,
        size_usd, leverage, entry_price, entry_at,
        fees_usd, funding_usd, slippage_usd,
        gross_pnl_usd, gross_pnl_pct, net_pnl_usd, net_pnl_pct,
        accounting_version, accounting_status, status,
        setup_context, notes, episode_id, entry_idempotency_key,
        exit_price, exit_at
    ) VALUES (
        :strategy_id, :symbol, :exchange, 'perp', 'long',
        100.0, 5.0, 1.0, :entry_at,
        0.5, 0.1, 0.2,
        25.0, 25.0, 24.2, 24.2,
        'paper_conservative_costs_v1', 'complete', 'closed',
        :setup_context, 'take_profit move=5.0%', :episode_id, :idempotency_key,
        1.05, :exit_at
    ) RETURNING id
""")


async def _ensure_strategy(connection: AsyncConnection, *, name: str, version: str) -> int:
    result = await connection.execute(_INSERT_STRATEGY, {"name": name, "version": version})
    return int(result.scalar_one())


async def _cleanup(connection: AsyncConnection, *, exchange: str) -> None:
    await connection.execute(
        text(
            "DELETE FROM app.trades WHERE episode_id IN "
            "(SELECT episode_id FROM app.early_momentum_episodes WHERE exchange = :exchange)"
        ),
        {"exchange": exchange},
    )
    await connection.execute(
        text(
            "DELETE FROM app.trades WHERE exchange = :exchange AND episode_id IS NULL "
            "AND setup_context->>'strategy' LIKE 'early_momentum%'"
        ),
        {"exchange": exchange},
    )
    await connection.execute(
        text("DELETE FROM app.early_momentum_episodes WHERE exchange = :exchange"),
        {"exchange": exchange},
    )


async def test_fetch_scopes_episodes_to_armed_at_window_not_entry_at() -> None:
    """The colleague-flagged case: an episode armed BEFORE cohort_start
    must never appear, even if its trade's entry_at falls inside the
    window -- membership is armed_at, always."""
    engine = await _connect_or_skip()
    exchange = f"{_TEST_EXCHANGE}_{uuid.uuid4().hex[:8]}"
    cohort_start = datetime.now(UTC) - timedelta(hours=2)
    cohort_end = datetime.now(UTC) + timedelta(hours=1)
    try:
        async with engine.begin() as connection:
            strategy_id = await _ensure_strategy(
                connection, name=STRATEGY_NAME, version=STRATEGY_VERSION
            )
            episode_id = str(uuid.uuid4())
            await connection.execute(
                _INSERT_EPISODE,
                {
                    "episode_id": episode_id,
                    "strategy_id": strategy_id,
                    "contract_sha256": _EXPECTED_HASH,
                    "exchange": exchange,
                    "native_market_id": "PREEXISTUSDT",
                    "identity_key": "ik1",
                    "cluster_key": "ck1",
                    "armed_at": cohort_start - timedelta(hours=1),  # BEFORE the window
                    "expires_at": cohort_start,
                    "status": "opened",
                },
            )
            await connection.execute(
                _INSERT_TRADE,
                {
                    "strategy_id": strategy_id,
                    "symbol": "PREEXIST/USDT:USDT",
                    "exchange": exchange,
                    "entry_at": cohort_start + timedelta(minutes=5),  # INSIDE the window
                    "exit_at": cohort_start + timedelta(hours=1),
                    "setup_context": json.dumps({"strategy": "early_momentum_v4", "paper": True}),
                    "episode_id": episode_id,
                    "idempotency_key": f"{episode_id}:entry",
                },
            )

        repository = EarlyMomentumNetEvidenceRepository(engine)
        dataset = await repository.fetch(cohort_start=cohort_start, cohort_end=cohort_end)
        assert dataset.episodes == ()
        # The trade is still visible via the orphan-linkage path only if it
        # declares itself v4 with no episode_id -- here it DOES have an
        # episode_id, so it is simply invisible (correctly so: it belongs
        # to an episode outside the formal window).
        assert all(t.episode_id != episode_id for t in dataset.trades)
    finally:
        async with engine.begin() as connection:
            await _cleanup(connection, exchange=exchange)
        await engine.dispose()


async def test_fetch_returns_linked_trade_for_an_in_window_episode() -> None:
    engine = await _connect_or_skip()
    exchange = f"{_TEST_EXCHANGE}_{uuid.uuid4().hex[:8]}"
    cohort_start = datetime.now(UTC) - timedelta(hours=2)
    cohort_end = datetime.now(UTC) + timedelta(hours=1)
    try:
        async with engine.begin() as connection:
            strategy_id = await _ensure_strategy(
                connection, name=STRATEGY_NAME, version=STRATEGY_VERSION
            )
            episode_id = str(uuid.uuid4())
            armed_at = cohort_start + timedelta(minutes=5)
            await connection.execute(
                _INSERT_EPISODE,
                {
                    "episode_id": episode_id,
                    "strategy_id": strategy_id,
                    "contract_sha256": _EXPECTED_HASH,
                    "exchange": exchange,
                    "native_market_id": "INWINDOWUSDT",
                    "identity_key": "ik2",
                    "cluster_key": "ck2",
                    "armed_at": armed_at,
                    "expires_at": armed_at + timedelta(hours=1),
                    "status": "opened",
                },
            )
            await connection.execute(
                _INSERT_TRADE,
                {
                    "strategy_id": strategy_id,
                    "symbol": "INWINDOW/USDT:USDT",
                    "exchange": exchange,
                    "entry_at": armed_at + timedelta(seconds=2),
                    "exit_at": armed_at + timedelta(hours=1),
                    "setup_context": json.dumps({"strategy": "early_momentum_v4", "paper": True}),
                    "episode_id": episode_id,
                    "idempotency_key": f"{episode_id}:entry",
                },
            )

        repository = EarlyMomentumNetEvidenceRepository(engine)
        dataset = await repository.fetch(cohort_start=cohort_start, cohort_end=cohort_end)
        assert len(dataset.episodes) == 1
        assert dataset.episodes[0].episode_id == episode_id
        assert len(dataset.trades) == 1
        assert dataset.trades[0].episode_id == episode_id
        assert dataset.trades[0].is_paper is True
        assert dataset.db_snapshot_at is not None
    finally:
        async with engine.begin() as connection:
            await _cleanup(connection, exchange=exchange)
        await engine.dispose()


async def test_fetch_finds_orphan_v4_trade_with_no_episode() -> None:
    engine = await _connect_or_skip()
    exchange = f"{_TEST_EXCHANGE}_{uuid.uuid4().hex[:8]}"
    cohort_start = datetime.now(UTC) - timedelta(hours=2)
    cohort_end = datetime.now(UTC) + timedelta(hours=1)
    try:
        async with engine.begin() as connection:
            strategy_id = await _ensure_strategy(
                connection, name=STRATEGY_NAME, version=STRATEGY_VERSION
            )
            await connection.execute(
                _INSERT_TRADE,
                {
                    "strategy_id": strategy_id,
                    "symbol": "ORPHAN/USDT:USDT",
                    "exchange": exchange,
                    "entry_at": cohort_start + timedelta(minutes=5),
                    "exit_at": cohort_start + timedelta(hours=1),
                    "setup_context": json.dumps({"strategy": "early_momentum_v4", "paper": True}),
                    "episode_id": None,
                    "idempotency_key": f"orphan-{uuid.uuid4().hex}",
                },
            )

        repository = EarlyMomentumNetEvidenceRepository(engine)
        dataset = await repository.fetch(cohort_start=cohort_start, cohort_end=cohort_end)
        assert dataset.episodes == ()
        assert len(dataset.trades) == 1
        assert dataset.trades[0].episode_id is None
        assert dataset.trades[0].setup_context_strategy == "early_momentum_v4"
    finally:
        async with engine.begin() as connection:
            await _cleanup(connection, exchange=exchange)
        await engine.dispose()


async def test_fetch_finds_orphan_trade_entered_shortly_after_cohort_end() -> None:
    """colleague review: an episode armed just before cohort_end can still
    open its trade after cohort_end (episode TTL/trigger delay) -- if the
    episode_id link were ever lost, that orphan must still be caught, not
    only orphans with entry_at strictly before cohort_end."""
    engine = await _connect_or_skip()
    exchange = f"{_TEST_EXCHANGE}_{uuid.uuid4().hex[:8]}"
    cohort_start = datetime.now(UTC) - timedelta(hours=2)
    cohort_end = datetime.now(UTC)
    try:
        async with engine.begin() as connection:
            strategy_id = await _ensure_strategy(
                connection, name=STRATEGY_NAME, version=STRATEGY_VERSION
            )
            await connection.execute(
                _INSERT_TRADE,
                {
                    "strategy_id": strategy_id,
                    "symbol": "LATEORPHAN/USDT:USDT",
                    "exchange": exchange,
                    # 30 minutes after cohort_end -- inside the widened
                    # orphan window (cohort_end + COHORT_MATURITY_BUFFER_
                    # SECONDS), outside a naive `< cohort_end` bound.
                    "entry_at": cohort_end + timedelta(minutes=30),
                    "exit_at": cohort_end + timedelta(hours=1),
                    "setup_context": json.dumps({"strategy": "early_momentum_v4", "paper": True}),
                    "episode_id": None,
                    "idempotency_key": f"late-orphan-{uuid.uuid4().hex}",
                },
            )

        repository = EarlyMomentumNetEvidenceRepository(engine)
        dataset = await repository.fetch(cohort_start=cohort_start, cohort_end=cohort_end)
        assert len(dataset.trades) == 1
        assert dataset.trades[0].symbol == "LATEORPHAN/USDT:USDT"
    finally:
        async with engine.begin() as connection:
            await _cleanup(connection, exchange=exchange)
        await engine.dispose()


async def test_fetch_does_not_find_orphan_trade_entered_long_after_cohort_end() -> None:
    """The widened window is still bounded -- a trade entered well past
    even the maturity buffer is not a plausible orphan of this cohort."""
    engine = await _connect_or_skip()
    exchange = f"{_TEST_EXCHANGE}_{uuid.uuid4().hex[:8]}"
    cohort_start = datetime.now(UTC) - timedelta(hours=2)
    cohort_end = datetime.now(UTC)
    try:
        async with engine.begin() as connection:
            strategy_id = await _ensure_strategy(
                connection, name=STRATEGY_NAME, version=STRATEGY_VERSION
            )
            await connection.execute(
                _INSERT_TRADE,
                {
                    "strategy_id": strategy_id,
                    "symbol": "FARORPHAN/USDT:USDT",
                    "exchange": exchange,
                    "entry_at": cohort_end + timedelta(hours=24),
                    "exit_at": cohort_end + timedelta(hours=25),
                    "setup_context": json.dumps({"strategy": "early_momentum_v4", "paper": True}),
                    "episode_id": None,
                    "idempotency_key": f"far-orphan-{uuid.uuid4().hex}",
                },
            )

        repository = EarlyMomentumNetEvidenceRepository(engine)
        dataset = await repository.fetch(cohort_start=cohort_start, cohort_end=cohort_end)
        assert dataset.trades == ()
    finally:
        async with engine.begin() as connection:
            await _cleanup(connection, exchange=exchange)
        await engine.dispose()


async def test_fetch_reads_entry_impact_from_entry_vwap_impact_bps_not_market_quality() -> None:
    """colleague review: setup_context.market_quality.ask_impact_bps is
    measured at the market-quality gate's larger safety-margin depth
    target, not the real traded notional -- entry_vwap_impact_bps (a
    top-level setup_context field) is the real entry-side reading at the
    actual trade size and must be what capacity evidence reads."""
    engine = await _connect_or_skip()
    exchange = f"{_TEST_EXCHANGE}_{uuid.uuid4().hex[:8]}"
    cohort_start = datetime.now(UTC) - timedelta(hours=2)
    cohort_end = datetime.now(UTC) + timedelta(hours=1)
    try:
        async with engine.begin() as connection:
            strategy_id = await _ensure_strategy(
                connection, name=STRATEGY_NAME, version=STRATEGY_VERSION
            )
            episode_id = str(uuid.uuid4())
            armed_at = cohort_start + timedelta(minutes=5)
            await connection.execute(
                _INSERT_EPISODE,
                {
                    "episode_id": episode_id,
                    "strategy_id": strategy_id,
                    "contract_sha256": _EXPECTED_HASH,
                    "exchange": exchange,
                    "native_market_id": "IMPACTUSDT",
                    "identity_key": "ik-impact",
                    "cluster_key": "ck-impact",
                    "armed_at": armed_at,
                    "expires_at": armed_at + timedelta(hours=1),
                    "status": "opened",
                },
            )
            await connection.execute(
                _INSERT_TRADE,
                {
                    "strategy_id": strategy_id,
                    "symbol": "IMPACT/USDT:USDT",
                    "exchange": exchange,
                    "entry_at": armed_at + timedelta(seconds=2),
                    "exit_at": armed_at + timedelta(hours=1),
                    "setup_context": json.dumps(
                        {
                            "strategy": "early_momentum_v4",
                            "paper": True,
                            # The real reading at the actual traded size.
                            "entry_vwap_impact_bps": 5.5,
                            # A decoy at the gate's larger safety-margin
                            # depth target -- must NOT be what gets read.
                            "market_quality": {"ask_impact_bps": 99.9, "bid_impact_bps": 88.8},
                        }
                    ),
                    "episode_id": episode_id,
                    "idempotency_key": f"{episode_id}:entry:base",
                },
            )

        repository = EarlyMomentumNetEvidenceRepository(engine)
        dataset = await repository.fetch(cohort_start=cohort_start, cohort_end=cohort_end)
        assert len(dataset.trades) == 1
        assert dataset.trades[0].entry_ask_impact_bps == pytest.approx(5.5)
    finally:
        async with engine.begin() as connection:
            await _cleanup(connection, exchange=exchange)
        await engine.dispose()


async def test_fetch_never_writes_to_the_database() -> None:
    """A read-only-transaction violation (an accidental write anywhere in
    fetch()) would raise, not silently succeed -- exercising fetch() against
    a real DB and confirming no exception is itself the assertion that the
    transaction stayed read-only."""
    engine = await _connect_or_skip()
    cohort_start = datetime.now(UTC) - timedelta(hours=2)
    cohort_end = datetime.now(UTC) + timedelta(hours=1)
    try:
        repository = EarlyMomentumNetEvidenceRepository(engine)
        await repository.fetch(cohort_start=cohort_start, cohort_end=cohort_end)
    finally:
        await engine.dispose()


async def test_legacy_context_keeps_versions_separate_and_never_touches_v4() -> None:
    engine = await _connect_or_skip()
    exchange = f"{_TEST_EXCHANGE}_{uuid.uuid4().hex[:8]}"
    cohort_end = datetime.now(UTC) + timedelta(hours=1)
    try:
        async with engine.begin() as connection:
            strategy_id = await _ensure_strategy(connection, name=STRATEGY_NAME, version="2")
            await connection.execute(
                _INSERT_TRADE,
                {
                    "strategy_id": strategy_id,
                    "symbol": "LEGACY/USDT:USDT",
                    "exchange": exchange,
                    "entry_at": datetime.now(UTC) - timedelta(hours=1),
                    "exit_at": datetime.now(UTC) - timedelta(minutes=30),
                    "setup_context": json.dumps({"strategy": "early_momentum_v2", "paper": True}),
                    "episode_id": None,
                    "idempotency_key": f"legacy-{uuid.uuid4().hex}",
                },
            )

        repository = EarlyMomentumNetEvidenceRepository(engine)
        context = await repository.fetch_legacy_context(cohort_end=cohort_end)
        labels = {row.setup_context_strategy for row in context}
        assert "early_momentum_v2" in labels
        assert "early_momentum_v4" not in labels
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM app.trades WHERE exchange = :exchange"), {"exchange": exchange}
            )
        await engine.dispose()
