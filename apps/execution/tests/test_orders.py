from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import ccxt
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from schurfer_execution import exit as exit_module
from schurfer_execution import order_attempts
from schurfer_execution.orders import close_position, place_order
from schurfer_execution.risk import PNL_READY_KEY, TRADING_ENABLED_KEY
from schurfer_execution.routers.orders import OrderRequest, post_order
from schurfer_execution.supervisor import WorkerReadinessGate


async def _default_get(key: str) -> bytes | None:
    # place_order defaults to fail-closed when these keys are missing, so the
    # happy-path fixture must explicitly report trading enabled and PnL fresh.
    if key == TRADING_ENABLED_KEY:
        return b"1"
    if key == PNL_READY_KEY:
        return b"1"
    return None  # daily pnl = 0, etc.


def _open_gate() -> WorkerReadinessGate:
    return WorkerReadinessGate(set())


class _GenerationChangingGate:
    def __init__(self) -> None:
        self.calls = 0

    def is_open(self) -> tuple[bool, int]:
        self.calls += 1
        return (True, 1) if self.calls == 1 else (False, 2)

    def get_reasons(self) -> list[str]:
        return ["critical worker failed"]


def _kwargs(**overrides: object) -> dict:  # type: ignore[type-arg]
    rdb = MagicMock()
    rdb.set = AsyncMock(return_value=True)  # lock acquired by default
    rdb.get = AsyncMock(side_effect=_default_get)  # trading enabled, pnl = 0
    rdb.eval = AsyncMock(return_value=1)  # lock released
    rdb.delete = AsyncMock(return_value=1)

    base: dict = {  # type: ignore[type-arg]
        "base": "BEAT",
        "symbol": "BEAT/USDT:USDT",
        "exchange": "bingx",
        "side": "short",
        "size_usd": 100.0,
        "leverage": 2,
        "exchanges": {},
        "rdb": rdb,
        "max_positions": 5,
        "max_position_usd": 500.0,
        "daily_loss_limit_usd": 200.0,
        "worker_gate": _open_gate(),
    }
    base.update(overrides)
    return base


class TestOrderRequestValidation:
    def _req(self, **kw: object) -> OrderRequest:
        return OrderRequest(
            symbol="BEAT",
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


class TestManualEntryModeCeiling:
    """Regression for ENG-020: POST /order goes straight to orders.place_order,
    which submits a real exchange order through the authenticated trading
    clients. The endpoint used to ignore the global AUTO_TRADE/DRY_RUN ceiling
    entirely, so a paper-mode -- or fully disabled -- deployment that still
    holds live API keys would place a real order on request. The ceiling is
    read through execution_intent.mode_ceiling, the same function resolve_mode
    uses, so the endpoint's gate and the strategy mode ladder cannot drift.
    """

    @staticmethod
    def _cfg(*, auto_trade: bool, dry_run: bool) -> MagicMock:
        return MagicMock(
            auto_trade=auto_trade,
            dry_run=dry_run,
            max_positions=5,
            max_position_usd=500.0,
            daily_loss_limit_usd=200.0,
            liquidation_buffer_pct=20.0,
            db_url=None,
        )

    @staticmethod
    def _request(cfg: MagicMock) -> tuple[SimpleNamespace, MagicMock]:
        ex = MagicMock()
        ex.markets = {
            "BEAT/USDT:USDT": {
                "id": "BEATUSDT",
                "type": "swap",
                "contractSize": 1.0,
                "limits": {},
            }
        }
        ex.set_leverage = AsyncMock()
        ex.fetch_ticker = AsyncMock(return_value={"last": 1.0})
        ex.amount_to_precision = MagicMock(side_effect=lambda _symbol, amount: str(amount))
        ex.price_to_precision = MagicMock(side_effect=lambda _symbol, price: str(price))
        ex.create_market_order = AsyncMock(return_value={"id": "order-1"})
        ex.create_stop_market_order = AsyncMock(return_value={"id": "sl-1"})

        rdb = MagicMock()
        rdb.set = AsyncMock(return_value=True)
        rdb.get = AsyncMock(side_effect=_default_get)
        rdb.eval = AsyncMock(return_value=1)
        rdb.delete = AsyncMock(return_value=1)

        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    cfg=cfg,
                    trading_exchanges={"bingx": ex},
                    rdb=rdb,
                    worker_gate=_open_gate(),
                )
            )
        )
        return request, ex

    @pytest.mark.parametrize(
        ("auto_trade", "dry_run", "expected_ceiling"),
        [(False, True, "paper"), (False, False, "disabled")],
    )
    async def test_manual_entry_below_live_ceiling_never_reaches_the_exchange(
        self, auto_trade: bool, dry_run: bool, expected_ceiling: str
    ) -> None:
        cfg = self._cfg(auto_trade=auto_trade, dry_run=dry_run)
        request, ex = self._request(cfg)
        req = OrderRequest(base="BEAT", exchange="bingx", side="short", size_usd=100.0)

        with (
            patch(
                "schurfer_execution.routers.orders.symbols.resolve_execution_instrument",
                return_value=SimpleNamespace(symbol="BEAT/USDT:USDT"),
            ),
            patch(
                "schurfer_execution.orders.fetch_positions",
                AsyncMock(return_value=([], set())),
            ),
            patch(
                "schurfer_execution.orders.fetch_margin_balance",
                AsyncMock(
                    return_value=[
                        {"exchange": "bingx", "asset": "USDT", "free": 10_000.0, "tradeable": True}
                    ]
                ),
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await post_order(req, request)  # type: ignore[arg-type]

        assert exc_info.value.status_code == 409
        assert expected_ceiling in str(exc_info.value.detail)
        ex.set_leverage.assert_not_awaited()
        ex.create_market_order.assert_not_awaited()

    async def test_manual_entry_is_allowed_under_a_live_ceiling(self) -> None:
        """The gate must reject only what the ceiling actually forbids: with
        AUTO_TRADE=true the existing manual path still runs unchanged."""
        cfg = self._cfg(auto_trade=True, dry_run=False)
        request, _ex = self._request(cfg)
        req = OrderRequest(base="BEAT", exchange="bingx", side="short", size_usd=100.0)

        with (
            patch("schurfer_execution.routers.orders.symbols.resolve_execution_instrument"),
            patch(
                "schurfer_execution.routers.orders.place_order",
                AsyncMock(return_value={"allowed": True}),
            ) as mock_place,
        ):
            result = await post_order(req, request)  # type: ignore[arg-type]

        assert result == {"allowed": True}
        mock_place.assert_awaited_once()


class TestManualOrderEndpointJournalsToo:
    """Regression: a manually-triggered order via POST /order used to place
    a real exchange order but never journal it at all -- cfg simply wasn't
    threaded through to place_order, even though it was already sitting on
    request.app.state. Now that place_order's own journal.complete_open
    does the write, this endpoint must actually pass cfg through, and must
    give the write an honest, non-pump_short identity."""

    async def test_post_order_passes_cfg_and_manual_setup_context(self) -> None:
        cfg = MagicMock(
            # Explicit: the endpoint now refuses manual entries below a live
            # ceiling (TestManualEntryModeCeiling), so this journalling
            # regression must state the mode it is exercising rather than
            # relying on MagicMock's truthy default attributes.
            auto_trade=True,
            dry_run=False,
            max_positions=5,
            max_position_usd=500.0,
            daily_loss_limit_usd=200.0,
            liquidation_buffer_pct=20.0,
            db_url="postgresql://x",
        )
        ex = MagicMock()
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    cfg=cfg,
                    trading_exchanges={"bingx": ex},
                    rdb=MagicMock(),
                    worker_gate=_open_gate(),
                )
            )
        )
        req = OrderRequest(base="BEAT", exchange="bingx", side="short", size_usd=100.0)

        with (
            patch("schurfer_execution.routers.orders.symbols.resolve_execution_instrument"),
            patch(
                "schurfer_execution.routers.orders.place_order",
                AsyncMock(return_value={"allowed": True}),
            ) as mock_place,
        ):
            await post_order(req, request)  # type: ignore[arg-type]

        kw = mock_place.call_args.kwargs
        assert kw["cfg"] is cfg
        assert kw["setup_context"] == {"strategy_name": "manual", "strategy_version": "1"}


