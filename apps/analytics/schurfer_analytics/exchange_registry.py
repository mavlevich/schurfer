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
    # bitmart removed with the ccxt 4.5.77 upgrade: upstream dropped the
    # exchange entirely (4.5.71 onward), and staying below that version means
    # keeping ccxt's exact pins on aiohttp 3.14.1 and cryptography 49.0.0,
    # which carry four advisories. It cost nothing measurable: over the 30
    # days to 2026-09-07 bitmart produced zero pump event sources and was
    # never the only venue for an event, despite listing 1187 USDT linear
    # instruments. Historical rows keep their bitmart attribution (ENG-026).
    "xt": _swap_factory(ccxt.xt),
    "toobit": _swap_factory(ccxt.toobit),
    "blofin": _swap_factory(ccxt.blofin),
}

DEFAULT_EXCHANGES: tuple[str, ...] = tuple(EXCHANGE_FACTORIES)
