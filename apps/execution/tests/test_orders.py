from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from schurfer_execution.orders import place_order
from schurfer_execution.routers.orders import OrderRequest


def _kwargs(**overrides: object) -> dict:  # type: ignore[type-arg]
    rdb = MagicMock()
    rdb.set = AsyncMock(return_value=True)  # lock acquired by default
    rdb.get = AsyncMock(return_value=None)  # trading enabled, pnl = 0
    rdb.eval = AsyncMock(return_value=1)  # lock released

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
            **kw,  # type: ignore[arg-type]
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

    async def test_lock_released_via_lua_not_delete(self) -> None:
        """Lock release must use compare-and-delete, not unconditional DEL."""
        rdb = MagicMock()
        rdb.set = AsyncMock(return_value=True)
        rdb.get = AsyncMock(return_value=b"0")  # trading disabled → early return
        rdb.eval = AsyncMock(return_value=1)
        rdb.delete = AsyncMock()

        with (
            patch("schurfer_execution.orders.fetch_positions", return_value=([], set())),
            patch("schurfer_execution.orders.fetch_balance", return_value=[]),
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
            patch("schurfer_execution.orders.fetch_balance", return_value=[]),
        ):
            await place_order(**_kwargs(rdb=rdb))

        assert set_token and eval_token
        assert set_token[0] == eval_token[0]

    @patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
    @patch(
        "schurfer_execution.orders.fetch_balance",
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
        ex.create_market_order = AsyncMock(return_value={"id": "ord999", "status": "closed"})

        rdb = MagicMock()
        rdb.set = AsyncMock(return_value=True)
        rdb.get = AsyncMock(return_value=None)
        rdb.eval = AsyncMock(side_effect=ConnectionError("redis down"))

        result = await place_order(**_kwargs(exchanges={"bingx": ex}, rdb=rdb))

        assert result["allowed"]
        assert result["order_id"] == "ord999"


@patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
@patch(
    "schurfer_execution.orders.fetch_balance",
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
    ex.create_market_order = AsyncMock(return_value={"id": "ord123", "status": "closed"})

    result = await place_order(**_kwargs(exchanges={"bingx": ex}))

    # 100 USD / 1.0 price / 10.0 contractSize = 10.0 contracts
    ex.amount_to_precision.assert_called_once_with("BEAT/USDT:USDT", 10.0)
    ex.create_market_order.assert_called_once_with("BEAT/USDT:USDT", "sell", 10.0)
    assert result["allowed"]
    assert result["order_id"] == "ord123"


@patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
@patch(
    "schurfer_execution.orders.fetch_balance",
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
