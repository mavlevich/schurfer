"""Real-Postgres coverage for exit_liquidity_net_economics_repository.py.

Follows the same `_connect_or_skip`/`REQUIRE_INTEGRATION_DB` convention as
`test_exit_liquidity_calibration_repository_integration.py` (colleague
review, 2026-08-24). This specifically proves the query/join actually
works against real tables: the `Strategy` join resolves `strategy_name`/
`strategy_version`, and `TradeExitLiquidityObservation.mid`/`ask_impact_bps`
-- the two fields this dataset reads that the sibling calibration query
does not -- come through the mapper correctly.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics.exit_liquidity_calibration_report import ExitLiquidityFilters
from schurfer_analytics.exit_liquidity_net_economics_repository import (
    map_net_economics_row,
    net_economics_statement,
)
from schurfer_journal.models import Trade, TradeExitLiquidityObservation
from sqlalchemy import delete, insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"


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


_INSERT_STRATEGY = text("""
    INSERT INTO app.strategies (name, version)
    VALUES (:name, :version)
    RETURNING id
""")

# The report's frozen allowlist (ALLOWED_STRATEGY_IDENTITIES) is a single
# literal identity, not something a UUID-suffixed test name can satisfy --
# so this fixture row is idempotently get-or-created (ON CONFLICT DO
# NOTHING, then SELECT) rather than inserted fresh and torn down per test,
# the same way a migration-seeded reference row would be treated. Trades
# pointing at it ARE torn down per test.
_GET_OR_CREATE_PUMP_SHORT_STRATEGY = text("""
    INSERT INTO app.strategies (name, version)
    VALUES ('pump_short', '1')
    ON CONFLICT (name, version) DO NOTHING
""")
_SELECT_PUMP_SHORT_STRATEGY_ID = text("""
    SELECT id FROM app.strategies WHERE name = 'pump_short' AND version = '1'
""")


async def _get_or_create_pump_short_strategy_id(connection: AsyncConnection) -> int:
    await connection.execute(_GET_OR_CREATE_PUMP_SHORT_STRATEGY)
    return int((await connection.execute(_SELECT_PUMP_SHORT_STRATEGY_ID)).scalar_one())


async def test_statement_joins_strategy_and_reads_mid_ask_impact_and_ask_vwap() -> None:
    engine = await _connect_or_skip()
    exit_at = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    trade_id: int | None = None

    try:
        async with engine.begin() as connection:
            strategy_id = await _get_or_create_pump_short_strategy_id(connection)

            trade_id = (
                await connection.execute(
                    insert(Trade)
                    .values(
                        strategy_id=strategy_id,
                        symbol="COTI/USDT:USDT",
                        exchange="binance",
                        market_type="linear",
                        side="short",
                        size_usd=50,
                        leverage=5,
                        entry_price=1.0,
                        entry_at=exit_at - timedelta(hours=3),
                        entry_slippage_bps=2.0,
                        exit_price=0.99,
                        exit_at=exit_at,
                        exit_slippage_bps=0.0,
                        fees_usd=0.05,
                        funding_usd=0.01,
                        gross_pnl_usd=0.5,
                        net_pnl_usd=0.20,
                        status="closed",
                        accounting_version="paper_conservative_costs_v1",
                        accounting_status="complete",
                        setup_context={
                            "paper": True,
                            "market_quality": {"ask_impact_bps": 5.0, "bid_impact_bps": 5.0},
                        },
                        notes="max_hold age=180min",
                    )
                    .returning(Trade.id)
                )
            ).scalar_one()

            await connection.execute(
                insert(TradeExitLiquidityObservation).values(
                    trade_id=trade_id,
                    observed_at=exit_at - timedelta(seconds=1),
                    exchange="binance",
                    symbol="COTI/USDT:USDT",
                    status="sampled",
                    requested_notional_usd=50,
                    filled_notional_usd=50,
                    mid=0.995,
                    spread_bps=4.0,
                    ask_vwap=0.9944,
                    ask_impact_bps=6.0,
                    latency_ms=100,
                )
            )

            filters = ExitLiquidityFilters(
                since=exit_at - timedelta(minutes=1), until=exit_at + timedelta(minutes=1)
            )
            result = await connection.execute(net_economics_statement(filters))
            rows = {
                int(mapping["trade_id"]): map_net_economics_row(dict(mapping))
                for mapping in result.mappings()
                if int(mapping["trade_id"]) == trade_id
            }

        row = rows[trade_id]
        assert row.strategy_name == "pump_short"
        assert row.strategy_version == "1"
        assert row.observed_mid == pytest.approx(0.995)
        assert row.observed_exit_bps == pytest.approx(6.0)
        assert row.observed_ask_vwap == pytest.approx(0.9944)
        assert row.entry_slippage_bps == pytest.approx(2.0)
        assert row.recorded_net_pnl_usd == pytest.approx(0.20)
    finally:
        async with engine.begin() as connection:
            if trade_id is not None:
                await connection.execute(delete(Trade).where(Trade.id == trade_id))
        await engine.dispose()


async def test_statement_excludes_a_non_allowlisted_strategy() -> None:
    """Regression (colleague review, 2026-08-25): the query used to select
    every closed paper short regardless of strategy -- a historical
    early_momentum short, a differently-versioned pump_short variant, or a
    future short strategy would all silently blend into one aggregate."""
    engine = await _connect_or_skip()
    exit_at = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    run_id = uuid.uuid4().hex
    strategy_name = f"test_not_allowlisted_{run_id}"  # Strategy.name is String(64)
    strategy_id: int | None = None
    trade_id: int | None = None

    try:
        async with engine.begin() as connection:
            strategy_id = (
                await connection.execute(_INSERT_STRATEGY, {"name": strategy_name, "version": "1"})
            ).scalar_one()

            trade_id = (
                await connection.execute(
                    insert(Trade)
                    .values(
                        strategy_id=strategy_id,
                        symbol="COTI/USDT:USDT",
                        exchange="binance",
                        market_type="linear",
                        side="short",
                        size_usd=50,
                        leverage=5,
                        entry_price=1.0,
                        entry_at=exit_at - timedelta(hours=3),
                        entry_slippage_bps=2.0,
                        exit_price=0.99,
                        exit_at=exit_at,
                        exit_slippage_bps=0.0,
                        fees_usd=0.05,
                        funding_usd=0.01,
                        gross_pnl_usd=0.5,
                        net_pnl_usd=0.20,
                        status="closed",
                        accounting_version="paper_conservative_costs_v1",
                        accounting_status="complete",
                        setup_context={
                            "paper": True,
                            "market_quality": {"ask_impact_bps": 5.0, "bid_impact_bps": 5.0},
                        },
                        notes="max_hold age=180min",
                    )
                    .returning(Trade.id)
                )
            ).scalar_one()

            filters = ExitLiquidityFilters(
                since=exit_at - timedelta(minutes=1), until=exit_at + timedelta(minutes=1)
            )
            result = await connection.execute(net_economics_statement(filters))
            matched_ids = {int(mapping["trade_id"]) for mapping in result.mappings()}

        assert trade_id not in matched_ids
    finally:
        async with engine.begin() as connection:
            if trade_id is not None:
                await connection.execute(delete(Trade).where(Trade.id == trade_id))
            if strategy_id is not None:
                await connection.execute(
                    text("DELETE FROM app.strategies WHERE id = :id"), {"id": strategy_id}
                )
        await engine.dispose()
