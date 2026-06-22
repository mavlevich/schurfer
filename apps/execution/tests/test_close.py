import time
from unittest.mock import AsyncMock, MagicMock, patch

from schurfer_execution.config import Config
from schurfer_execution.monitor import _check_exit
from schurfer_execution.orders import close_position


def _cfg(*, tp: float = 15.0, sl: float = 5.0, hold: int = 60) -> Config:
    cfg = object.__new__(Config)
    cfg.take_profit_pct = tp
    cfg.stop_loss_pct = sl
    cfg.max_hold_minutes = hold
    return cfg


def _rdb(*, locked: bool = True, opened_at: bytes | None = None) -> MagicMock:
    rdb = MagicMock()
    rdb.set = AsyncMock(return_value=locked)
    rdb.eval = AsyncMock(return_value=1)
    rdb.delete = AsyncMock()
    rdb.get = AsyncMock(return_value=opened_at)
    return rdb


def _exchange(*, contracts: float = 10.0, symbol: str = "BEAT/USDT:USDT") -> MagicMock:
    ex = MagicMock()
    ex.markets = {symbol: {"contractSize": 1.0}}
    ex.fetch_positions = AsyncMock(
        return_value=[{"symbol": symbol, "contracts": contracts, "side": "short"}]
    )
    ex.create_market_order = AsyncMock(return_value={"id": "order-123", "status": "closed"})
    ex.amount_to_precision = MagicMock(side_effect=lambda sym, amt: str(amt))
    return ex


def _pos(entry: float, mark: float, side: str = "short") -> dict:  # type: ignore[type-arg]
    return {
        "exchange": "bybit",
        "base": "BEAT",
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
        reason="test",
        rdb=rdb,
    )
    rdb.delete.assert_called_once_with("position:opened_at:bybit:BEAT")


# --- monitor _check_exit ---


async def test_check_exit_take_profit_short_triggers_close() -> None:
    # Entry 100, mark 84 → 16% profit for short → above 15% TP threshold
    pos = _pos(entry=100.0, mark=84.0, side="short")
    rdb = _rdb()
    with patch("schurfer_execution.monitor.close_position", new_callable=AsyncMock) as mock_close:
        await _check_exit(pos, rdb, _cfg(tp=15.0), {})
        mock_close.assert_called_once()
        assert "take_profit" in mock_close.call_args.kwargs["reason"]


async def test_check_exit_take_profit_long_triggers_close() -> None:
    # Entry 100, mark 116 → 16% profit for long → above 15% TP threshold
    pos = _pos(entry=100.0, mark=116.0, side="long")
    rdb = _rdb()
    with patch("schurfer_execution.monitor.close_position", new_callable=AsyncMock) as mock_close:
        await _check_exit(pos, rdb, _cfg(tp=15.0), {})
        mock_close.assert_called_once()
        assert "take_profit" in mock_close.call_args.kwargs["reason"]


async def test_check_exit_stop_loss_short_triggers_close() -> None:
    # Entry 100, mark 106 → 6% loss for short → below -5% SL threshold
    pos = _pos(entry=100.0, mark=106.0, side="short")
    rdb = _rdb()
    with patch("schurfer_execution.monitor.close_position", new_callable=AsyncMock) as mock_close:
        await _check_exit(pos, rdb, _cfg(sl=5.0), {})
        mock_close.assert_called_once()
        assert "stop_loss" in mock_close.call_args.kwargs["reason"]


async def test_check_exit_max_hold_triggers_close() -> None:
    # No P&L signal, but position opened 61+ minutes ago
    pos = _pos(entry=100.0, mark=100.0)
    old_ts = str(time.time() - 3700).encode()
    rdb = _rdb(opened_at=old_ts)
    with patch("schurfer_execution.monitor.close_position", new_callable=AsyncMock) as mock_close:
        await _check_exit(pos, rdb, _cfg(hold=60), {})
        mock_close.assert_called_once()
        assert "max_hold" in mock_close.call_args.kwargs["reason"]


async def test_check_exit_no_conditions_met_no_close() -> None:
    # 5% profit for short, TP is 15% → below threshold; no opened_at in Redis
    pos = _pos(entry=100.0, mark=95.0)
    rdb = _rdb(opened_at=None)
    with patch("schurfer_execution.monitor.close_position", new_callable=AsyncMock) as mock_close:
        await _check_exit(pos, rdb, _cfg(tp=15.0, sl=5.0, hold=60), {})
        mock_close.assert_not_called()


async def test_check_exit_missing_mark_price_skips_pnl_check() -> None:
    # mark=0 means price unavailable — P&L block is skipped entirely
    pos = _pos(entry=100.0, mark=0.0)
    rdb = _rdb(opened_at=None)
    with patch("schurfer_execution.monitor.close_position", new_callable=AsyncMock) as mock_close:
        await _check_exit(pos, rdb, _cfg(tp=1.0, sl=1.0, hold=60), {})
        mock_close.assert_not_called()


async def test_check_exit_max_hold_not_reached_no_close() -> None:
    # Position opened 30 minutes ago, max_hold is 60 minutes
    pos = _pos(entry=100.0, mark=100.0)
    recent_ts = str(time.time() - 1800).encode()
    rdb = _rdb(opened_at=recent_ts)
    with patch("schurfer_execution.monitor.close_position", new_callable=AsyncMock) as mock_close:
        await _check_exit(pos, rdb, _cfg(hold=60), {})
        mock_close.assert_not_called()