class TestLockBehavior:
    async def test_closed_worker_gate_rejects_before_redis_lock(self) -> None:
        rdb = MagicMock()
        rdb.set = AsyncMock(return_value=True)
        gate = WorkerReadinessGate({"critical"})

        result = await place_order(**_kwargs(rdb=rdb, worker_gate=gate))

        assert not result["allowed"]
        assert "readiness gate closed" in result["reason"]
        rdb.set.assert_not_awaited()

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
        assert "missing" in result["reason"]

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
        ex.create_market_order = AsyncMock(
            return_value={"id": "ord999", "status": "closed", "average": 1.0}
        )
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
    ex.create_market_order.assert_called_once()
    call = ex.create_market_order.call_args
    assert call.args == ("BEAT/USDT:USDT", "sell", 10.0)
    assert isinstance(call.kwargs["params"]["clientOrderId"], str)
    assert call.kwargs["params"]["clientOrderId"]
    assert result["allowed"]
    assert result["order_id"] == "ord123"


@patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
@patch(
    "schurfer_execution.orders.fetch_margin_balance",
    return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
)
async def test_gate_generation_change_rejects_before_exchange_order(
    _mock_bal: MagicMock, _mock_pos: MagicMock
) -> None:
    ex = MagicMock()
    ex.markets = {"BEAT/USDT:USDT": {"contractSize": 1.0}}
    ex.set_leverage = AsyncMock()
    ex.fetch_ticker = AsyncMock(return_value={"last": 1.0})
    ex.amount_to_precision = MagicMock(return_value="100.0")
    ex.price_to_precision = MagicMock(return_value="1.1")
    ex.create_market_order = AsyncMock()

    result = await place_order(
        **_kwargs(exchanges={"bingx": ex}, worker_gate=_GenerationChangingGate())
    )

    assert not result["allowed"]
    assert "generation changed" in result["reason"]
    ex.create_market_order.assert_not_awaited()


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
    ex.create_market_order = AsyncMock(
        return_value={"id": "ord-rounded", "status": "closed", "average": 5.0}
    )
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
async def test_place_order_round_up_exceeds_liquidity_checked_notional_blocked(
    _mock_bal: MagicMock, _mock_pos: MagicMock
) -> None:
    ex = MagicMock()
    ex.markets = {
        "BEAT/USDT:USDT": {
            "contractSize": 1.0,
            "limits": {"amount": {"min": 1.0}, "cost": {"min": 150.0}},
        }
    }
    ex.set_leverage = AsyncMock()
    ex.fetch_ticker = AsyncMock(return_value={"last": 150.0})
    ex.amount_to_precision = MagicMock(return_value="1.0")

    result = await place_order(
        **_kwargs(
            size_usd=50.0,
            max_position_usd=500.0,
            liquidity_checked_usd=100.0,
            exchanges={"bingx": ex},
        )
    )

    assert not result["allowed"]
    assert result["reason"] == (
        "actual position $150.00 exceeds liquidity-checked notional $100.00"
    )
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
    ex.create_market_order = AsyncMock(
        return_value={"id": "ord-ok", "status": "closed", "average": 1.0}
    )
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
        ex.create_market_order = AsyncMock(
            return_value={"id": "entry-1", "status": "closed", "average": 1.0}
        )
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
            **_kwargs(
                side="short",
                exit_params={**exit_module.exit_params(None), "initial_sl_pct": 8.0},
                exchanges={"bingx": ex},
                rdb=rdb,
            )
        )

        assert result["allowed"]
        ex.create_stop_market_order.assert_called_once()
        call_args = ex.create_stop_market_order.call_args
        assert call_args.args[0] == "BEAT/USDT:USDT"
        assert call_args.args[1] == "buy"  # closing side for a short is buy
        trigger_price = call_args.args[3]
        assert trigger_price > 1.0  # short SL triggers above entry price
        assert call_args.kwargs["params"]["reduceOnly"] is True
        assert isinstance(call_args.kwargs["params"]["clientOrderId"], str)
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


