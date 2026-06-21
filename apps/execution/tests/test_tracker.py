from unittest.mock import AsyncMock, MagicMock, patch

from schurfer_execution.tracker import _tick


def _mock_rdb(current_pnl: float = 0.0) -> MagicMock:
    rdb = MagicMock()
    rdb.set = AsyncMock()
    rdb.get = AsyncMock(return_value=str(current_pnl).encode())
    return rdb


def _mock_exchange(pnl: float, *, fail: bool = False) -> MagicMock:
    ex = MagicMock()
    if fail:
        ex.fetch_positions = AsyncMock(side_effect=Exception("timeout"))
    else:
        ex.fetch_positions = AsyncMock(
            return_value=[{"contracts": 1.0, "unrealizedPnl": pnl, "symbol": "BEAT/USDT:USDT"}]
        )
    return ex


async def test_tick_resets_key_on_new_day() -> None:
    rdb = _mock_rdb()
    with patch("schurfer_execution.tracker.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = "2026-06-20"
        await _tick({"bybit": _mock_exchange(-10.0)}, rdb, last_date="2026-06-19")

    first_call = rdb.set.call_args_list[0]
    assert first_call[0] == ("trading:daily_pnl", "0")


async def test_tick_no_reset_same_day() -> None:
    rdb = _mock_rdb(current_pnl=-5.0)
    with patch("schurfer_execution.tracker.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = "2026-06-20"
        await _tick({"bybit": _mock_exchange(0.0)}, rdb, last_date="2026-06-20")

    # No set call — unrealized 0.0 is not worse than current -5.0
    rdb.set.assert_not_called()


async def test_tick_updates_when_unrealized_worse() -> None:
    rdb = _mock_rdb(current_pnl=-30.0)
    with patch("schurfer_execution.tracker.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = "2026-06-20"
        await _tick({"bybit": _mock_exchange(-50.0)}, rdb, last_date="2026-06-20")

    rdb.set.assert_called_once_with("trading:daily_pnl", "-50.0")


async def test_tick_does_not_update_when_unrealized_recovers() -> None:
    rdb = _mock_rdb(current_pnl=-50.0)
    with patch("schurfer_execution.tracker.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = "2026-06-20"
        await _tick({"bybit": _mock_exchange(-10.0)}, rdb, last_date="2026-06-20")

    # -10 is better than -50: closed loss stays remembered, no overwrite
    rdb.set.assert_not_called()


async def test_tick_skips_update_on_partial_failure() -> None:
    rdb = _mock_rdb()
    exchanges = {"bybit": _mock_exchange(-20.0), "bingx": _mock_exchange(0.0, fail=True)}
    with patch("schurfer_execution.tracker.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = "2026-06-20"
        await _tick(exchanges, rdb, last_date="2026-06-20")

    rdb.set.assert_not_called()


async def test_tick_sums_across_exchanges() -> None:
    rdb = _mock_rdb(current_pnl=0.0)
    ex1 = MagicMock()
    ex1.fetch_positions = AsyncMock(
        return_value=[
            {"contracts": 1.0, "unrealizedPnl": -30.0, "symbol": "BEAT/USDT:USDT"},
            {"contracts": 1.0, "unrealizedPnl": 10.0, "symbol": "ACT/USDT:USDT"},
        ]
    )
    ex2 = MagicMock()
    ex2.fetch_positions = AsyncMock(
        return_value=[{"contracts": 1.0, "unrealizedPnl": -5.5, "symbol": "SYN/USDT:USDT"}]
    )
    with patch("schurfer_execution.tracker.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = "2026-06-20"
        await _tick({"bybit": ex1, "bingx": ex2}, rdb, last_date="2026-06-20")

    rdb.set.assert_called_once_with("trading:daily_pnl", "-25.5")


async def test_tick_returns_today() -> None:
    rdb = _mock_rdb()
    with patch("schurfer_execution.tracker.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = "2026-06-20"
        result = await _tick({"bybit": _mock_exchange(0.0)}, rdb, last_date=None)

    assert result == "2026-06-20"
