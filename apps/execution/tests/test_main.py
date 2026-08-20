from unittest.mock import AsyncMock, MagicMock

from schurfer_execution.main import _preload_markets


async def test_preload_markets_isolates_optional_venue_failure() -> None:
    healthy = MagicMock()
    healthy.load_markets = AsyncMock(return_value={})
    unavailable = MagicMock()
    unavailable.load_markets = AsyncMock(side_effect=RuntimeError("maintenance"))

    failed = await _preload_markets({"bybit": healthy, "optional": unavailable})

    assert failed == {"optional"}
    healthy.load_markets.assert_awaited_once_with()
    unavailable.load_markets.assert_awaited_once_with()
