import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_execution.config import Config
from schurfer_execution.exit import exit_params
from schurfer_execution.monitor import _check_exit, _tick
from schurfer_execution.orders import close_position
from schurfer_execution.symbols import ExecutionInstrument


@pytest.fixture(autouse=True)
def mock_resolve_execution_instrument(monkeypatch):
    def dummy_resolve(ex, base, *args, **kwargs):
        return ExecutionInstrument(
            exchange=ex.id if hasattr(ex, "id") else "bybit",
            symbol=f"{base.upper()}/USDT:USDT",
            native_market_id=f"{base.upper()}USDT",
            base=base.upper(),
            quote="USDT",
            settle="USDT",
            market_type="swap",
        )

    monkeypatch.setattr("schurfer_execution.symbols.resolve_execution_instrument", dummy_resolve)


def _cfg() -> Config:
    cfg = object.__new__(Config)
    cfg.db_url = None
    cfg.telegram_bot_token = None
    cfg.telegram_chat_id = None
    return cfg


def _rdb(
    *,
    locked: bool = True,
    opened_at: bytes | None = None,
    exit_params_raw: bytes | None = None,
    best_price: bytes | None = None,
) -> MagicMock:
    rdb = MagicMock()
    rdb.set = AsyncMock(return_value=locked)
    rdb.eval = AsyncMock(return_value=1)
    rdb.delete = AsyncMock()

    # Route get() calls to the right fixture values by key substring
    async def _get(key: str) -> bytes | None:
        k = key if isinstance(key, str) else key.decode()
        if "opened_at" in k:
            return opened_at
        if "exit:params" in k:
            return exit_params_raw
        if "exit:best" in k:
            return best_price
        return None

    rdb.get = AsyncMock(side_effect=_get)
    return rdb


_CLOSE_OK = {"closed": True, "order_id": "ord-1", "exit_price": None}


def _exchange(*, contracts: float = 10.0, symbol: str = "BEAT/USDT:USDT") -> MagicMock:
    ex = MagicMock()
    ex.markets = {symbol: {"contractSize": 1.0}}
    ex.fetch_positions = AsyncMock(
        return_value=[{"symbol": symbol, "contracts": contracts, "side": "short"}]
    )
    ex.create_market_order = AsyncMock(
        return_value={"id": "order-123", "status": "closed", "average": 1.0}
    )
    ex.amount_to_precision = MagicMock(side_effect=lambda sym, amt: str(amt))
    return ex


def _pos(entry: float, mark: float, side: str = "short") -> dict:  # type: ignore[type-arg]
    return {
        "exchange": "bybit",
        "base": "BEAT",
        "symbol": "BEAT/USDT:USDT",
        "side": side,
        "entry_price": entry,
        "mark_price": mark,
        "size_usd": 200.0,
    }


# --- close_position ---


async def test_close_short_places_buy_with_reduce_only() -> None:
    # Side derived from position["side"] = "short" → close with "buy"
    ex = _exchange()
    rdb = _rdb()
    result = await close_position(
        exchanges={"bybit": ex},
        exchange="bybit",
        base="BEAT",
        symbol="BEAT/USDT:USDT",
        reason="test",
        rdb=rdb,
    )
    assert result["closed"] is True
    call = ex.create_market_order.call_args
    assert call[0][1] == "buy"
    assert call[1]["params"] == {"reduceOnly": True}


async def test_close_long_places_sell_order() -> None:
    # Side derived from position["side"] = "long" → close with "sell"
    ex = _exchange()
    ex.fetch_positions = AsyncMock(
        return_value=[{"symbol": "BEAT/USDT:USDT", "contracts": 5.0, "side": "long"}]
    )
    rdb = _rdb()
    result = await close_position(
        exchanges={"bybit": ex},
        exchange="bybit",
        base="BEAT",
        symbol="BEAT/USDT:USDT",
        reason="test",
        rdb=rdb,
    )
    assert result["closed"] is True
    assert ex.create_market_order.call_args[0][1] == "sell"


async def test_close_side_derived_from_position_not_caller() -> None:
    # Regression: close direction must come from exchange-reported position, never from caller.
    # _exchange() reports side="short" → must send "buy" regardless of what caller would pass.
    ex = _exchange()
    rdb = _rdb()
    result = await close_position(
        exchanges={"bybit": ex},
        exchange="bybit",
        base="BEAT",
        symbol="BEAT/USDT:USDT",
        reason="test",
        rdb=rdb,
    )
    assert result["closed"] is True
    assert result["side"] == "short"
    assert ex.create_market_order.call_args[0][1] == "buy"


async def test_close_position_unknown_side_returns_error() -> None:
    ex = _exchange()
    ex.fetch_positions = AsyncMock(
        return_value=[{"symbol": "BEAT/USDT:USDT", "contracts": 5.0, "side": ""}]
    )
    rdb = _rdb()
    result = await close_position(
        exchanges={"bybit": ex},
        exchange="bybit",
        base="BEAT",
        symbol="BEAT/USDT:USDT",
        reason="test",
        rdb=rdb,
    )
    assert result["closed"] is False
    assert "side unknown" in result["reason"]