class TestFillEvidenceGovernsTheLedger:
    """Regression for ENG-022 / audit C-3. Two related claims the ledger used
    to make without evidence: a partial entry was journalled at the full
    requested notional, and an order the exchange said filled nothing was
    journalled as a real position at whatever average the payload carried."""

    @staticmethod
    def _exchange(*, order: dict[str, object]) -> MagicMock:
        ex = MagicMock()
        ex.markets = {"BEAT/USDT:USDT": {"contractSize": 1.0}}
        ex.set_leverage = AsyncMock()
        ex.fetch_ticker = AsyncMock(return_value={"last": 1.0})
        ex.amount_to_precision = MagicMock(return_value="100.0")
        ex.price_to_precision = MagicMock(return_value="1.1")
        ex.create_market_order = AsyncMock(return_value=order)
        ex.create_stop_market_order = AsyncMock(return_value={"id": "sl-1"})
        ex.cancel_order = AsyncMock()
        ex.fetch_order = AsyncMock(return_value=order)
        ex.has = {}
        return ex

    @staticmethod
    def _rdb() -> MagicMock:
        stored: dict[str, bytes] = {}

        async def _get(key: str) -> bytes | None:
            if key == TRADING_ENABLED_KEY:
                return b"1"
            if key == PNL_READY_KEY:
                return b"1"
            return stored.get(key)

        async def _set(key: str, value: object, **_kw: object) -> bool:
            stored[key] = str(value).encode()
            return True

        async def _delete(key: str) -> int:
            return 1 if stored.pop(key, None) is not None else 0

        rdb = MagicMock()
        rdb.get = AsyncMock(side_effect=_get)
        rdb.set = AsyncMock(side_effect=_set)
        rdb.delete = AsyncMock(side_effect=_delete)
        rdb.eval = AsyncMock(return_value=1)
        return rdb

    @patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
    @patch(
        "schurfer_execution.orders.fetch_margin_balance",
        return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
    )
    async def test_partial_entry_journals_the_filled_notional(
        self, _mock_bal: MagicMock, _mock_pos: MagicMock
    ) -> None:
        """40 of 100 contracts filled at 1.5 is $60 of exposure, not the $100
        that was requested."""
        cfg = MagicMock(db_url="postgresql://x")
        ex = self._exchange(
            order={"id": "entry-1", "status": "closed", "average": 1.5, "filled": 40.0}
        )

        with (
            patch(
                "schurfer_execution.orders.journal.complete_open", AsyncMock(return_value=77)
            ) as mock_complete,
            patch(
                "schurfer_execution.orders.order_attempts.create_attempt",
                AsyncMock(return_value=1),
            ),
            patch("schurfer_execution.orders.order_attempts.mark_accepted", AsyncMock()),
            patch("schurfer_execution.orders.order_attempts.mark_completed", AsyncMock()),
        ):
            result = await place_order(**_kwargs(exchanges={"bingx": ex}, cfg=cfg))

        assert result["allowed"]
        assert result["fill_status"] == "partial"
        assert mock_complete.call_args.kwargs["size_usd"] == 60.0
        assert result["size_usd"] == 60.0
        assert result["requested_size_usd"] == 100.0
        # The protective stop covers what actually filled, not the request.
        assert ex.create_stop_market_order.call_args.args[2] == 40.0

    @patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
    @patch(
        "schurfer_execution.orders.fetch_margin_balance",
        return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
    )
    async def test_zero_fill_opens_no_position_and_leaves_no_resting_stop(
        self, _mock_bal: MagicMock, _mock_pos: MagicMock
    ) -> None:
        cfg = MagicMock(db_url="postgresql://x")
        ex = self._exchange(
            order={"id": "entry-1", "status": "canceled", "average": 1.5, "filled": 0.0}
        )
        # What a real venue does with a reduceOnly stop when no position
        # exists. If the no-fill check ever moves back below the protective
        # stop, this rejection drops the call into the stop-failure fail-safe,
        # which returns early with a false "UNPROTECTED position" reason and
        # never marks the durable attempt failed (colleague review).
        ex.create_stop_market_order = AsyncMock(
            side_effect=RuntimeError("reduceOnly rejected: no position")
        )
        rdb = self._rdb()

        with (
            patch(
                "schurfer_execution.orders.journal.complete_open", AsyncMock(return_value=77)
            ) as mock_complete,
            patch(
                "schurfer_execution.orders.order_attempts.create_attempt",
                AsyncMock(return_value=1),
            ),
            patch("schurfer_execution.orders.order_attempts.mark_accepted", AsyncMock()),
            # Patched so that a regression which lets this path reach the
            # completion write cannot open a real database connection.
            patch("schurfer_execution.orders.order_attempts.mark_completed", AsyncMock()),
            patch(
                "schurfer_execution.orders.order_attempts.mark_failed", AsyncMock()
            ) as mock_failed,
        ):
            result = await place_order(**_kwargs(rdb=rdb, exchanges={"bingx": ex}, cfg=cfg))

        assert not result["allowed"]
        assert result["fill_status"] == "none"
        assert "no fill" in result["reason"]
        # No ledger entry for a position that does not exist.
        mock_complete.assert_not_awaited()
        # Nothing to protect, so no stop is attempted at all: no rejected
        # reduceOnly order, no fail-safe force-close, no stray resting stop for
        # the next real position on this instrument to inherit.
        ex.create_stop_market_order.assert_not_awaited()
        ex.cancel_order.assert_not_awaited()
        # The durable attempt row is closed out rather than left pending.
        mock_failed.assert_awaited_once()

    async def test_close_that_fills_nothing_is_not_reported_closed(self) -> None:
        """The caller commits a journal close and drops exit tracking off
        `closed`, so claiming it for an order that executed nothing would
        abandon a position that is still on the exchange."""
        ex = MagicMock()
        ex.markets = {"BEAT/USDT:USDT": {"contractSize": 1.0}}
        ex.fetch_positions = AsyncMock(
            return_value=[
                {"symbol": "BEAT/USDT:USDT", "contracts": 10.0, "side": "short", "markPrice": 1.0}
            ]
        )
        ex.amount_to_precision = MagicMock(return_value="10.0")
        ex.cancel_order = AsyncMock()
        ex.create_market_order = AsyncMock(
            return_value={"id": "close-1", "status": "canceled", "average": 1.0, "filled": 0.0}
        )
        ex.fetch_order = AsyncMock(
            return_value={"id": "close-1", "status": "canceled", "average": 1.0, "filled": 0.0}
        )
        ex.has = {}
        rdb = self._rdb()

        result = await close_position(
            exchanges={"bingx": ex},
            exchange="bingx",
            base="BEAT",
            symbol="BEAT/USDT:USDT",
            reason="manual",
            rdb=rdb,
        )

        assert result["closed"] is False
        assert result["fill_status"] == "none"
        assert "still open" in result["reason"]


