from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from schurfer_execution.orders import close_position, place_order
from schurfer_execution.risk import PNL_READY_KEY, TRADING_ENABLED_KEY
from schurfer_execution.routers.orders import OrderRequest


async def _default_get(key: str) -> bytes | None:
    # place_order defaults to fail-closed when these keys are missing, so the
    # happy-path fixture must explicitly report trading enabled and PnL fresh.
    if key == TRADING_ENABLED_KEY:
        return b"1"
    if key == PNL_READY_KEY:
        return b"1"
    return None  # daily pnl = 0, etc.


def _kwargs(**overrides: object) -> dict:  # type: ignore[type-arg]
    rdb = MagicMock()
    rdb.set = AsyncMock(return_value=True)  # lock acquired by default
    rdb.get = AsyncMock(side_effect=_default_get)  # trading enabled, pnl = 0
    rdb.eval = AsyncMock(return_value=1)  # lock released
    rdb.delete = AsyncMock(return_value=1)

    base: dict = {  # type: ignore[type-arg]
        "base": "BEAT",
        "exchange": "bingx",
        "side": "short",
        "size_usd": 100.0,
        "leverage": 2,
        "exchanges": {},
        "rdb": rdb,
        "max_positions": 5,
        "max_position_usd": 500.0,
        "daily_loss_limit_usd": 200.0,
    }
    base.update(overrides)
    return base


class TestOrderRequestValidation:
    def _req(self, **kw: object) -> OrderRequest:
        return OrderRequest(
            base="BEAT",
            exchange="bingx",
            side="short",
            size_usd=100.0,
            **kw,
        )

    def test_lowercase_base_normalized(self) -> None:
        req = OrderRequest(base="beat", exchange="bingx", side="short", size_usd=100.0)
        assert req.base == "BEAT"

    def test_special_chars_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OrderRequest(base="../evil", exchange="bingx", side="short", size_usd=100.0)

    def test_empty_base_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OrderRequest(base="", exchange="bingx", side="short", size_usd=100.0)

    def test_too_long_base_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OrderRequest(base="A" * 21, exchange="bingx", side="short", size_usd=100.0)

    def test_single_char_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OrderRequest(base="A", exchange="bingx", side="short", size_usd=100.0)

    def test_valid_alphanumeric(self) -> None:
        req = OrderRequest(base="1INCH", exchange="bingx", side="short", size_usd=100.0)
        assert req.base == "1INCH"


