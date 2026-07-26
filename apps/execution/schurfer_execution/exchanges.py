from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import ccxt.async_support as ccxt
import structlog

if TYPE_CHECKING:
    from .config import Config

log = structlog.get_logger()

ExchangeFactory = Callable[[], ccxt.Exchange]


def _swap_factory(
    exchange_type: type[ccxt.Exchange],
    *,
    default_type: str = "swap",
    adjust_for_time_difference: bool = False,
) -> ExchangeFactory:
    def build() -> ccxt.Exchange:
        options: dict[str, object] = {
            "enableRateLimit": True,
            "options": {"defaultType": default_type},
        }
        if adjust_for_time_difference:
            options["adjustForTimeDifference"] = True
        return exchange_type(options)

    return build


# Keep this coverage aligned with the scanner's public linear-USDT registry. These
# clients never receive credentials and are used only for measurement and paper
# monitoring. A separate registry prevents public clients from reaching account,
# position, or order paths.
MARKET_EXCHANGE_FACTORIES: dict[str, ExchangeFactory] = {
    "binance": _swap_factory(ccxt.binance, default_type="future"),
    "bybit": _swap_factory(
        ccxt.bybit,
        default_type="linear",
        adjust_for_time_difference=True,
    ),
    "okx": _swap_factory(ccxt.okx),
    "gate": _swap_factory(ccxt.gate),
    "bitget": _swap_factory(ccxt.bitget),
    "mexc": _swap_factory(ccxt.mexc),
    "kucoin": _swap_factory(ccxt.kucoinfutures),
    "bingx": _swap_factory(ccxt.bingx, adjust_for_time_difference=True),
    "coinex": _swap_factory(ccxt.coinex),
    "phemex": _swap_factory(ccxt.phemex),
    "cryptocom": _swap_factory(ccxt.cryptocom),
    "htx": _swap_factory(ccxt.htx),
    "lbank": _swap_factory(ccxt.lbank),
    "bitmart": _swap_factory(ccxt.bitmart),
    "xt": _swap_factory(ccxt.xt),
    "toobit": _swap_factory(ccxt.toobit),
    "blofin": _swap_factory(ccxt.blofin),
}


@dataclass(frozen=True)
class ExchangeClients:
    market: dict[str, ccxt.Exchange]
    trading: dict[str, ccxt.Exchange]

    def strategy_clients(self, *, dry_run: bool) -> dict[str, ccxt.Exchange]:
        """Return public clients for paper measurement, authenticated clients for live."""
        return self.market if dry_run else self.trading


def _build_trading_exchanges(cfg: Config) -> dict[str, ccxt.Exchange]:
    exchanges: dict[str, ccxt.Exchange] = {}

    if cfg.binance_api_key and cfg.binance_api_secret:
        exchanges["binance"] = ccxt.binance(
            {
                "apiKey": cfg.binance_api_key,
                "secret": cfg.binance_api_secret,
                "options": {"defaultType": "future"},
            }
        )

    if cfg.bybit_api_key and cfg.bybit_api_secret:
        exchanges["bybit"] = ccxt.bybit(
            {
                "apiKey": cfg.bybit_api_key,
                "secret": cfg.bybit_api_secret,
                "options": {"defaultType": "linear"},
                "adjustForTimeDifference": True,
            }
        )

    if cfg.okx_api_key and cfg.okx_api_secret and cfg.okx_passphrase:
        exchanges["okx"] = ccxt.okx(
            {
                "apiKey": cfg.okx_api_key,
                "secret": cfg.okx_api_secret,
                "password": cfg.okx_passphrase,
                "options": {"defaultType": "swap"},
            }
        )

    if cfg.gate_api_key and cfg.gate_api_secret:
        exchanges["gate"] = ccxt.gate(
            {
                "apiKey": cfg.gate_api_key,
                "secret": cfg.gate_api_secret,
                "options": {"defaultType": "swap"},
            }
        )

    if cfg.kucoin_api_key and cfg.kucoin_api_secret and cfg.kucoin_passphrase:
        exchanges["kucoin"] = ccxt.kucoinfutures(
            {
                "apiKey": cfg.kucoin_api_key,
                "secret": cfg.kucoin_api_secret,
                "password": cfg.kucoin_passphrase,
            }
        )

    if cfg.bingx_api_key and cfg.bingx_api_secret:
        exchanges["bingx"] = ccxt.bingx(
            {
                "apiKey": cfg.bingx_api_key,
                "secret": cfg.bingx_api_secret,
                "options": {"defaultType": "swap"},
                "adjustForTimeDifference": True,
            }
        )

    if cfg.mexc_api_key and cfg.mexc_api_secret:
        exchanges["mexc"] = ccxt.mexc(
            {
                "apiKey": cfg.mexc_api_key,
                "secret": cfg.mexc_api_secret,
                "options": {"defaultType": "swap"},
            }
        )

    return exchanges


def _enable_testnet(exchanges: dict[str, ccxt.Exchange], *, scope: str) -> None:
    for name, exchange in exchanges.items():
        try:
            exchange.set_sandbox_mode(True)
            log.info("exchanges.testnet.enabled", exchange=name, scope=scope)
        except Exception as exc:
            log.warning(
                "exchanges.testnet.unsupported",
                exchange=name,
                scope=scope,
                err=str(exc),
            )


def build_exchange_clients(cfg: Config) -> ExchangeClients:
    """Build isolated public measurement and authenticated trading clients."""
    market = (
        {name: factory() for name, factory in MARKET_EXCHANGE_FACTORIES.items()}
        if cfg.dry_run
        else {}
    )
    trading = _build_trading_exchanges(cfg)

    if cfg.testnet:
        _enable_testnet(market, scope="market")
        _enable_testnet(trading, scope="trading")

    return ExchangeClients(market=market, trading=trading)


async def close_exchange_clients(clients: ExchangeClients) -> None:
    """Close every client even if one exchange raises during shutdown."""
    named_clients = [
        *((f"market:{name}", exchange) for name, exchange in clients.market.items()),
        *((f"trading:{name}", exchange) for name, exchange in clients.trading.items()),
    ]
    results = await asyncio.gather(
        *(exchange.close() for _, exchange in named_clients),
        return_exceptions=True,
    )
    for (name, _), result in zip(named_clients, results, strict=True):
        if isinstance(result, BaseException):
            log.warning("exchanges.close_failed", exchange=name, err=str(result))