class TestCompletesJournalOnFill:
    """place_order's happy path now completes journal.complete_open itself
    (see journal.py's own docstring on why), instead of leaving it to
    trader.py a few awaits later -- these lock that in independently of
    trader.py's own tests."""

    def _confirmed_exchange(self) -> MagicMock:
        ex = MagicMock()
        ex.markets = {"BEAT/USDT:USDT": {"contractSize": 1.0}}
        ex.set_leverage = AsyncMock()
        ex.fetch_ticker = AsyncMock(return_value={"last": 1.0})
        ex.amount_to_precision = MagicMock(return_value="100.0")
        ex.price_to_precision = MagicMock(return_value="1.1")
        ex.create_market_order = AsyncMock(
            return_value={"id": "entry-1", "status": "closed", "average": 1.5}
        )
        ex.create_stop_market_order = AsyncMock(return_value={"id": "sl-1"})
        return ex

    @patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
    @patch(
        "schurfer_execution.orders.fetch_margin_balance",
        return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
    )
    async def test_completes_journal_and_returns_trade_id(
        self, _mock_bal: MagicMock, _mock_pos: MagicMock
    ) -> None:
        cfg = MagicMock(db_url="postgresql://x")
        with (
            patch(
                "schurfer_execution.orders.journal.complete_open",
                AsyncMock(return_value=77),
            ) as mock_complete,
            patch(
                "schurfer_execution.orders.order_attempts.create_attempt",
                AsyncMock(return_value=1),
            ),
            patch("schurfer_execution.orders.order_attempts.mark_accepted", AsyncMock()),
            patch("schurfer_execution.orders.order_attempts.mark_completed", AsyncMock()),
        ):
            result = await place_order(
                **_kwargs(exchanges={"bingx": self._confirmed_exchange()}, cfg=cfg)
            )

        assert result["allowed"]
        assert result["trade_id"] == 77
        mock_complete.assert_awaited_once()
        kw = mock_complete.call_args.kwargs
        assert kw["symbol"] == "BEAT/USDT:USDT"
        assert kw["exchange"] == "bingx"
        assert kw["base"] == "BEAT"
        assert kw["side"] == "short"
        assert kw["entry_price"] == 1.5
        assert kw["size_usd"] == 100.0
        assert mock_complete.call_args.args[0] == "postgresql://x"

    @patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
    @patch(
        "schurfer_execution.orders.fetch_margin_balance",
        return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
    )
    async def test_creates_incident_when_journal_write_fails(
        self, _mock_bal: MagicMock, _mock_pos: MagicMock
    ) -> None:
        cfg = MagicMock(db_url="postgresql://x")
        with (
            patch(
                "schurfer_execution.orders.journal.complete_open",
                AsyncMock(return_value=None),
            ),
            patch(
                "schurfer_execution.orders.incidents.create_incident",
                AsyncMock(return_value=99),
            ) as mock_incident,
            patch(
                "schurfer_execution.orders.order_attempts.create_attempt",
                AsyncMock(return_value=1),
            ),
            patch("schurfer_execution.orders.order_attempts.mark_accepted", AsyncMock()),
            patch("schurfer_execution.orders.order_attempts.mark_completed", AsyncMock()),
        ):
            result = await place_order(
                **_kwargs(exchanges={"bingx": self._confirmed_exchange()}, cfg=cfg)
            )

        # The exchange fill is real and protected -- a failed journal write
        # must never turn that into "not allowed".
        assert result["allowed"]
        assert result["trade_id"] is None
        mock_incident.assert_awaited_once()
        assert mock_incident.call_args.kwargs["operation"] == "open"

    @patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
    @patch(
        "schurfer_execution.orders.fetch_margin_balance",
        return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
    )
    async def test_no_incident_when_no_db_url_configured(
        self, _mock_bal: MagicMock, _mock_pos: MagicMock
    ) -> None:
        """No cfg at all (e.g. an operator posting via /order with no journal
        configured) must not try to create an incident it has no db_url to
        write to -- journal.complete_open itself already treats db_url=None
        as "operational Redis keys only, no journal row", not a failure."""
        with (
            patch(
                "schurfer_execution.orders.journal.complete_open",
                AsyncMock(return_value=None),
            ) as mock_complete,
            patch(
                "schurfer_execution.orders.incidents.create_incident", AsyncMock()
            ) as mock_incident,
        ):
            result = await place_order(**_kwargs(exchanges={"bingx": self._confirmed_exchange()}))

        assert result["allowed"]
        assert mock_complete.call_args.args[0] is None
        mock_incident.assert_not_awaited()


