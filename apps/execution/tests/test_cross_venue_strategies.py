import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_execution.execution_intent import (
    DisabledBroker,
    ExecutionResult,
    ExecutionStatus,
    PaperBroker,
    TradingMode,
)
from schurfer_execution.liquidation_cascade import run_liquidation_cascade_scanner
from schurfer_execution.supervisor import WorkerReadinessGate
from schurfer_execution.symbols import ResolvedRoute

# broker=PaperBroker(_open_gate()) is passed explicitly to each scanner
# call below -- these tests exercise the scanner's own candidate-processing logic,
# not execution_intent.resolve_mode's cfg.auto_trade/dry_run/*_mode ceiling (that
# has its own dedicated tests in test_execution_intent.py). A real PaperBroker still
# calls the real schurfer_execution.execution_intent.paper.open_paper, which is what
# gets patched below -- only the patch TARGET moved from liquidation_cascade.paper to
# execution_intent.paper, every asserted kwarg is unchanged.

# early_momentum's own trigger/scanner tests live in test_early_momentum.py
# (the v3 episode-lifecycle rewrite needs a much larger, dedicated mocking
# surface -- episodes.py's create/claim/reap/list_actionable/etc -- that
# doesn't belong mixed into this file's cross-venue routing focus).


def _open_gate() -> WorkerReadinessGate:
    return WorkerReadinessGate(set())


def _market_exchange() -> MagicMock:
    exchange = MagicMock()
    exchange.id = "bybit"
    exchange.markets = {
        "BTC/USDT:USDT": {
            "id": "BTCUSDT",
            "symbol": "BTC/USDT:USDT",
            "base": "BTC",
            "quote": "USDT",
            "settle": "USDT",
            "type": "swap",
            "active": True,
        }
    }
    return exchange


def _route() -> ResolvedRoute:
    return ResolvedRoute(
        source_exchange="binance",
        source_native_id="BTCUSDT",
        source_identity_key="binance:linear_usdt_perpetual:BTCUSDT:1",
        execution_exchange="bybit",
        execution_native_id="BTCUSDT",
        execution_identity_key="bybit:linear_usdt_perpetual:BTCUSDT:2",
        cluster_key="cluster-btc",
    )


def _scanner_db(candidate: dict[str, object]) -> MagicMock:
    cursor = MagicMock()
    cursor.execute = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=[candidate])
    cursor_context = MagicMock()
    cursor_context.__aenter__ = AsyncMock(return_value=cursor)
    cursor_context.__aexit__ = AsyncMock(return_value=None)
    connection = MagicMock()
    connection.__aenter__ = AsyncMock(return_value=connection)
    connection.__aexit__ = AsyncMock(return_value=None)
    connection.cursor.return_value = cursor_context
    return connection