class TestLockBehavior:
    async def test_denied_when_lock_not_acquired(self) -> None:
        rdb = MagicMock()
        rdb.set = AsyncMock(return_value=None)
        rdb.eval = AsyncMock()

        result = await place_order(**_kwargs(rdb=rdb))

        assert not result["allowed"]
        assert "in progress" in result["reason"]
        rdb.eval.assert_not_called()  # lock never held — must not be released

    async def test_missing_trading_enabled_key_fails_closed(self) -> None:
        """Regression: a missing/evicted trading:enabled key must block trading,
        not default to enabled. Redis eviction or a fresh deploy must never
        silently permit orders."""
        rdb = MagicMock()
        rdb.set = AsyncMock(return_value=True)
        rdb.get = AsyncMock(return_value=None)  # key absent — simulates eviction/fresh deploy
        rdb.eval = AsyncMock(return_value=1)

        with (
            patch("schurfer_execution.orders.fetch_positions", return_value=([], set())),
            patch("schurfer_execution.orders.fetch_margin_balance", return_value=[]),
        ):
            result = await place_order(**_kwargs(rdb=rdb))

        assert not result["allowed"]
        assert "disabled" in result["reason"]

    async def test_lock_released_via_lua_not_delete(self) -> None:
        """Lock release must use compare-and-delete, not unconditional DEL."""
        rdb = MagicMock()
        rdb.set = AsyncMock(return_value=True)
        rdb.get = AsyncMock(return_value=b"0")  # trading disabled → early return
        rdb.eval = AsyncMock(return_value=1)
        rdb.delete = AsyncMock()

        with (
            patch("schurfer_execution.orders.fetch_positions", return_value=([], set())),
            patch("schurfer_execution.orders.fetch_margin_balance", return_value=[]),
        ):
            result = await place_order(**_kwargs(rdb=rdb))

        assert not result["allowed"]
        rdb.eval.assert_called_once()
        rdb.delete.assert_not_called()  # regression: must not use unconditional delete

    async def test_lock_token_matches_between_acquire_and_release(self) -> None:
        """Token stored on acquire must equal token passed to Lua on release."""
        set_token: list[str] = []
        eval_token: list[str] = []

        async def capture_set(key: str, value: str, **kw: object) -> bool:
            if "lock:order" in key:
                set_token.append(value)
            return True

        async def capture_eval(_script: str, _numkeys: int, *args: str) -> int:
            if len(args) >= 2:
                eval_token.append(args[1])  # ARGV[1] = token
            return 1

        rdb = MagicMock()
        rdb.set = capture_set
        rdb.get = AsyncMock(return_value=b"0")  # early exit after lock
        rdb.eval = capture_eval

        with (
            patch("schurfer_execution.orders.fetch_positions", return_value=([], set())),
            patch("schurfer_execution.orders.fetch_margin_balance", return_value=[]),
        ):
            await place_order(**_kwargs(rdb=rdb))

        assert set_token and eval_token
        assert set_token[0] == eval_token[0]

    @patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
    @patch(
        "schurfer_execution.orders.fetch_margin_balance",
        return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
    )
    async def test_lock_release_failure_does_not_mask_order_result(
        self, _mock_bal: MagicMock, _mock_pos: MagicMock
    ) -> None:
        """Redis eval failure in finally must not override a successfully placed order."""
        ex = MagicMock()
        ex.markets = {"BEAT/USDT:USDT": {"contractSize": 1.0}}
        ex.set_leverage = AsyncMock()
        ex.fetch_ticker = AsyncMock(return_value={"last": 1.0})
        ex.amount_to_precision = MagicMock(return_value="100.0")
        ex.price_to_precision = MagicMock(return_value="1.1")
        ex.create_market_order = AsyncMock(return_value={"id": "ord999", "status": "closed"})
        ex.create_stop_market_order = AsyncMock(return_value={"id": "sl-999"})

        rdb = MagicMock()
        rdb.set = AsyncMock(return_value=True)
        rdb.get = AsyncMock(side_effect=_default_get)
        rdb.eval = AsyncMock(side_effect=ConnectionError("redis down"))

        result = await place_order(**_kwargs(exchanges={"bingx": ex}, rdb=rdb))

        assert result["allowed"]
        assert result["order_id"] == "ord999"


@patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
@patch(
    "schurfer_execution.orders.fetch_margin_balance",
    return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
)
async def test_place_order_uses_contract_size_and_precision(
    _mock_bal: MagicMock, _mock_pos: MagicMock
) -> None:
    ex = MagicMock()
    ex.markets = {"BEAT/USDT:USDT": {"contractSize": 10.0}}
    ex.set_leverage = AsyncMock()
    ex.fetch_ticker = AsyncMock(return_value={"last": 1.0})
    ex.amount_to_precision = MagicMock(return_value="10.0")
    ex.price_to_precision = MagicMock(return_value="1.1")
    ex.create_market_order = AsyncMock(return_value={"id": "ord123", "status": "closed"})
    ex.create_stop_market_order = AsyncMock(return_value={"id": "sl-123"})

    result = await place_order(**_kwargs(exchanges={"bingx": ex}))

    # 100 USD / 1.0 price / 10.0 contractSize = 10.0 contracts
    ex.amount_to_precision.assert_called_once_with("BEAT/USDT:USDT", 10.0)
    ex.create_market_order.assert_called_once_with("BEAT/USDT:USDT", "sell", 10.0)
    assert result["allowed"]
    assert result["order_id"] == "ord123"


@patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
@patch(
    "schurfer_execution.orders.fetch_margin_balance",
    return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
)
async def test_place_order_rejects_unknown_symbol(
    _mock_bal: MagicMock, _mock_pos: MagicMock
) -> None:
    ex = MagicMock()
    ex.markets = {}
    ex.load_markets = AsyncMock(return_value=None)

    result = await place_order(**_kwargs(exchanges={"bingx": ex}))

    assert not result["allowed"]
    assert "not found" in result["reason"]


@patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
@patch(
    "schurfer_execution.orders.fetch_margin_balance",
    return_value=[
        {
            "exchange": "bingx",
            "free": 1000.0,
            "used": 0.0,
            "total": 1000.0,
            "tradeable": True,
            "asset": "USDT",
        }
    ],
)
async def test_place_order_rounds_up_to_exchange_minimum(
    _mock_bal: MagicMock, _mock_pos: MagicMock
) -> None:
    # $1 / $5 price / 1.0 contract_size = 0.2 contracts, but min is 1.0
    ex = MagicMock()
    ex.markets = {
        "BEAT/USDT:USDT": {
            "contractSize": 1.0,
            "limits": {"amount": {"min": 1.0}, "cost": {"min": 5.0}},
        }
    }
    ex.set_leverage = AsyncMock()
    ex.fetch_ticker = AsyncMock(return_value={"last": 5.0})
    ex.amount_to_precision = MagicMock(return_value="1.0")
    ex.price_to_precision = MagicMock(return_value="5.5")
    ex.create_market_order = AsyncMock(return_value={"id": "ord-rounded", "status": "closed"})
    ex.create_stop_market_order = AsyncMock(return_value={"id": "sl-rounded"})

    result = await place_order(**_kwargs(size_usd=1.0, exchanges={"bingx": ex}))

    assert result["allowed"]
    assert result["rounded_up"] is True
    ex.amount_to_precision.assert_called_once_with("BEAT/USDT:USDT", 1.0)


@patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
@patch(
    "schurfer_execution.orders.fetch_margin_balance",
    return_value=[
        {
            "exchange": "bingx",
            "free": 1000.0,
            "used": 0.0,
            "total": 1000.0,
            "tradeable": True,
            "asset": "USDT",
        }
    ],
)
async def test_place_order_round_up_exceeds_max_position_blocked(
    _mock_bal: MagicMock, _mock_pos: MagicMock
) -> None:
    # Regression: $1 requested → rounds up to 1 contract at $200 → exceeds MAX_POSITION_USD=150.
    # Risk checks run on original size_usd=$1 and pass; re-check after round-up must block this.
    ex = MagicMock()
    ex.markets = {
        "BEAT/USDT:USDT": {
            "contractSize": 1.0,
            "limits": {"amount": {"min": 1.0}, "cost": {"min": 200.0}},
        }
    }
    ex.set_leverage = AsyncMock()
    ex.fetch_ticker = AsyncMock(return_value={"last": 200.0})
    ex.amount_to_precision = MagicMock(return_value="1.0")

    result = await place_order(
        **_kwargs(size_usd=1.0, max_position_usd=150.0, exchanges={"bingx": ex})
    )

    assert not result["allowed"]
    assert "exceeds limit" in result["reason"]
    ex.create_market_order.assert_not_called()


@patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
@patch(
    "schurfer_execution.orders.fetch_margin_balance",
    return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
)
async def test_place_order_no_round_up_when_above_minimum(
    _mock_bal: MagicMock, _mock_pos: MagicMock
) -> None:
    ex = MagicMock()
    ex.markets = {
        "BEAT/USDT:USDT": {
            "contractSize": 1.0,
            "limits": {"amount": {"min": 1.0}, "cost": {"min": 1.0}},
        }
    }
    ex.set_leverage = AsyncMock()
    ex.fetch_ticker = AsyncMock(return_value={"last": 1.0})
    ex.amount_to_precision = MagicMock(return_value="100.0")
    ex.price_to_precision = MagicMock(return_value="1.1")
    ex.create_market_order = AsyncMock(return_value={"id": "ord-ok", "status": "closed"})
    ex.create_stop_market_order = AsyncMock(return_value={"id": "sl-ok"})

    result = await place_order(**_kwargs(size_usd=100.0, exchanges={"bingx": ex}))

    assert result["allowed"]
    assert result["rounded_up"] is False


@patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
@patch(
    "schurfer_execution.orders.fetch_margin_balance",
    return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
)
async def test_place_order_amount_zero_after_precision_returns_error(
    _mock_bal: MagicMock, _mock_pos: MagicMock
) -> None:
    ex = MagicMock()
    ex.markets = {
        "BEAT/USDT:USDT": {
            "contractSize": 1.0,
            "limits": {"amount": {"min": 0.0}, "cost": {"min": 0.0}},
        }
    }
    ex.set_leverage = AsyncMock()
    ex.fetch_ticker = AsyncMock(return_value={"last": 1000.0})
    ex.amount_to_precision = MagicMock(return_value="0")  # precision rounds tiny amount to 0

    result = await place_order(**_kwargs(size_usd=0.001, exchanges={"bingx": ex}))

    assert not result["allowed"]
    assert "rounds to 0" in result["reason"]


class TestExchangeStopLoss:
    """Exchange-native reduce-only stop-loss placed immediately after entry fill."""

    def _entry_exchange(self) -> MagicMock:
        ex = MagicMock()
        ex.markets = {"BEAT/USDT:USDT": {"contractSize": 1.0}}
        ex.set_leverage = AsyncMock()
        ex.fetch_ticker = AsyncMock(return_value={"last": 1.0})
        ex.amount_to_precision = MagicMock(return_value="100.0")
        ex.price_to_precision = MagicMock(side_effect=lambda _sym, p: str(round(p, 6)))
        ex.create_market_order = AsyncMock(return_value={"id": "entry-1", "status": "closed"})
        return ex

    @patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
    @patch(
        "schurfer_execution.orders.fetch_margin_balance",
        return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
    )
    async def test_stop_loss_placed_above_entry_for_short(
        self, _mock_bal: MagicMock, _mock_pos: MagicMock
    ) -> None:
        ex = self._entry_exchange()
        ex.create_stop_market_order = AsyncMock(return_value={"id": "sl-1"})
        rdb = MagicMock()
        rdb.set = AsyncMock(return_value=True)
        rdb.get = AsyncMock(side_effect=_default_get)
        rdb.eval = AsyncMock(return_value=1)

        result = await place_order(
            **_kwargs(side="short", initial_sl_pct=8.0, exchanges={"bingx": ex}, rdb=rdb)
        )

        assert result["allowed"]
        ex.create_stop_market_order.assert_called_once()
        call_args = ex.create_stop_market_order.call_args
        assert call_args.args[0] == "BEAT/USDT:USDT"
        assert call_args.args[1] == "buy"  # closing side for a short is buy
        trigger_price = call_args.args[3]
        assert trigger_price > 1.0  # short SL triggers above entry price
        assert call_args.kwargs["params"] == {"reduceOnly": True}
        # SL order id persisted for later cancellation on close.
        rdb.set.assert_any_call("position:sl_order_id:bingx:BEAT", "sl-1", ex=86400)

    @patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
    @patch(
        "schurfer_execution.orders.fetch_margin_balance",
        return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
    )
    async def test_stop_loss_failure_force_closes_position(
        self, _mock_bal: MagicMock, _mock_pos: MagicMock
    ) -> None:
        ex = self._entry_exchange()
        ex.create_stop_market_order = AsyncMock(side_effect=RuntimeError("exchange rejected"))
        # Second create_market_order call is the emergency reduce-only close.
        ex.create_market_order = AsyncMock(
            side_effect=[
                {"id": "entry-1", "status": "closed"},
                {"id": "emergency-close-1", "status": "closed"},
            ]
        )

        result = await place_order(**_kwargs(side="short", exchanges={"bingx": ex}))

        assert not result["allowed"]
        assert result["force_closed"] is True
        assert "force-closed" in result["reason"]
        assert ex.create_market_order.call_count == 2
        emergency_call = ex.create_market_order.call_args_list[1]
        assert emergency_call.args == ("BEAT/USDT:USDT", "buy", 100.0)
        assert emergency_call.kwargs["params"] == {"reduceOnly": True}

    @patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
    @patch(
        "schurfer_execution.orders.fetch_margin_balance",
        return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
    )
    async def test_stop_loss_and_emergency_close_both_fail_reports_unprotected(
        self, _mock_bal: MagicMock, _mock_pos: MagicMock
    ) -> None:
        """Regression: if the emergency close ALSO fails, the result must say so —
        not claim force-closed when the position is actually still open and naked."""
        ex = self._entry_exchange()
        ex.create_stop_market_order = AsyncMock(side_effect=RuntimeError("exchange rejected"))
        ex.create_market_order = AsyncMock(
            side_effect=[
                {"id": "entry-1", "status": "closed"},
                RuntimeError("close also rejected"),
            ]
        )

        result = await place_order(**_kwargs(side="short", exchanges={"bingx": ex}))

        assert not result["allowed"]
        assert result["force_closed"] is False
        assert "UNPROTECTED" in result["reason"]


