from unittest.mock import AsyncMock, MagicMock, patch

from schurfer_execution.account import fetch_balance, fetch_margin_balance, fetch_positions
from schurfer_execution.config import Config
from schurfer_execution.routers.account import CloseBody, manual_close_position


def _mock_exchange_positions(
    positions: list[dict[str, object]], *, fail: bool = False
) -> MagicMock:
    ex = MagicMock()
    if fail:
        ex.fetch_positions = AsyncMock(side_effect=Exception("network error"))
    else:
        ex.fetch_positions = AsyncMock(return_value=positions)
    return ex


def _mock_exchange_balance(
    futures_usdt: float = 100.0,
    spot_usdt: float = 0.0,
    fund_usdt: float | None = None,
    fund_assets: dict[str, float] | None = None,
    default_type: str = "swap",
) -> MagicMock:
    ex = MagicMock()
    ex.options = {"defaultType": default_type}

    def _usdt_bal(amount: float) -> dict:  # type: ignore[type-arg]
        return {"USDT": {"free": amount, "used": 0.0, "total": amount}}

    def _multi_bal(assets: dict[str, float]) -> dict:  # type: ignore[type-arg]
        return {sym: {"free": amt, "used": 0.0, "total": amt} for sym, amt in assets.items()}

    async def _fetch_balance(params: dict | None = None) -> dict:  # type: ignore[type-arg]
        t = (params or {}).get("type", default_type)
        if t == "spot":
            return _usdt_bal(spot_usdt)
        if t == "fund":
            if fund_usdt is None and fund_assets is None:
                raise Exception("funding account not available")
            if fund_assets is not None:
                return _multi_bal(fund_assets)
            return _usdt_bal(fund_usdt or 0.0)
        return _usdt_bal(futures_usdt)

    ex.fetch_balance = _fetch_balance
    ex.fetch_ticker = AsyncMock(side_effect=Exception("ticker not available in test"))
    return ex


def _open_position(symbol: str = "BEAT/USDT:USDT") -> dict:  # type: ignore[type-arg]
    return {
        "symbol": symbol,
        "contracts": 1.0,
        "side": "short",
        "notional": 200.0,
        "entryPrice": 1.0,
        "unrealizedPnl": 0.0,
        "leverage": 2.0,
        "liquidationPrice": None,
    }


# --- fetch_balance ---


async def test_fetch_balance_returns_futures_and_spot_rows() -> None:
    ex = _mock_exchange_balance(futures_usdt=100.0, spot_usdt=50.0)
    rows = await fetch_balance({"mexc": ex})

    assert len(rows) == 2
    futures = next(r for r in rows if r["wallet"] == "swap")
    spot = next(r for r in rows if r["wallet"] == "spot")
    assert futures["tradeable"] is True
    assert futures["total"] == 100.0
    assert spot["tradeable"] is False
    assert spot["total"] == 50.0


async def test_fetch_balance_spot_exchange_returns_one_row() -> None:
    ex = _mock_exchange_balance(spot_usdt=200.0, default_type="spot")
    rows = await fetch_balance({"binance_spot": ex})

    assert len(rows) == 1
    assert rows[0]["wallet"] == "spot"
    assert rows[0]["tradeable"] is True


async def test_fetch_balance_includes_fund_row() -> None:
    ex = _mock_exchange_balance(futures_usdt=100.0, spot_usdt=50.0, fund_usdt=200.0)
    rows = await fetch_balance({"bybit": ex})

    assert len(rows) == 3
    fund = next(r for r in rows if r["wallet"] == "fund")
    assert fund["tradeable"] is False
    assert fund["total"] == 200.0
    assert fund["asset"] == "USDT"