async def test_close_lock_contention_returns_blocked() -> None:
    rdb = _rdb(locked=False)
    result = await close_position(
        exchanges={"bybit": _exchange()},
        exchange="bybit",
        base="BEAT",
        symbol="BEAT/USDT:USDT",
        reason="test",
        rdb=rdb,
    )
    assert result["closed"] is False
    assert "in progress" in result["reason"]


async def test_close_lock_not_released_when_not_acquired() -> None:
    rdb = _rdb(locked=False)
    await close_position(
        exchanges={"bybit": _exchange()},
        exchange="bybit",
        base="BEAT",
        symbol="BEAT/USDT:USDT",
        reason="test",
        rdb=rdb,
    )
    rdb.eval.assert_not_called()


async def test_close_unknown_exchange_returns_not_closed() -> None:
    rdb = _rdb()
    result = await close_position(
        exchanges={},
        exchange="bybit",
        base="BEAT",
        symbol="BEAT/USDT:USDT",
        reason="test",
        rdb=rdb,
    )
    assert result["closed"] is False
    assert "not configured" in result["reason"]


async def test_close_no_open_position_returns_not_closed() -> None:
    ex = _exchange()
    ex.fetch_positions = AsyncMock(return_value=[])
    rdb = _rdb()
    result = await close_position(
        exchanges={"bybit": ex},
        exchange="bybit",
        base="BEAT",
        symbol="BEAT/USDT:USDT",
        reason="test",
        rdb=rdb,
    )
    assert result["closed"] is False
    assert "no open position" in result["reason"]
    rdb.delete.assert_not_called()


async def test_close_zero_contracts_not_found() -> None:
    ex = _exchange(contracts=0.0)
    rdb = _rdb()
    result = await close_position(
        exchanges={"bybit": ex},
        exchange="bybit",
        base="BEAT",
        symbol="BEAT/USDT:USDT",
        reason="test",
        rdb=rdb,
    )
    assert result["closed"] is False


async def test_close_deletes_opened_at_key_on_success() -> None:
    ex = _exchange()
    rdb = _rdb()
    await close_position(
        exchanges={"bybit": ex},
        exchange="bybit",
        base="BEAT",
        symbol="BEAT/USDT:USDT",
        reason="test",
        rdb=rdb,
    )
    rdb.delete.assert_called_once_with("position:opened_at:bybit:BEAT")


async def test_close_unresolved_fill_never_fabricates_exit_price() -> None:
    # No average/price/cost/filled anywhere, no fetchable order-trades support —
    # close_position must never invent an exit price from mark/ticker data.
    ex = _exchange()
    ex.create_market_order = AsyncMock(return_value={"id": "order-999", "status": "closed"})
    ex.has = {"fetchOrderTrades": False, "fetchMyTrades": False}
    rdb = _rdb()

    result = await close_position(
        exchanges={"bybit": ex},
        exchange="bybit",
        base="BEAT",
        symbol="BEAT/USDT:USDT",
        reason="test",
        rdb=rdb,
        cfg=_cfg(),
    )

    assert result["closed"] is True
    assert result["exit_price"] is None
    assert result["fill_status"] == "unresolved"
    rdb.delete.assert_any_call("risk:pnl_ready")


# --- monitor _check_exit ---


def _params_bytes(pump_pct: float = 50.0) -> bytes:
    return json.dumps(exit_params(pump_pct)).encode()


async def test_check_exit_initial_sl_triggers_close() -> None:
    # Short entry=100, mark=111 → 11% loss > initial_sl=10% (pump=75) → close
    pos = _pos(entry=100.0, mark=111.0, side="short")
    rdb = _rdb(exit_params_raw=_params_bytes(75.0))
    with patch("schurfer_execution.monitor.close_position", new_callable=AsyncMock) as mock_close:
        mock_close.return_value = _CLOSE_OK
        await _check_exit(pos, rdb, _cfg(), {})
        mock_close.assert_called_once()
        assert "initial_sl" in mock_close.call_args.kwargs["reason"]


async def test_check_exit_trailing_stop_triggers_close() -> None:
    # Short entry=100, best_price=80 activated, trail=15% → sl=92, mark=93 → close
    pos = _pos(entry=100.0, mark=93.0, side="short")
    rdb = _rdb(exit_params_raw=_params_bytes(75.0), best_price=b"80.0")
    with patch("schurfer_execution.monitor.close_position", new_callable=AsyncMock) as mock_close:
        mock_close.return_value = _CLOSE_OK
        await _check_exit(pos, rdb, _cfg(), {})
        mock_close.assert_called_once()
        assert "trailing_stop" in mock_close.call_args.kwargs["reason"]


