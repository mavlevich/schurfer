"""Tests for symbols.py — exact CCXT market metadata resolution."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_execution.symbols import resolve_execution_instrument, resolve_route


def _route_db(rows: list[tuple[str, str, str, str]]) -> tuple[MagicMock, MagicMock]:
    cur = MagicMock()
    cur.execute = AsyncMock()
    cur.fetchall = AsyncMock(return_value=rows)
    cursor_context = MagicMock()
    cursor_context.__aenter__ = AsyncMock(return_value=cur)
    cursor_context.__aexit__ = AsyncMock(return_value=None)
    conn = MagicMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    conn.cursor.return_value = cursor_context
    return conn, cur


@pytest.fixture
def mock_bybit() -> MagicMock:
    ex = MagicMock()
    ex.id = "bybit"
    ex.markets = {
        "BTC/USDT:USDT": {
            "id": "BTCUSDT",
            "symbol": "BTC/USDT:USDT",
            "base": "BTC",
            "quote": "USDT",
            "settle": "USDT",
            "type": "swap",
            "active": True,
        },
        "BTC/USDC:USDC": {
            "id": "BTCUSDC",
            "symbol": "BTC/USDC:USDC",
            "base": "BTC",
            "quote": "USDC",
            "settle": "USDC",
            "type": "swap",
            "active": True,
        },
        "BTC/USDT": {
            "id": "BTCUSDT_SPOT",
            "symbol": "BTC/USDT",
            "base": "BTC",
            "quote": "USDT",
            "settle": None,
            "type": "spot",
            "active": True,
        },
        "DOGE/USDT:USDT": {
            "id": "DOGEUSDT",
            "symbol": "DOGE/USDT:USDT",
            "base": "DOGE",
            "quote": "USDT",
            "settle": "USDT",
            "type": "swap",
            "active": True,
        },
        "AMBIG/USDT:USDT": {
            "id": "AMBIGUSDT",
            "symbol": "AMBIG/USDT:USDT",
            "base": "AMBIG",
            "quote": "USDT",
            "settle": "USDT",
            "type": "swap",
            "active": True,
        },
        "AMBIG_ALIAS/USDT:USDT": {
            "id": "AMBIGUSDT",  # Duplicate native ID collision
            "symbol": "AMBIG_ALIAS/USDT:USDT",
            "base": "AMBIG_ALIAS",
            "quote": "USDT",
            "settle": "USDT",
            "type": "swap",
            "active": True,
        },
    }
    return ex


class TestResolveExecutionInstrument:
    def test_resolve_from_native_id(self, mock_bybit: MagicMock) -> None:
        inst = resolve_execution_instrument(mock_bybit, "DOGEUSDT")
        assert inst.symbol == "DOGE/USDT:USDT"
        assert inst.base == "DOGE"
        assert inst.native_market_id == "DOGEUSDT"

    def test_resolve_from_ccxt_symbol(self, mock_bybit: MagicMock) -> None:
        inst = resolve_execution_instrument(mock_bybit, "BTC/USDT:USDT")
        assert inst.symbol == "BTC/USDT:USDT"
        assert inst.base == "BTC"

    def test_resolve_from_base_fallback(self, mock_bybit: MagicMock) -> None:
        inst = resolve_execution_instrument(mock_bybit, "DOGE")
        assert inst.symbol == "DOGE/USDT:USDT"
        assert inst.base == "DOGE"

    def test_spot_market_rejected(self, mock_bybit: MagicMock) -> None:
        with pytest.raises(ValueError, match="Cannot resolve"):
            # BTCUSDT_SPOT is spot, we require swap
            resolve_execution_instrument(mock_bybit, "BTCUSDT_SPOT")

    def test_usdc_settle_rejected_by_default(self, mock_bybit: MagicMock) -> None:
        with pytest.raises(ValueError, match="Cannot resolve"):
            resolve_execution_instrument(mock_bybit, "BTCUSDC")

    def test_ambiguous_native_id_fails_closed(self, mock_bybit: MagicMock) -> None:
        with pytest.raises(ValueError, match="Ambiguous symbol"):
            resolve_execution_instrument(mock_bybit, "AMBIGUSDT")

    def test_missing_market_fails_closed(self, mock_bybit: MagicMock) -> None:
        with pytest.raises(ValueError, match="Cannot resolve"):
            resolve_execution_instrument(mock_bybit, "XYZUSDT")

    def test_markets_not_loaded_fails(self) -> None:
        ex = MagicMock()
        ex.markets = None
        with pytest.raises(RuntimeError, match="Markets not loaded"):
            resolve_execution_instrument(ex, "BTCUSDT")


class TestResolveRoute:
    async def test_returns_unique_confirmed_native_route(self) -> None:
        conn, cur = _route_db(
            [
                (
                    "binance:linear_usdt_perpetual:BTCUSDT:1",
                    "BTCUSDT",
                    "bybit:linear_usdt_perpetual:BTCUSDT:2",
                    "cluster-btc",
                )
            ]
        )
        with patch(
            "schurfer_execution.symbols.psycopg.AsyncConnection.connect",
            AsyncMock(return_value=conn),
        ):
            route = await resolve_route(
                "postgresql://x",
                "binance",
                "BTCUSDT",
                "bybit",
            )

        assert route is not None
        assert route.execution_native_id == "BTCUSDT"
        assert route.execution_identity_key.startswith("bybit:")
        assert not hasattr(route, "execution_symbol")
        query, params = cur.execute.await_args.args
        assert "a.match_status = 'confirmed'" in query
        assert "b.match_status = 'confirmed'" in query
        assert params == ("binance", "BTCUSDT", "bybit")

    @pytest.mark.parametrize("rows", [[], [("a", "ONE", "b", "c"), ("a", "TWO", "d", "c")]])
    async def test_missing_or_ambiguous_route_fails_closed(
        self,
        rows: list[tuple[str, str, str, str]],
    ) -> None:
        conn, _cur = _route_db(rows)
        with patch(
            "schurfer_execution.symbols.psycopg.AsyncConnection.connect",
            AsyncMock(return_value=conn),
        ):
            route = await resolve_route(
                "postgresql://x",
                "binance",
                "BTCUSDT",
                "bybit",
            )

        assert route is None

    async def test_database_failure_is_not_misreported_as_missing_route(self) -> None:
        with (
            patch(
                "schurfer_execution.symbols.psycopg.AsyncConnection.connect",
                AsyncMock(side_effect=RuntimeError("db unavailable")),
            ),
            pytest.raises(RuntimeError, match="db unavailable"),
        ):
            await resolve_route("postgresql://x", "binance", "BTCUSDT", "bybit")