async def test_fetch_balance_fund_multi_asset() -> None:
    ex = _mock_exchange_balance(
        futures_usdt=100.0,
        fund_assets={"BTC": 0.05, "ETH": 1.2, "USDT": 50.0},
    )
    rows = await fetch_balance({"bybit": ex})

    fund_rows = [r for r in rows if r["wallet"] == "fund"]
    assert len(fund_rows) == 3
    assets = {r["asset"] for r in fund_rows}
    assert assets == {"BTC", "ETH", "USDT"}
    btc = next(r for r in fund_rows if r["asset"] == "BTC")
    assert btc["tradeable"] is False
    assert btc["total"] == 0.05


async def test_fetch_balance_spot_error_does_not_drop_futures_row() -> None:
    ex = MagicMock()
    ex.options = {"defaultType": "swap"}

    call_count = 0

    async def _fetch_balance(params: dict | None = None) -> dict:  # type: ignore[type-arg]
        nonlocal call_count
        call_count += 1
        t = (params or {}).get("type", "swap")
        if t in ("spot", "fund"):
            raise Exception(f"{t} not available")
        return {"USDT": {"free": 80.0, "used": 0.0, "total": 80.0}}

    ex.fetch_balance = _fetch_balance
    rows = await fetch_balance({"bybit": ex})

    assert len(rows) == 1
    assert rows[0]["wallet"] == "swap"
    assert rows[0]["tradeable"] is True


# --- fetch_margin_balance ---


async def test_fetch_balance_all_wallets_fail_returns_error_row() -> None:
    ex = MagicMock()
    ex.options = {"defaultType": "swap"}
    ex.fetch_balance = AsyncMock(side_effect=Exception("auth error"))
    ex.fetch_ticker = AsyncMock(side_effect=Exception("not in test"))
    rows = await fetch_balance({"bybit": ex})

    assert len(rows) == 1
    assert rows[0]["exchange"] == "bybit"
    assert rows[0]["error"] is True
    assert rows[0]["total"] == 0.0


async def test_fetch_balance_successful_row_has_no_error() -> None:
    ex = _mock_exchange_balance(futures_usdt=100.0)
    rows = await fetch_balance({"mexc": ex})

    assert all(not r.get("error") for r in rows)


# --- fetch_margin_balance ---


async def test_fetch_margin_balance_returns_only_usdt_tradeable() -> None:
    ex = _mock_exchange_balance(futures_usdt=500.0, spot_usdt=200.0)
    rows = await fetch_margin_balance({"bingx": ex}, "bingx")

    assert len(rows) == 1
    assert rows[0]["asset"] == "USDT"
    assert rows[0]["tradeable"] is True
    assert rows[0]["total"] == 500.0


async def test_fetch_margin_balance_unknown_exchange_returns_empty() -> None:
    rows = await fetch_margin_balance({}, "bingx")
    assert rows == []


async def test_fetch_margin_balance_exchange_error_returns_empty() -> None:
    ex = MagicMock()
    ex.options = {"defaultType": "swap"}
    ex.fetch_balance = AsyncMock(side_effect=Exception("network error"))
    rows = await fetch_margin_balance({"bingx": ex}, "bingx")
    assert rows == []


# --- fetch_positions ---


async def test_fetch_positions_returns_failed_on_error() -> None:
    ex = _mock_exchange_positions([], fail=True)
    positions, failed = await fetch_positions({"bingx": ex})
    assert positions == []
    assert "bingx" in failed


async def test_fetch_positions_ok_exchange_not_in_failed() -> None:
    ex = _mock_exchange_positions([_open_position()])
    positions, failed = await fetch_positions({"bybit": ex})
    assert len(positions) == 1
    assert "bybit" not in failed


async def test_fetch_positions_partial_failure() -> None:
    good = _mock_exchange_positions([_open_position()])
    bad = _mock_exchange_positions([], fail=True)

    positions, failed = await fetch_positions({"bybit": good, "bingx": bad})

    assert len(positions) == 1
    assert positions[0]["exchange"] == "bybit"
    assert "bingx" in failed
    assert "bybit" not in failed