class TestEmergencyStopDuringPreFlight:
    """Regression for ENG-020 / audit H-4: trading:enabled was read once, at
    the start of place_order, before several seconds of network pre-flight
    (positions, balances, load_markets, set_leverage, fetch_ticker). An
    operator pressing POST /stop inside that window still saw the entry go
    out. The flag is now re-read immediately before submission, where the
    readiness-gate generation is already rechecked.
    """

    def _confirmed_exchange(self) -> MagicMock:
        ex = MagicMock()
        ex.markets = {"BEAT/USDT:USDT": {"contractSize": 1.0}}
        ex.set_leverage = AsyncMock()
        ex.fetch_ticker = AsyncMock(return_value={"last": 1.0})
        ex.amount_to_precision = MagicMock(return_value="100.0")
        ex.price_to_precision = MagicMock(return_value="1.1")
        ex.create_market_order = AsyncMock(
            return_value={"id": "entry-1", "status": "closed", "average": 1.5}
        )
        ex.create_stop_market_order = AsyncMock(return_value={"id": "sl-1"})
        return ex

    @staticmethod
    def _rdb(*, stop_after_reads: int | None = None, stopped: dict[str, bool] | None = None):
        """A Redis double whose kill switch flips mid-flight: either after a
        given number of admission reads, or when `stopped` is set by the
        exchange call itself."""
        reads = {"trading": 0}

        async def _get(key: str) -> bytes | None:
            if key == TRADING_ENABLED_KEY:
                reads["trading"] += 1
                if stopped is not None and stopped.get("value"):
                    return b"0"
                if stop_after_reads is not None and reads["trading"] > stop_after_reads:
                    return b"0"
                return b"1"
            if key == PNL_READY_KEY:
                return b"1"
            return None

        rdb = MagicMock()
        rdb.set = AsyncMock(return_value=True)
        rdb.get = AsyncMock(side_effect=_get)
        rdb.eval = AsyncMock(return_value=1)
        rdb.delete = AsyncMock(return_value=1)
        return rdb

    @patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
    @patch(
        "schurfer_execution.orders.fetch_margin_balance",
        return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
    )
    async def test_stop_pressed_during_preflight_blocks_submission(
        self, _mock_bal: MagicMock, _mock_pos: MagicMock
    ) -> None:
        cfg = MagicMock(db_url="postgresql://x")
        ex = self._confirmed_exchange()
        rdb = self._rdb(stop_after_reads=1)

        with (
            patch(
                "schurfer_execution.orders.order_attempts.create_attempt",
                AsyncMock(return_value=7),
            ),
            patch(
                "schurfer_execution.orders.order_attempts.mark_failed", AsyncMock()
            ) as mock_failed,
        ):
            result = await place_order(**_kwargs(rdb=rdb, exchanges={"bingx": ex}, cfg=cfg))

        assert not result["allowed"]
        assert "emergency stop" in result["reason"]
        assert "pre-flight" in result["reason"]
        ex.create_market_order.assert_not_called()
        # The durable attempt row was already written before the exchange call,
        # so it must be closed out as failed rather than left pending forever.
        mock_failed.assert_awaited_once()

    @patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
    @patch(
        "schurfer_execution.orders.fetch_margin_balance",
        return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
    )
    async def test_stop_after_submission_still_protects_the_position(
        self, _mock_bal: MagicMock, _mock_pos: MagicMock
    ) -> None:
        """The entry is the only thing an emergency stop may cancel. Once a
        real position exists, its protective stop must still be placed --
        blocking that would leave the position unprotected, which is worse
        than the entry the operator was trying to prevent."""
        stopped = {"value": False}
        ex = self._confirmed_exchange()

        async def _submit(*_args: object, **_kwargs: object) -> dict[str, object]:
            stopped["value"] = True  # operator presses stop as the order lands
            return {"id": "entry-1", "status": "closed", "average": 1.5}

        ex.create_market_order = AsyncMock(side_effect=_submit)
        rdb = self._rdb(stopped=stopped)

        result = await place_order(**_kwargs(rdb=rdb, exchanges={"bingx": ex}))

        assert result["allowed"]
        ex.create_market_order.assert_awaited_once()
        ex.create_stop_market_order.assert_awaited_once()


