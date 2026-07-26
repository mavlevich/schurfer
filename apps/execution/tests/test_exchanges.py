from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from schurfer_analytics.exchange_registry import DEFAULT_EXCHANGES
from schurfer_execution.config import Config
from schurfer_execution.exchanges import (
    MARKET_EXCHANGE_FACTORIES,
    ExchangeClients,
    build_exchange_clients,
    close_exchange_clients,
)

EXPECTED_MARKET_EXCHANGES = set(DEFAULT_EXCHANGES)


def _cfg(
    *,
    dry_run: bool,
    testnet: bool = False,
    binance_api_key: str | None = None,
    binance_api_secret: str | None = None,
) -> Config:
    cfg = object.__new__(Config)
    cfg.dry_run = dry_run
    cfg.testnet = testnet
    cfg.binance_api_key = binance_api_key
    cfg.binance_api_secret = binance_api_secret
    cfg.bybit_api_key = None
    cfg.bybit_api_secret = None
    cfg.okx_api_key = None
    cfg.okx_api_secret = None
    cfg.okx_passphrase = None
    cfg.gate_api_key = None
    cfg.gate_api_secret = None
    cfg.kucoin_api_key = None
    cfg.kucoin_api_secret = None
    cfg.kucoin_passphrase = None
    cfg.bingx_api_key = None
    cfg.bingx_api_secret = None
    cfg.mexc_api_key = None
    cfg.mexc_api_secret = None
    return cfg


def test_market_registry_matches_scanner_coverage() -> None:
    assert tuple(MARKET_EXCHANGE_FACTORIES) == DEFAULT_EXCHANGES


async def test_dry_run_builds_all_public_market_clients() -> None:
    clients = build_exchange_clients(_cfg(dry_run=True))
    try:
        assert set(clients.market) == EXPECTED_MARKET_EXCHANGES
        assert clients.trading == {}
        assert clients.market["binance"].options["defaultType"] == "future"
        assert clients.market["bybit"].options["defaultType"] == "linear"
        assert clients.market["kucoin"].id == "kucoinfutures"
        assert all(exchange.enableRateLimit for exchange in clients.market.values())
    finally:
        await close_exchange_clients(clients)


async def test_authenticated_client_is_isolated_from_public_market_client() -> None:
    clients = build_exchange_clients(
        _cfg(
            dry_run=True,
            binance_api_key="private-key",
            binance_api_secret="private-secret",  # noqa: S106
        )
    )
    try:
        assert set(clients.trading) == {"binance"}
        assert clients.market["binance"] is not clients.trading["binance"]
        assert not clients.market["binance"].apiKey
        assert clients.trading["binance"].apiKey == "private-key"
    finally:
        await close_exchange_clients(clients)


def test_market_clients_are_not_built_when_measurement_is_disabled() -> None:
    clients = build_exchange_clients(_cfg(dry_run=False))
    assert clients.market == {}
    assert clients.trading == {}


def test_strategy_scope_never_uses_public_clients_for_live_orders() -> None:
    market = {"market": MagicMock()}
    trading = {"trading": MagicMock()}
    clients = ExchangeClients(market=market, trading=trading)

    assert clients.strategy_clients(dry_run=True) is market
    assert clients.strategy_clients(dry_run=False) is trading


async def test_close_clients_attempts_every_client_after_failure() -> None:
    failed = MagicMock()
    failed.close = AsyncMock(side_effect=RuntimeError("close failed"))
    healthy = MagicMock()
    healthy.close = AsyncMock()
    clients = ExchangeClients(
        market={"failed": failed},
        trading={"healthy": healthy},
    )

    await close_exchange_clients(clients)

    failed.close.assert_awaited_once()
    healthy.close.assert_awaited_once()