async def test_fetch_positions_skips_zero_contracts() -> None:
    pos = {**_open_position(), "contracts": 0.0}
    ex = _mock_exchange_positions([pos])
    positions, failed = await fetch_positions({"bybit": ex})
    assert positions == []
    assert not failed


# --- manual_close_position ---


def _close_cfg(*, db_url: str | None = None) -> Config:
    cfg = object.__new__(Config)
    cfg.db_url = db_url
    cfg.max_positions = 5
    cfg.daily_loss_limit_usd = 200.0
    cfg.telegram_bot_token = None
    cfg.telegram_chat_id = None
    return cfg


def _close_rdb(**redis_data: bytes | None) -> MagicMock:
    rdb = MagicMock()
    rdb.delete = AsyncMock()

    async def _get(key: str) -> bytes | None:
        return redis_data.get(key)

    rdb.get = AsyncMock(side_effect=_get)
    return rdb


def _close_request(cfg: Config, rdb: MagicMock) -> MagicMock:
    req = MagicMock()
    req.app.state.cfg = cfg
    req.app.state.rdb = rdb
    exchange = MagicMock()
    exchange.id = "bybit"
    exchange.markets = {
        "BEAT/USDT:USDT": {
            "id": "BEATUSDT",
            "symbol": "BEAT/USDT:USDT",
            "base": "BEAT",
            "quote": "USDT",
            "settle": "USDT",
            "type": "swap",
            "active": True,
        }
    }
    req.app.state.trading_exchanges = {"bybit": exchange}
    return req