class TestPreFlightDurability:
    """order_attempts.create_attempt is written BEFORE the exchange is ever
    called -- if that write itself fails while a database IS configured,
    place_order must refuse the order outright rather than risk a real
    position with zero durable trace anywhere (colleague review, P0)."""

    def _confirmed_exchange(self) -> MagicMock:
        ex = MagicMock()
        ex.markets = {"BEAT/USDT:USDT": {"contractSize": 1.0}}
        ex.set_leverage = AsyncMock()
        ex.fetch_ticker = AsyncMock(return_value={"last": 1.0})
        ex.amount_to_precision = MagicMock(return_value="100.0")
        ex.price_to_precision = MagicMock(return_value="1.1")
        ex.create_market_order = AsyncMock(
            return_value={"id": "entry-1", "status": "closed", "average": 1.5}
        )
        ex.create_stop_market_order = AsyncMock(return_value={"id": "sl-1"})
        return ex

    @patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
    @patch(
        "schurfer_execution.orders.fetch_margin_balance",
        return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
    )
    async def test_refuses_order_when_preflight_record_cannot_be_written(
        self, _mock_bal: MagicMock, _mock_pos: MagicMock
    ) -> None:
        cfg = MagicMock(db_url="postgresql://x")
        ex = self._confirmed_exchange()
        with patch(
            "schurfer_execution.orders.order_attempts.create_attempt",
            AsyncMock(return_value=None),
        ):
            result = await place_order(**_kwargs(exchanges={"bingx": ex}, cfg=cfg))

        assert not result["allowed"]
        assert "durably record" in result["reason"]
        ex.create_market_order.assert_not_called()

    @patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
    @patch(
        "schurfer_execution.orders.fetch_margin_balance",
        return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
    )
    async def test_concurrent_reservation_at_capacity_never_submits_exchange_order(
        self, _mock_bal: MagicMock, _mock_pos: MagicMock
    ) -> None:
        cfg = MagicMock(db_url="postgresql://x")
        ex = self._confirmed_exchange()
        with patch(
            "schurfer_execution.orders.order_attempts.create_attempt",
            AsyncMock(
                return_value=order_attempts.PortfolioCapacityReached(
                    occupied_slots=5,
                    max_positions=5,
                )
            ),
        ):
            result = await place_order(**_kwargs(exchanges={"bingx": ex}, cfg=cfg))

        assert not result["allowed"]
        assert result["reason"] == "max positions reached (5/5)"
        ex.create_market_order.assert_not_called()

    @patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
    @patch(
        "schurfer_execution.orders.fetch_margin_balance",
        return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
    )
    async def test_no_preflight_gate_when_no_db_configured(
        self, _mock_bal: MagicMock, _mock_pos: MagicMock
    ) -> None:
        """cfg=None entirely (no database configured for this call at all)
        must behave exactly as before this PR -- the gate only applies when
        a database IS configured but unreachable."""
        ex = self._confirmed_exchange()
        with patch(
            "schurfer_execution.orders.order_attempts.create_attempt", AsyncMock()
        ) as mock_create:
            result = await place_order(**_kwargs(exchanges={"bingx": ex}))

        assert result["allowed"]
        mock_create.assert_not_awaited()
        ex.create_market_order.assert_called_once()

    @patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
    @patch(
        "schurfer_execution.orders.fetch_margin_balance",
        return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
    )
    async def test_marks_attempt_accepted_with_real_order_id(
        self, _mock_bal: MagicMock, _mock_pos: MagicMock
    ) -> None:
        cfg = MagicMock(db_url="postgresql://x")
        ex = self._confirmed_exchange()
        with (
            patch(
                "schurfer_execution.orders.order_attempts.create_attempt",
                AsyncMock(return_value=5),
            ),
            patch(
                "schurfer_execution.orders.order_attempts.mark_accepted", AsyncMock()
            ) as mock_accepted,
            patch("schurfer_execution.orders.order_attempts.mark_completed", AsyncMock()),
            patch("schurfer_execution.orders.journal.complete_open", AsyncMock(return_value=1)),
        ):
            result = await place_order(**_kwargs(exchanges={"bingx": ex}, cfg=cfg))

        assert result["allowed"]
        mock_accepted.assert_awaited_once_with("postgresql://x", 5, order_id="entry-1")

    @patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
    @patch(
        "schurfer_execution.orders.fetch_margin_balance",
        return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
    )
    async def test_ambiguous_exchange_error_marks_unknown_and_closes_gate(
        self, _mock_bal: MagicMock, _mock_pos: MagicMock
    ) -> None:
        cfg = MagicMock(db_url="postgresql://x")
        ex = self._confirmed_exchange()
        ex.create_market_order = AsyncMock(side_effect=RuntimeError("exchange down"))
        gate = _open_gate()
        with (
            patch(
                "schurfer_execution.orders.order_attempts.create_attempt",
                AsyncMock(return_value=5),
            ),
            patch(
                "schurfer_execution.orders.order_attempts.mark_submission_unknown",
                AsyncMock(),
            ) as mock_unknown,
        ):
            result = await place_order(
                **_kwargs(exchanges={"bingx": ex}, cfg=cfg, worker_gate=gate)
            )

        assert not result["allowed"]
        assert "submission_unknown" in result["reason"]
        mock_unknown.assert_awaited_once_with("postgresql://x", 5, error="exchange down")
        assert not gate.is_open()[0]
        assert gate.get_reasons() == ["submission_unknown"]

    @patch("schurfer_execution.orders.fetch_positions", return_value=([], set()))
    @patch(
        "schurfer_execution.orders.fetch_margin_balance",
        return_value=[{"exchange": "bingx", "free": 1000.0, "used": 0.0, "total": 1000.0}],
    )
    async def test_marks_attempt_completed_with_trade_id_and_filled_amount(
        self, _mock_bal: MagicMock, _mock_pos: MagicMock
    ) -> None:
        cfg = MagicMock(db_url="postgresql://x")
        ex = self._confirmed_exchange()
        with (
            patch(
                "schurfer_execution.orders.order_attempts.create_attempt",
                AsyncMock(return_value=5),
            ),
            patch("schurfer_execution.orders.order_attempts.mark_accepted", AsyncMock()),
            patch(
                "schurfer_execution.orders.order_attempts.mark_completed", AsyncMock()
            ) as mock_completed,
            patch("schurfer_execution.orders.journal.complete_open", AsyncMock(return_value=77)),
        ):
            result = await place_order(**_kwargs(exchanges={"bingx": ex}, cfg=cfg))

        assert result["allowed"]
        mock_completed.assert_awaited_once()
        kw = mock_completed.call_args.kwargs
        assert kw["trade_id"] == 77