class TestClosePositionCancelsStopLoss:
    async def test_close_position_cancels_resting_sl_order_first(self) -> None:
        ex = MagicMock()
        ex.markets = {"BEAT/USDT:USDT": {}}
        ex.fetch_positions = AsyncMock(
            return_value=[
                {"symbol": "BEAT/USDT:USDT", "contracts": 100.0, "side": "short", "markPrice": 1.1}
            ]
        )
        ex.amount_to_precision = MagicMock(return_value="100.0")
        ex.cancel_order = AsyncMock(return_value={"id": "sl-1", "status": "canceled"})
        ex.create_market_order = AsyncMock(
            return_value={"id": "close-1", "status": "closed", "average": 1.1}
        )

        rdb = MagicMock()
        rdb.set = AsyncMock(return_value=True)
        rdb.get = AsyncMock(return_value=b"sl-1")
        rdb.delete = AsyncMock()
        rdb.eval = AsyncMock(return_value=1)

        result = await close_position(
            exchanges={"bingx": ex}, exchange="bingx", base="BEAT", reason="test", rdb=rdb
        )

        assert result["closed"]
        ex.cancel_order.assert_called_once_with("sl-1", "BEAT/USDT:USDT")
        rdb.delete.assert_any_call("position:sl_order_id:bingx:BEAT")

    async def test_close_position_proceeds_even_if_sl_cancel_fails(self) -> None:
        """SL order may have already filled/expired — cancel failure must not block close."""
        ex = MagicMock()
        ex.markets = {"BEAT/USDT:USDT": {}}
        ex.fetch_positions = AsyncMock(
            return_value=[
                {"symbol": "BEAT/USDT:USDT", "contracts": 100.0, "side": "short", "markPrice": 1.1}
            ]
        )
        ex.amount_to_precision = MagicMock(return_value="100.0")
        ex.cancel_order = AsyncMock(side_effect=RuntimeError("order not found"))
        ex.create_market_order = AsyncMock(
            return_value={"id": "close-1", "status": "closed", "average": 1.1}
        )

        rdb = MagicMock()
        rdb.set = AsyncMock(return_value=True)
        rdb.get = AsyncMock(return_value=b"sl-1")
        rdb.delete = AsyncMock()
        rdb.eval = AsyncMock(return_value=1)

        result = await close_position(
            exchanges={"bingx": ex}, exchange="bingx", base="BEAT", reason="test", rdb=rdb
        )

        assert result["closed"]
