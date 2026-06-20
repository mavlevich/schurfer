from unittest.mock import AsyncMock, MagicMock

from schurfer_execution.account import fetch_positions


def _mock_exchange(positions: list, *, fail: bool = False) -> MagicMock:
    ex = MagicMock()
    if fail:
        ex.fetch_positions = AsyncMock(side_effect=Exception("network error"))
    else:
        ex.fetch_positions = AsyncMock(return_value=positions)
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


async def test_fetch_positions_returns_failed_on_error() -> None:
    ex = _mock_exchange([], fail=True)
    positions, failed = await fetch_positions({"bingx": ex})
    assert positions == []
    assert "bingx" in failed


async def test_fetch_positions_ok_exchange_not_in_failed() -> None:
    ex = _mock_exchange([_open_position()])
    positions, failed = await fetch_positions({"bybit": ex})
    assert len(positions) == 1
    assert "bybit" not in failed


async def test_fetch_positions_partial_failure() -> None:
    good = _mock_exchange([_open_position()])
    bad = _mock_exchange([], fail=True)

    positions, failed = await fetch_positions({"bybit": good, "bingx": bad})

    assert len(positions) == 1
    assert positions[0]["exchange"] == "bybit"
    assert "bingx" in failed
    assert "bybit" not in failed


async def test_fetch_positions_skips_zero_contracts() -> None:
    pos = {**_open_position(), "contracts": 0.0}
    ex = _mock_exchange([pos])
    positions, failed = await fetch_positions({"bybit": ex})
    assert positions == []
    assert not failed
