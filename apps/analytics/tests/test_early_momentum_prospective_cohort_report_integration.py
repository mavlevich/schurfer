"""Real-Postgres, end-to-end coverage for early_momentum_prospective_cohort_
report.py -- proves the two things this module adds are real, not just
mocked-through: a genuinely later cohort boundary excludes a historical
episode, and a contract-hash mismatch inside the prospective window still
fails closed (maps to `blocked_integrity`, never silently `eligible`).

Follows this package's own repository integration-test convention
(`test_early_momentum_net_evidence_repository_integration.py`): skips when
the local migrated development Postgres is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from schurfer_analytics import early_momentum_prospective_cohort_report as prospective_mod
from schurfer_analytics.early_momentum_net_evidence import (
    COHORT_MATURITY_BUFFER_SECONDS,
    EXPECTED_CONTRACT_SHA256_HEX,
    STRATEGY_NAME,
    STRATEGY_VERSION,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"
_TEST_EXCHANGE = "test_prospective_cohort"
_EXPECTED_HASH = bytes.fromhex(EXPECTED_CONTRACT_SHA256_HEX)
_WRONG_HASH = hashlib.sha256(b"not the real contract").digest()


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


_INSERT_STRATEGY = text("""
    INSERT INTO app.strategies (name, version, description)
    VALUES (:name, :version, 'prospective-cohort integration test')
    ON CONFLICT (name, version) DO UPDATE SET updated_at = now()
    RETURNING id
""")

_INSERT_EPISODE = text("""
    INSERT INTO app.early_momentum_episodes (
        episode_id, strategy_id, contract_sha256, source_exchange, source_native_id,
        exchange, native_market_id, execution_symbol, execution_identity_key,
        source_identity_key, cluster_key, ceiling, features, armed_at, expires_at, status,
        claimed_at, claim_attempts
    ) VALUES (
        :episode_id, :strategy_id, :contract_sha256, :exchange, :native_market_id,
        :exchange, :native_market_id, :execution_symbol, :identity_key,
        :identity_key, :cluster_key, 1.0, '{}'::jsonb, :armed_at, :expires_at, :status,
        :claimed_at, 1
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
    )
