import ccxt.async_support as ccxt
import structlog

from .config import Config

log = structlog.get_logger()


def build_exchanges(cfg: Config) -> dict[str, ccxt.Exchange]:
    """Return ccxt async exchange instances for all configured API keys."""
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

    if cfg.testnet:
        for name, ex in exchanges.items():
            try:
                ex.set_sandbox_mode(True)
                log.info("exchanges.testnet.enabled", exchange=name)
            except Exception as e:
                log.warning("exchanges.testnet.unsupported", exchange=name, err=str(e))

    return exchanges


async def close_exchanges(exchanges: dict[str, ccxt.Exchange]) -> None:
    for ex in exchanges.values():
        await ex.close()