class TestClosePositionCancelsStopLoss:
    async def test_close_position_cancels_resting_sl_order_after_terminal_fill(self) -> None:
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
            return_value={"id": "close-1", "status": "closed", "average": 1.1, "filled": 100.0}
        )

        rdb = MagicMock()
        rdb.set = AsyncMock(return_value=True)
        rdb.get = AsyncMock(return_value=b"sl-1")
        rdb.delete = AsyncMock()
        rdb.eval = AsyncMock(return_value=1)

        result = await close_position(
            exchanges={"bingx": ex},
            exchange="bingx",
            base="BEAT",
            symbol="BEAT/USDT:USDT",
            reason="test",
            rdb=rdb,
        )

        assert result["closed"]
        assert ex.create_market_order.await_count == 1
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
            return_value={"id": "close-1", "status": "closed", "average": 1.1, "filled": 100.0}
        )

        rdb = MagicMock()
        rdb.set = AsyncMock(return_value=True)
        rdb.get = AsyncMock(return_value=b"sl-1")
        rdb.delete = AsyncMock()
        rdb.eval = AsyncMock(return_value=1)

        result = await close_position(
            exchanges={"bingx": ex},
            exchange="bingx",
            base="BEAT",
            symbol="BEAT/USDT:USDT",
            reason="test",
            rdb=rdb,
        )

        assert result["closed"]
        # A failed terminal cleanup remains discoverable for reconciliation.
        assert call("position:sl_order_id:bingx:BEAT") not in rdb.delete.await_args_list

    async def test_partial_close_retains_stop_and_position_tracking(self) -> None:
        ex = MagicMock()
        ex.markets = {"BEAT/USDT:USDT": {}}
        ex.fetch_positions = AsyncMock(
            return_value=[
                {"symbol": "BEAT/USDT:USDT", "contracts": 100.0, "side": "short", "markPrice": 1.1}
            ]
        )
        ex.amount_to_precision = MagicMock(return_value="100.0")
        ex.cancel_order = AsyncMock()
        ex.create_market_order = AsyncMock(
            return_value={"id": "close-partial", "status": "closed", "average": 1.1, "filled": 40.0}
        )
        rdb = MagicMock()
        rdb.set = AsyncMock(return_value=True)
        rdb.get = AsyncMock(return_value=b"sl-1")
        rdb.delete = AsyncMock()
        rdb.eval = AsyncMock(return_value=1)

        result = await close_position(
            exchanges={"bingx": ex},
            exchange="bingx",
            base="BEAT",
            symbol="BEAT/USDT:USDT",
            reason="test",
            rdb=rdb,
        )

        assert result["closed"] is False
        assert result["fill_status"] == "partial"
        assert result["filled_amount"] == 40.0
        assert result["remaining_amount"] == 60.0
        assert result["protected"] is True
        ex.cancel_order.assert_not_awaited()
        assert call("position:opened_at:bingx:BEAT") not in rdb.delete.await_args_list

    async def test_active_partial_close_stays_recoverable_and_is_not_persisted_early(self) -> None:
        ex = MagicMock()
        ex.markets = {"BEAT/USDT:USDT": {"contractSize": 1.0}}
        ex.fetch_positions = AsyncMock(
            return_value=[
                {"symbol": "BEAT/USDT:USDT", "contracts": 100.0, "side": "short", "markPrice": 1.1}
            ]
        )
        ex.amount_to_precision = MagicMock(return_value="100.0")
        ex.create_market_order = AsyncMock(
            return_value={"id": "close-active", "status": "open", "average": 1.1, "filled": 40.0}
        )
        ex.cancel_order = AsyncMock()
        rdb = MagicMock()
        rdb.set = AsyncMock(return_value=True)
        rdb.get = AsyncMock(return_value=b"sl-1")
        rdb.delete = AsyncMock()
        rdb.eval = AsyncMock(return_value=1)
        cfg = MagicMock(db_url="postgresql://x")

        with (
            patch("schurfer_execution.orders._tracked_trade_id", AsyncMock(return_value=42)),
            patch(
                "schurfer_execution.orders.order_attempts.create_close_attempt",
                AsyncMock(return_value=7),
            ),
            patch("schurfer_execution.orders.order_attempts.mark_accepted", AsyncMock()),
            patch("schurfer_execution.orders.order_attempts.mark_partial", AsyncMock()) as partial,
            patch("schurfer_execution.orders.journal.record_close_fill", AsyncMock()) as record,
        ):
            result = await close_position(
                exchanges={"bingx": ex},
                exchange="bingx",
                base="BEAT",
                symbol="BEAT/USDT:USDT",
                reason="test",
                rdb=rdb,
                cfg=cfg,
            )

        assert result["closed"] is False
        assert "remains active" in result["reason"]
        record.assert_not_awaited()
        partial.assert_not_awaited()
        ex.cancel_order.assert_not_awaited()

    async def test_close_intent_is_durable_before_exchange_submission(self) -> None:
        ex = MagicMock()
        ex.markets = {"BEAT/USDT:USDT": {"id": "BEAT-USDT", "type": "swap", "contractSize": 1.0}}
        ex.fetch_positions = AsyncMock(
            return_value=[
                {"symbol": "BEAT/USDT:USDT", "contracts": 10.0, "side": "short", "markPrice": 1.0}
            ]
        )
        ex.amount_to_precision = MagicMock(return_value="10.0")
        events: list[str] = []

        async def _create_attempt(*_args: object, **_kwargs: object) -> int:
            events.append("durable")
            return 7

        async def _submit(*_args: object, **_kwargs: object) -> dict[str, object]:
            events.append("exchange")
            return {"id": "close-1", "average": 1.0, "filled": 10.0}

        ex.create_market_order = AsyncMock(side_effect=_submit)
        ex.cancel_order = AsyncMock()
        rdb = MagicMock()
        rdb.set = AsyncMock(return_value=True)
        rdb.get = AsyncMock(return_value=None)
        rdb.delete = AsyncMock()
        rdb.eval = AsyncMock(return_value=1)
        cfg = MagicMock(db_url="postgresql://x")

        with (
            patch("schurfer_execution.orders._tracked_trade_id", AsyncMock(return_value=42)),
            patch(
                "schurfer_execution.orders.order_attempts.create_close_attempt",
                AsyncMock(side_effect=_create_attempt),
            ) as mock_create_attempt,
            patch("schurfer_execution.orders.order_attempts.mark_accepted", AsyncMock()),
            patch("schurfer_execution.orders.order_attempts.mark_completed", AsyncMock()),
            patch(
                "schurfer_execution.orders.journal.record_close_fill",
                AsyncMock(return_value=1.0),
            ),
        ):
            result = await close_position(
                exchanges={"bingx": ex},
                exchange="bingx",
                base="BEAT",
                symbol="BEAT/USDT:USDT",
                reason="test",
                rdb=rdb,
                cfg=cfg,
            )

        assert result["closed"] is True
        assert events == ["durable", "exchange"]
        mock_create_attempt.assert_awaited_once()
        params = ex.create_market_order.call_args.kwargs["params"]
        assert params["reduceOnly"] is True
        assert params["clientOrderId"]

    async def test_ambiguous_close_submission_keeps_stop_and_marks_attempt(self) -> None:
        ex = MagicMock()
        ex.markets = {"BEAT/USDT:USDT": {"contractSize": 1.0}}
        ex.fetch_positions = AsyncMock(
            return_value=[
                {"symbol": "BEAT/USDT:USDT", "contracts": 10.0, "side": "short", "markPrice": 1.0}
            ]
        )
        ex.amount_to_precision = MagicMock(return_value="10.0")
        ex.create_market_order = AsyncMock(side_effect=ccxt.NetworkError("timeout"))
        ex.cancel_order = AsyncMock()
        rdb = MagicMock()
        rdb.set = AsyncMock(return_value=True)
        rdb.get = AsyncMock(return_value=b"sl-1")
        rdb.delete = AsyncMock()
        rdb.eval = AsyncMock(return_value=1)
        cfg = MagicMock(db_url="postgresql://x")

        with (
            patch("schurfer_execution.orders._tracked_trade_id", AsyncMock(return_value=42)),
            patch(
                "schurfer_execution.orders.order_attempts.create_close_attempt",
                AsyncMock(return_value=7),
            ),
            patch(
                "schurfer_execution.orders.order_attempts.mark_submission_unknown",
                AsyncMock(),
            ) as mock_unknown,
        ):
            result = await close_position(
                exchanges={"bingx": ex},
                exchange="bingx",
                base="BEAT",
                symbol="BEAT/USDT:USDT",
                reason="test",
                rdb=rdb,
                cfg=cfg,
            )

        assert result["closed"] is False
        assert result["protected"] is True
        assert "submission_unknown" in result["reason"]
        mock_unknown.assert_awaited_once()
        ex.cancel_order.assert_not_awaited()
