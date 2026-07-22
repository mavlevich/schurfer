"""Public linear-USDT exchange clients shared by analytics workers."""

from collections.abc import Callable
from typing import Any

import ccxt.async_support as ccxt

ExchangeFactory = Callable[[], Any]


def _swap_factory(exchange_type: type[Any]) -> ExchangeFactory:
    """Build an isolated rate-limited linear swap client."""

    def build() -> Any:
        return exchange_type({"enableRateLimit": True, "options": {"defaultType": "swap"}})

    return build


EXCHANGE_FACTORIES: dict[str, ExchangeFactory] = {
    "binance": _swap_factory(ccxt.binance),
    "bybit": _swap_factory(ccxt.bybit),
    "okx": _swap_factory(ccxt.okx),
    "gate": _swap_factory(ccxt.gate),
    "bitget": _swap_factory(ccxt.bitget),
    "mexc": _swap_factory(ccxt.mexc),
    "kucoin": _swap_factory(ccxt.kucoinfutures),
    "bingx": _swap_factory(ccxt.bingx),
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

DEFAULT_EXCHANGES: tuple[str, ...] = tuple(EXCHANGE_FACTORIES)