async def test_liquidation_cascade_uses_target_ticker_and_exact_symbol() -> None:
    candidate = {
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "bucket_start": datetime(2026, 8, 24, 7, 0, tzinfo=UTC),
        "close_price": 94.0,
        "price_15m_ago": 100.0,
        "open_interest": 80.0,
        "oi_15m_ago": 100.0,
    }
    connection = _scanner_db(candidate)
    exchange = _market_exchange()
    exchange.fetch_ticker = AsyncMock(return_value={"last": 95.0})
    # A tight, deep book around last_price so the v2 market-quality gate
    # passes -- this test is about ticker-based pricing/symbol resolution,
    # not the quality gate itself (see the market-quality tests below).
    exchange.fetch_order_book = AsyncMock(
        return_value={"bids": [[94.9, 1000.0]], "asks": [[95.1, 1000.0]]}
    )
    cfg = MagicMock(
        db_url="postgresql://x",
        require_market_quality=True,
        max_spread_bps=50.0,
        max_liquidity_impact_bps=50.0,
        liquidity_depth_multiplier=2.0,
    )

    with (
        patch(
            "schurfer_execution.liquidation_cascade.psycopg.AsyncConnection.connect",
            AsyncMock(return_value=connection),
        ),
        patch(
            "schurfer_execution.symbols.resolve_route",
            AsyncMock(return_value=_route()),
        ),
        patch(
            "schurfer_execution.liquidation_cascade.journal.find_open_trade_id",
            AsyncMock(return_value=None),
        ),
        patch(
            "schurfer_execution.execution_intent.paper.open_paper",
            AsyncMock(),
        ) as open_paper,
        patch(
            "schurfer_execution.liquidation_cascade.asyncio.sleep",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await run_liquidation_cascade_scanner(
            {"bybit": exchange}, MagicMock(), cfg, broker=PaperBroker(_open_gate())
        )

    exchange.fetch_ticker.assert_awaited_once_with("BTC/USDT:USDT")
    instrument = open_paper.await_args.kwargs["instrument"]
    assert instrument.symbol == "BTC/USDT:USDT"
    assert open_paper.await_args.kwargs["price"] == 95.0


def _candidate() -> dict[str, object]:
    return {
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "bucket_start": datetime(2026, 8, 24, 7, 0, tzinfo=UTC),
        "close_price": 94.0,
        "price_15m_ago": 100.0,
        "open_interest": 80.0,
        "oi_15m_ago": 100.0,
    }


async def _run_scanner_once(
    exchange: MagicMock, cfg: MagicMock, *, open_id: str | None = None
) -> AsyncMock:
    """Runs run_liquidation_cascade_scanner for exactly one scan iteration
    and returns the mocked paper.open_paper so callers can inspect whether/
    how it was called."""
    connection = _scanner_db(_candidate())
    with (
        patch(
            "schurfer_execution.liquidation_cascade.psycopg.AsyncConnection.connect",
            AsyncMock(return_value=connection),
        ),
        patch(
            "schurfer_execution.symbols.resolve_route",
            AsyncMock(return_value=_route()),
        ),
        patch(
            "schurfer_execution.liquidation_cascade.journal.find_open_trade_id",
            AsyncMock(return_value=open_id),
        ),
        patch(
            "schurfer_execution.execution_intent.paper.open_paper",
            AsyncMock(),
        ) as open_paper,
        patch(
            "schurfer_execution.liquidation_cascade.asyncio.sleep",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await run_liquidation_cascade_scanner(
            {"bybit": exchange}, MagicMock(), cfg, broker=PaperBroker(_open_gate())
        )
    return open_paper


# ---- v2 market-quality capture (colleague review) ----
#
# v1 never measured order-book quality at all -- setup_context never had a
# "market_quality" key, so entry_slippage_bps was permanently None and net
# accounting could never reach "complete" for any liquidation_cascade trade
# (verified against production: 0 of 26 v1 trades ever had one). These lock
# in the v2 fix: a snapshot is taken and attached to every opened trade, and
# the require_market_quality gate (already shared with trader.py/
# early_momentum.py) applies here too.


def _quality_cfg(*, require_market_quality: bool) -> MagicMock:
    return MagicMock(
        db_url="postgresql://x",
        require_market_quality=require_market_quality,
        max_spread_bps=50.0,
        max_liquidity_impact_bps=50.0,
        liquidity_depth_multiplier=2.0,
    )


async def test_liquidation_cascade_attaches_market_quality_and_bumps_version() -> None:
    exchange = _market_exchange()
    exchange.fetch_ticker = AsyncMock(return_value={"last": 95.0})
    exchange.fetch_order_book = AsyncMock(
        return_value={"bids": [[94.9, 1000.0]], "asks": [[95.1, 1000.0]]}
    )
    cfg = _quality_cfg(require_market_quality=True)

    open_paper = await _run_scanner_once(exchange, cfg)

    open_paper.assert_awaited_once()
    setup_context = open_paper.await_args.kwargs["setup_context"]
    assert setup_context["strategy"] == "liquidation_cascade_v2"
    assert setup_context["market_quality"]["allowed"] is True
    assert setup_context["market_quality"]["ask_impact_bps"] is not None
    # entry is still priced off the plain ticker, never a VWAP walk -- see
    # liquidation_cascade.py's own comment on why this must stay unset.
    assert "entry_price_includes_impact" not in setup_context


async def test_liquidation_cascade_market_quality_gate_skips_untradeable_book() -> None:
    exchange = _market_exchange()
    exchange.fetch_ticker = AsyncMock(return_value={"last": 95.0})
    # No book at all -- fetch_order_book raising means capture_snapshot
    # returns status="fetch_failed", quality.allowed=False.
    exchange.fetch_order_book = AsyncMock(side_effect=RuntimeError("no book"))
    cfg = _quality_cfg(require_market_quality=True)

    open_paper = await _run_scanner_once(exchange, cfg)

    open_paper.assert_not_awaited()


async def test_liquidation_cascade_gate_disabled_still_captures_quality() -> None:
    """require_market_quality=False must not open trades blind -- the
    snapshot is still taken and attached, only the reject-on-bad-book
    behavior is skipped (same convention as trader.py/early_momentum.py)."""
    exchange = _market_exchange()
    exchange.fetch_ticker = AsyncMock(return_value={"last": 95.0})
    exchange.fetch_order_book = AsyncMock(side_effect=RuntimeError("no book"))
    cfg = _quality_cfg(require_market_quality=False)

    open_paper = await _run_scanner_once(exchange, cfg)

    open_paper.assert_awaited_once()
    setup_context = open_paper.await_args.kwargs["setup_context"]
    assert setup_context["market_quality"]["allowed"] is False


# ---- TradingMode.DISABLED (colleague review, P1) ----


class _FakeShadowBroker:
    """A broker stub returning SHADOW_RECORDED without going through the real
    ShadowBroker (that write path has its own dedicated tests in
    test_execution_intent.py) -- this file only needs to check the scanner
    reads the status correctly, not as a rejection."""

    mode = TradingMode.SHADOW

    async def open(self, intent: object, *, cfg: object, rdb: object) -> ExecutionResult:
        return ExecutionResult(mode=self.mode, status=ExecutionStatus.SHADOW_RECORDED)


async def test_liquidation_cascade_shadow_recorded_logs_shadow_not_rejected() -> None:
    exchange = _market_exchange()
    exchange.fetch_ticker = AsyncMock(return_value={"last": 95.0})
    exchange.fetch_order_book = AsyncMock(
        return_value={"bids": [[94.9, 1000.0]], "asks": [[95.1, 1000.0]]}
    )
    cfg = _quality_cfg(require_market_quality=True)
    connection = _scanner_db(_candidate())

    with (
        patch(
            "schurfer_execution.liquidation_cascade.psycopg.AsyncConnection.connect",
            AsyncMock(return_value=connection),
        ),
        patch(
            "schurfer_execution.symbols.resolve_route",
            AsyncMock(return_value=_route()),
        ),
        patch(
            "schurfer_execution.liquidation_cascade.journal.find_open_trade_id",
            AsyncMock(return_value=None),
        ),
        patch(
            "schurfer_execution.liquidation_cascade.asyncio.sleep",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
        patch("schurfer_execution.liquidation_cascade.log") as log,
        pytest.raises(asyncio.CancelledError),
    ):
        await run_liquidation_cascade_scanner(
            {"bybit": exchange}, MagicMock(), cfg, broker=_FakeShadowBroker()
        )

    log.info.assert_any_call("liquidation_cascade.shadow_recorded", symbol="BTC/USDT:USDT")
    assert not any(
        call.args and call.args[0] == "liquidation_cascade.broker_rejected"
        for call in log.info.call_args_list
    )


async def test_liquidation_cascade_disabled_never_scans_or_queries_db() -> None:
    """LIQUIDATION_CASCADE_MODE=disabled must stop the scanner outright --
    not just make the eventual broker.open() call reject after a full
    tick's worth of DB/liquidity work already ran for nothing."""
    exchange = _market_exchange()
    cfg = _quality_cfg(require_market_quality=True)

    with (
        patch(
            "schurfer_execution.liquidation_cascade.psycopg.AsyncConnection.connect",
            new_callable=AsyncMock,
        ) as connect,
        patch(
            "schurfer_execution.liquidation_cascade.asyncio.sleep",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
    ):
        # No CancelledError expected -- the disabled branch returns before
        # ever reaching the while/sleep loop at all.
        await run_liquidation_cascade_scanner(
            {"bybit": exchange}, MagicMock(), cfg, broker=DisabledBroker()
        )

    connect.assert_not_called()