async def test_check_exit_resolved_close_without_trade_id_creates_durable_incident() -> None:
    """Regression: a resolved exit price with no trade:id pointer (e.g. the
    matching open's journal write is itself still deferred behind its own
    unresolved-fill incident) must not be silently dropped — it has to become
    a durable, alerted incident like any other close the journal can't
    complete inline."""
    pos = _pos(entry=100.0, mark=111.0, side="short")
    rdb = _rdb(exit_params_raw=_params_bytes(75.0))
    cfg = _cfg()
    cfg.db_url = "postgresql://x"
    cfg.telegram_bot_token = "tok"  # noqa: S105
    cfg.telegram_chat_id = "chat"
    close_result = {"closed": True, "order_id": "ord-1", "exit_price": 111.0}

    with (
        patch("schurfer_execution.monitor.close_position", new_callable=AsyncMock) as mock_close,
        patch(
            "schurfer_execution.monitor.incidents.create_incident",
            new_callable=AsyncMock,
            return_value=55,
        ) as mock_create,
        patch(
            "schurfer_execution.monitor.incidents.claim_creation_notification",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "schurfer_execution.monitor.notify.notify_alert", new_callable=AsyncMock
        ) as mock_alert,
        patch(
            "schurfer_execution.monitor.journal.revoke_pnl_readiness", new_callable=AsyncMock
        ) as mock_revoke,
    ):
        mock_close.return_value = close_result
        await _check_exit(pos, rdb, cfg, {})

    mock_create.assert_called_once()
    assert mock_create.call_args.kwargs["operation"] == "close"
    assert mock_create.call_args.kwargs["trade_id"] is None
    assert mock_create.call_args.kwargs["order_id"] == "ord-1"
    mock_revoke.assert_called_once()
    mock_alert.assert_awaited_once()


async def test_check_exit_max_hold_triggers_close() -> None:
    # Position opened well beyond max_hold_min → close regardless of price
    params = exit_params(50.0)
    old_ts = str(time.time() - params["max_hold_min"] * 60 - 60).encode()
    pos = _pos(entry=100.0, mark=100.0)
    rdb = _rdb(opened_at=old_ts, exit_params_raw=_params_bytes(50.0))
    with patch("schurfer_execution.monitor.close_position", new_callable=AsyncMock) as mock_close:
        mock_close.return_value = _CLOSE_OK
        await _check_exit(pos, rdb, _cfg(), {})
        mock_close.assert_called_once()
        assert "max_hold" in mock_close.call_args.kwargs["reason"]


async def test_check_exit_no_conditions_met_no_close() -> None:
    # 5% profit, activation=12% not reached, no max_hold → no close
    pos = _pos(entry=100.0, mark=95.0)
    rdb = _rdb(exit_params_raw=_params_bytes(75.0))
    with patch("schurfer_execution.monitor.close_position", new_callable=AsyncMock) as mock_close:
        await _check_exit(pos, rdb, _cfg(), {})
        mock_close.assert_not_called()


async def test_check_exit_missing_mark_price_skips() -> None:
    # mark=0 → skipped entirely
    pos = _pos(entry=100.0, mark=0.0)
    rdb = _rdb()
    with patch("schurfer_execution.monitor.close_position", new_callable=AsyncMock) as mock_close:
        await _check_exit(pos, rdb, _cfg(), {})
        mock_close.assert_not_called()


async def test_check_exit_max_hold_not_reached_no_close() -> None:
    # Position opened 10 minutes ago → no close
    recent_ts = str(time.time() - 600).encode()
    pos = _pos(entry=100.0, mark=100.0)
    rdb = _rdb(opened_at=recent_ts, exit_params_raw=_params_bytes(50.0))
    with patch("schurfer_execution.monitor.close_position", new_callable=AsyncMock) as mock_close:
        await _check_exit(pos, rdb, _cfg(), {})
        mock_close.assert_not_called()


# --- monitor _tick isolation ---


async def test_tick_continues_checking_remaining_positions_after_error() -> None:
    # Regression: if _check_exit raises for position 1, position 2 must still be checked.
    pos1 = _pos(entry=100.0, mark=95.0)
    pos2 = {**_pos(entry=200.0, mark=190.0), "base": "SOL"}
    rdb = _rdb()

    check_calls: list[str] = []

    async def _fake_check_exit(pos: dict, *args, **kwargs) -> None:
        check_calls.append(pos["base"])
        if pos["base"] == "BEAT":
            raise RuntimeError("simulated exchange error")

    with (
        patch(
            "schurfer_execution.monitor.fetch_positions",
            new_callable=AsyncMock,
            return_value=([pos1, pos2], set()),
        ),
        patch("schurfer_execution.monitor._check_exit", side_effect=_fake_check_exit),
    ):
        await _tick({}, rdb, _cfg())

    assert "BEAT" in check_calls
    assert "SOL" in check_calls  # must be reached despite BEAT raising
