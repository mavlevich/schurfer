from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from schurfer_execution.main import _preload_markets, lifespan
from schurfer_execution.supervisor import WorkerState


async def test_preload_markets_isolates_optional_venue_failure() -> None:
    healthy = MagicMock()
    healthy.load_markets = AsyncMock(return_value={})
    unavailable = MagicMock()
    unavailable.load_markets = AsyncMock(side_effect=RuntimeError("maintenance"))

    failed = await _preload_markets({"bybit": healthy, "optional": unavailable})

    assert failed == {"optional"}
    healthy.load_markets.assert_awaited_once_with()
    unavailable.load_markets.assert_awaited_once_with()


async def test_disabled_strategy_workers_do_not_crash_startup() -> None:
    cfg = SimpleNamespace(
        redis_addr="localhost:6379",
        auto_trade=False,
        dry_run=False,
        db_url=None,
        pump_short_mode=None,
        early_momentum_mode=None,
        liquidation_cascade_mode=None,
    )
    rdb = MagicMock()
    rdb.set = AsyncMock(return_value=True)
    rdb.aclose = AsyncMock()
    clients = SimpleNamespace(
        market={},
        trading={},
        strategy_clients=MagicMock(return_value={}),
    )
    app = FastAPI()

    with (
        patch("schurfer_execution.main.Config", return_value=cfg),
        patch("schurfer_execution.main.aioredis.from_url", return_value=rdb),
        patch("schurfer_execution.main.build_exchange_clients", return_value=clients),
        patch("schurfer_execution.main.close_exchange_clients", AsyncMock()),
    ):
        async with lifespan(app):
            workers = app.state.supervisor.workers
            assert workers["signal_trader"].state == WorkerState.STOPPED_INTENTIONALLY
            assert workers["paper_monitor"].state == WorkerState.STOPPED_INTENTIONALLY
            assert workers["liquidation_cascade_scanner"].state == WorkerState.STOPPED_INTENTIONALLY
            assert workers["early_momentum_scanner"].state == WorkerState.STOPPED_INTENTIONALLY