""")


async def _ensure_strategy(connection: AsyncConnection) -> int:
    result = await connection.execute(
        _INSERT_STRATEGY, {"name": STRATEGY_NAME, "version": STRATEGY_VERSION}
    )
    return int(result.scalar_one())


async def _insert_episode_and_trade(
    connection: AsyncConnection,
    *,
    strategy_id: int,
    exchange: str,
    native_market_id: str,
    contract_sha256: bytes,
    armed_at: datetime,
) -> None:
    episode_id = str(uuid.uuid4())
    execution_symbol = f"{native_market_id}/USDT:USDT"
    await connection.execute(
        _INSERT_EPISODE,
        {
            "episode_id": episode_id,
            "strategy_id": strategy_id,
            "contract_sha256": contract_sha256,
            "exchange": exchange,
            "native_market_id": native_market_id,
            "execution_symbol": execution_symbol,
            "identity_key": f"ik-{native_market_id}",
            "cluster_key": f"ck-{native_market_id}",
            "armed_at": armed_at,
            "expires_at": armed_at + timedelta(hours=1),
            "status": "opened",
            "claimed_at": armed_at + timedelta(seconds=1),
        },
    )
    entry_at = armed_at + timedelta(minutes=5)
    await connection.execute(
        _INSERT_TRADE,
        {
            "strategy_id": strategy_id,
            "symbol": execution_symbol,
            "exchange": exchange,
            "entry_at": entry_at,
            "exit_at": entry_at + timedelta(hours=1),
            "setup_context": json.dumps({"strategy": "early_momentum_v4", "paper": True}),
            "episode_id": episode_id,
            "idempotency_key": f"{episode_id}:entry:base",
        },
    )


async def _cleanup(connection: AsyncConnection, *, exchange: str) -> None:
    await connection.execute(
        text(
            "DELETE FROM app.trades WHERE episode_id IN "
            "(SELECT episode_id FROM app.early_momentum_episodes WHERE exchange = :exchange)"
        ),
        {"exchange": exchange},
    )
    await connection.execute(
        text("DELETE FROM app.early_momentum_episodes WHERE exchange = :exchange"),
        {"exchange": exchange},
    )


async def test_historical_episode_before_the_prospective_boundary_is_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The colleague's own required regression: a historical (pre-
    prospective-cohort) episode must never enter this cohort's funnel,
    even though it is a genuine early_momentum_v4 episode with the correct
    contract hash -- membership is armed_at vs. the boundary, nothing
    else."""
    engine = await _connect_or_skip()
    exchange = f"{_TEST_EXCHANGE}_{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)
    prospective_start = now - timedelta(days=2)
    cohort_end = now - timedelta(seconds=COHORT_MATURITY_BUFFER_SECONDS + 3600)

    try:
        async with engine.begin() as connection:
            strategy_id = await _ensure_strategy(connection)
            # Historical: armed well BEFORE the prospective boundary.
            await _insert_episode_and_trade(
                connection,
                strategy_id=strategy_id,
                exchange=exchange,
                native_market_id="HISTUSDT",
                contract_sha256=_EXPECTED_HASH,
                armed_at=prospective_start - timedelta(days=1),
            )
            # Prospective: armed AFTER the boundary, before cohort_end.
            await _insert_episode_and_trade(
                connection,
                strategy_id=strategy_id,
                exchange=exchange,
                native_market_id="PROSPUSDT",
                contract_sha256=_EXPECTED_HASH,
                armed_at=prospective_start + timedelta(hours=1),
            )

        registration = prospective_mod.CohortRegistration(
            cohort_key=prospective_mod.PROSPECTIVE_COHORT_KEY,
            strategy_name="early_momentum",
            strategy_version="4",
            contract_sha256=EXPECTED_CONTRACT_SHA256_HEX,
            runtime_policy_sha256=prospective_mod.EXPECTED_RUNTIME_POLICY_SHA256_HEX,
            cohort_started_at=prospective_start,
        )
        monkeypatch.setattr(
            prospective_mod,
            "load_registration",
            AsyncMock(return_value=registration),
        )
        report = await prospective_mod.generate_prospective_cohort_report(
            db_url=TEST_DATABASE_URL,
            cohort_end=cohort_end,
            code_revision="abc123",
            working_tree_dirty=False,
        )

        comparable_market_ids = {
            t.native_market_id for t in report.evidence.funnel.comparable if t.exchange == exchange
        }
        assert "PROSPUSDT" in comparable_market_ids
        assert "HISTUSDT" not in comparable_market_ids
    finally:
        async with engine.begin() as connection:
            await _cleanup(connection, exchange=exchange)
        await engine.dispose()


async def test_contract_hash_mismatch_inside_the_window_maps_to_blocked_not_eligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row-level/cohort-level integrity violation (wrong contract_sha256)
    must never let the report silently reach `eligible_for_live_probe_
    review` -- it maps to `blocked_integrity`, per map_verdict_to_prospective's
    own explicit design (colleague review, 2026-08-25)."""
    engine = await _connect_or_skip()
    exchange = f"{_TEST_EXCHANGE}_{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)
    prospective_start = now - timedelta(days=2)
    cohort_end = now - timedelta(seconds=COHORT_MATURITY_BUFFER_SECONDS + 3600)

    try:
        async with engine.begin() as connection:
            strategy_id = await _ensure_strategy(connection)
            await _insert_episode_and_trade(
                connection,
                strategy_id=strategy_id,
                exchange=exchange,
                native_market_id="WRONGHASHUSDT",
                contract_sha256=_WRONG_HASH,
                armed_at=prospective_start + timedelta(hours=1),
            )

        registration = prospective_mod.CohortRegistration(
            cohort_key=prospective_mod.PROSPECTIVE_COHORT_KEY,
            strategy_name="early_momentum",
            strategy_version="4",
            contract_sha256=EXPECTED_CONTRACT_SHA256_HEX,
            runtime_policy_sha256=prospective_mod.EXPECTED_RUNTIME_POLICY_SHA256_HEX,
            cohort_started_at=prospective_start,
        )
        monkeypatch.setattr(
            prospective_mod,
            "load_registration",
            AsyncMock(return_value=registration),
        )
        report = await prospective_mod.generate_prospective_cohort_report(
            db_url=TEST_DATABASE_URL,
            cohort_end=cohort_end,
            code_revision="abc123",
            working_tree_dirty=False,
        )

        assert report.prospective_verdict == prospective_mod.PROSPECTIVE_VERDICT_BLOCKED
    finally:
        async with engine.begin() as connection:
            await _cleanup(connection, exchange=exchange)
        await engine.dispose()