async def test_manual_close_journal_written_and_all_keys_deleted() -> None:
    rdb = _close_rdb(
        **{
            "trade:id:bybit:BEAT": b"42",
            "position:entry:bybit:BEAT": b"100.0",
            "position:side:bybit:BEAT": b"short",
        }
    )
    cfg = _close_cfg(db_url="postgresql://localhost/test")
    body = CloseBody(exchange="bybit", base="BEAT")
    request = _close_request(cfg, rdb)

    close_result = {"closed": True, "order_id": "ord-1", "exit_price": 95.0, "side": "short"}
    with (
        patch(
            "schurfer_execution.routers.account.close_position",
            new_callable=AsyncMock,
            return_value=close_result,
        ),
        patch(
            "schurfer_execution.routers.account.journal.try_commit_close",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_journal,
        patch(
            "schurfer_execution.routers.account.journal.delete_trade_id_if_matches",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_cas_delete,
    ):
        result = await manual_close_position(body, request)

    assert result["closed"] is True
    mock_journal.assert_called_once()
    kw = mock_journal.call_args.kwargs
    assert kw["trade_id"] == 42
    assert kw["exit_price"] == 95.0
    assert kw["reason"] == "manual"
    assert kw["exchange"] == "bybit"
    assert kw["base"] == "BEAT"

    # entry_price/side are no longer caller-supplied — journal.close_trade
    # loads them from the trade's own DB row by trade_id.
    assert "entry_price" not in kw
    assert "side" not in kw

    # Trade id pointer removed only via compare-and-delete, not unconditionally.
    mock_cas_delete.assert_called_once_with(rdb, "trade:id:bybit:BEAT", 42)

    deleted = {c.args[0] for c in rdb.delete.call_args_list}
    assert "exit:best:bybit:BEAT" in deleted
    assert "exit:params:bybit:BEAT" in deleted
    assert "position:entry:bybit:BEAT" in deleted
    assert "position:side:bybit:BEAT" in deleted


async def test_manual_close_cleanup_happens_without_db_url() -> None:
    rdb = _close_rdb()
    cfg = _close_cfg(db_url=None)
    body = CloseBody(exchange="bybit", base="BEAT")
    request = _close_request(cfg, rdb)

    close_result = {"closed": True, "order_id": "ord-2", "exit_price": None, "side": "short"}
    with (
        patch(
            "schurfer_execution.routers.account.close_position",
            new_callable=AsyncMock,
            return_value=close_result,
        ),
        patch(
            "schurfer_execution.routers.account.journal.try_commit_close",
            new_callable=AsyncMock,
        ) as mock_journal,
    ):
        await manual_close_position(body, request)

    mock_journal.assert_not_called()

    deleted = {c.args[0] for c in rdb.delete.call_args_list}
    assert "exit:best:bybit:BEAT" in deleted
    assert "exit:params:bybit:BEAT" in deleted
    assert "position:entry:bybit:BEAT" in deleted
    assert "position:side:bybit:BEAT" in deleted


async def test_manual_close_journal_failure_does_not_delete_trade_id() -> None:
    """Regression: if the journal commit fails, the trade-id pointer must
    survive (try_commit_close already wrote a durable pending-close marker
    for retry) — the old code deleted it unconditionally."""
    rdb = _close_rdb(**{"trade:id:bybit:BEAT": b"42"})
    cfg = _close_cfg(db_url="postgresql://localhost/test")
    body = CloseBody(exchange="bybit", base="BEAT")
    request = _close_request(cfg, rdb)

    close_result = {"closed": True, "order_id": "ord-1", "exit_price": 95.0, "side": "short"}
    with (
        patch(
            "schurfer_execution.routers.account.close_position",
            new_callable=AsyncMock,
            return_value=close_result,
        ),
        patch(
            "schurfer_execution.routers.account.journal.try_commit_close",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "schurfer_execution.routers.account.journal.delete_trade_id_if_matches",
            new_callable=AsyncMock,
        ) as mock_cas_delete,
    ):
        await manual_close_position(body, request)

    mock_cas_delete.assert_not_called()


async def test_manual_close_no_exit_price_does_not_call_journal() -> None:
    """A close with no usable price can't be safely committed or queued for
    retry (0 would read as a false profit) — must leave the trade-id pointer
    for a human to investigate, not attempt the commit at all."""
    rdb = _close_rdb(**{"trade:id:bybit:BEAT": b"42"})
    cfg = _close_cfg(db_url="postgresql://localhost/test")
    body = CloseBody(exchange="bybit", base="BEAT")
    request = _close_request(cfg, rdb)

    close_result = {"closed": True, "order_id": "ord-1", "exit_price": None, "side": "short"}
    with (
        patch(
            "schurfer_execution.routers.account.close_position",
            new_callable=AsyncMock,
            return_value=close_result,
        ),
        patch(
            "schurfer_execution.routers.account.journal.try_commit_close",
            new_callable=AsyncMock,
        ) as mock_journal,
    ):
        await manual_close_position(body, request)

    mock_journal.assert_not_called()
    deleted = {c.args[0] for c in rdb.delete.call_args_list}
    assert "trade:id:bybit:BEAT" not in deleted


async def test_manual_close_no_exit_price_does_not_double_revoke_readiness() -> None:
    """close_position itself now revokes PnL readiness and creates a durable
    incident the moment a fill price can't be confirmed (fill_price.py /
    incidents.py) — the router must not also call revoke_pnl_readiness, or a
    future accidental double-revoke would be indistinguishable from this
    correct single-revoke behavior."""
    rdb = _close_rdb(**{"trade:id:bybit:BEAT": b"42"})
    cfg = _close_cfg(db_url="postgresql://localhost/test")
    body = CloseBody(exchange="bybit", base="BEAT")
    request = _close_request(cfg, rdb)

    close_result = {
        "closed": True,
        "order_id": "ord-1",
        "exit_price": None,
        "side": "short",
        "fill_status": "unresolved",
        "incident_id": 7,
    }
    with (
        patch(
            "schurfer_execution.routers.account.close_position",
            new_callable=AsyncMock,
            return_value=close_result,
        ),
        patch(
            "schurfer_execution.routers.account.journal.revoke_pnl_readiness",
            new_callable=AsyncMock,
        ) as mock_revoke,
    ):
        await manual_close_position(body, request)

    mock_revoke.assert_not_called()
