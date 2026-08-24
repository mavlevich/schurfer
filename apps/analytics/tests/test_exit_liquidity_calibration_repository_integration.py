"""Real-Postgres coverage for exit_liquidity_calibration_repository.py.

The test follows the repository integration-test convention in this
package: it skips when the local migrated development Postgres is
unavailable, unless REQUIRE_INTEGRATION_DB=1 is set (CI sets this so a
broken/unprovisioned Postgres service fails the build loudly instead of
this test silently skipping and the run still going green -- mirrors
apps/execution/tests/test_episodes_integration.py's `_connect_or_skip`
convention; colleague review, 2026-08-24 -- our own first version of this
helper omitted the check entirely).

This specifically regression-tests the colleague-review finding
(2026-08-24): `modeled_exit_bps` must come from `setup_context->
market_quality->ask_impact_bps`, not from `Trade.exit_slippage_bps` -- the
column holds two genuinely different things depending on which accounting
path closed the trade (see exit_liquidity_calibration_repository.py's own
module docstring), and reading it directly silently corrupts the
comparison for one of the two cases.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.exit_liquidity_calibration_report import ExitLiquidityFilters
from schurfer_analytics.exit_liquidity_calibration_repository import (
    exit_liquidity_statement,
    map_exit_liquidity_row,
)
from schurfer_journal.models import Trade
from sqlalchemy import delete, insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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
    """The CI-enforcement path itself: with REQUIRE_INTEGRATION_DB=1, an
    unreachable Postgres must fail the test, never silently skip it."""

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


async def test_modeled_exit_bps_reads_setup_context_for_both_legacy_and_fresh_capture_rows() -> (
    None
):
    """Two trades, deliberately constructed so `Trade.exit_slippage_bps` and
    `setup_context.market_quality.ask_impact_bps` DISAGREE on both rows:

    - the "legacy" row mimics an older paper close (before exit-time VWAP
      capture existed): exit_slippage_bps holds a decoy value that must be
      ignored;
    - the "fresh capture" row mimics close_trade() zeroing exit_slippage_bps
      once a real close-time VWAP was captured (see journal.py's own
      close_trade docstring) -- exit_slippage_bps=0 must NOT be read as "the
      model predicted zero cost".

    Both must resolve modeled_exit_bps to their own setup_context value, not
    to whatever sits in the exit_slippage_bps column.
    """
    engine = await _connect_or_skip()
    exit_at = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    # UUID-suffixed, not a fixed name: a crashed prior run or a parallel test
    # run must never collide on the same strategy row, and cleanup below only
    # ever deletes the two trade ids this run itself created (colleague
    # review, 2026-08-24).
    run_id = uuid.uuid4().hex
    strategy_name = f"test_exit_liquidity_calibration_{run_id}"
    strategy_version = "v1"
    # Seeded before the try so the finally block below can always reference
    # them, even if a trade insert itself is what raised.
    strategy_id: int | None = None
    legacy_trade_id: int | None = None
    fresh_capture_trade_id: int | None = None

    try:
        async with engine.begin() as connection:
            strategy_id = (
                await connection.execute(
                    _INSERT_STRATEGY, {"name": strategy_name, "version": strategy_version}
                )
            ).scalar_one()

            legacy_trade_id = (
                await connection.execute(
                    insert(Trade)
                    .values(
                        strategy_id=strategy_id,
                        symbol="COTI/USDT:USDT",
                        exchange="binance",
                        market_type="linear",
                        side="short",
                        size_usd=50,
                        entry_price=1.0,
                        entry_at=exit_at - timedelta(hours=3),
                        exit_price=0.99,
                        exit_at=exit_at,
                        # Decoy: must never be read as the decision-time model.
                        exit_slippage_bps=999.0,
                        status="closed",
                        accounting_version="legacy_price_only_v1",
                        accounting_status="legacy",
                        setup_context={
                            "paper": True,
                            "market_quality": {"ask_impact_bps": 4.25, "bid_impact_bps": 4.25},
                        },
                        notes="max_hold",
                    )
                    .returning(Trade.id)
                )
            ).scalar_one()

            fresh_capture_trade_id = (
                await connection.execute(
                    insert(Trade)
                    .values(
                        strategy_id=strategy_id,
                        symbol="AKE/USDT:USDT",
                        exchange="bybit",
                        market_type="linear",
                        side="short",
                        size_usd=50,
                        entry_price=1.0,
                        entry_at=exit_at - timedelta(hours=3),
                        exit_price=0.98,
                        exit_at=exit_at,
                        # close_trade() zeroed this once a fresh VWAP capture
                        # succeeded -- see this test module's own docstring.
                        exit_slippage_bps=0.0,
                        status="closed",
                        accounting_version="paper_conservative_costs_v1",
                        accounting_status="complete",
                        setup_context={
                            "paper": True,
                            "market_quality": {"ask_impact_bps": 7.5, "bid_impact_bps": 7.5},
                        },
                        notes="initial_sl",
                    )
                    .returning(Trade.id)
                )
            ).scalar_one()

            filters = ExitLiquidityFilters(
                since=exit_at - timedelta(minutes=1), until=exit_at + timedelta(minutes=1)
            )
            result = await connection.execute(exit_liquidity_statement(filters))
            rows = {
                int(mapping["trade_id"]): map_exit_liquidity_row(dict(mapping))
                for mapping in result.mappings()
                if int(mapping["trade_id"]) in (legacy_trade_id, fresh_capture_trade_id)
            }

        assert rows[legacy_trade_id].modeled_exit_bps == pytest.approx(4.25)
        assert rows[fresh_capture_trade_id].modeled_exit_bps == pytest.approx(7.5)
    finally:
        # Deletes exactly the trade ids this run itself created, never
        # "everything on this strategy_id" -- a UUID-suffixed strategy name
        # already prevents collisions, but scoping cleanup to the specific
        # ids is the second, independent guard against over-deleting
        # (colleague review, 2026-08-24). Guards against a partial failure
        # (e.g. the second insert raising) leaving one or both trade ids
        # unset -- only delete the ones that actually got created.
        trade_ids = tuple(
            trade_id
            for trade_id in (legacy_trade_id, fresh_capture_trade_id)
            if trade_id is not None
        )
        async with engine.begin() as connection:
            if trade_ids:
                await connection.execute(delete(Trade).where(Trade.id.in_(trade_ids)))
            if strategy_id is not None:
                await connection.execute(
                    text("DELETE FROM app.strategies WHERE id = :id"), {"id": strategy_id}
                )
        await engine.dispose()
