"""Funding rate fetching: per-exchange 8h rates for live and tracked pumps."""

import asyncio
from typing import Any

import structlog

from .exchange_registry import EXCHANGE_FACTORIES

log = structlog.get_logger()

FETCH_TIMEOUT = 10
MAX_CONCURRENT_PER_EXCHANGE = 5

# 3 funding periods per day x 365 days -- converts an 8h rate to annualized APR.
# Assumes the standard 8h interval used by Binance/Bybit/OKX USDT perps.
# Exchanges with non-standard intervals (e.g. 4h or 1h) will show inflated APR.
PERIODS_PER_YEAR = 3 * 365

# Rate above which longs are paying heavily (0.1% per 8h = 109.5% APR).
ELEVATED_THRESHOLD = 0.001


async def _fetch_exchange_funding(name: str, bases: list[str]) -> list[dict[str, Any]]:
    """Fetch current funding rate per base symbol on a single exchange."""
    try:
        exchange = EXCHANGE_FACTORIES[name]()
    except Exception as exc:
        log.warning("funding.init_failed", exchange=name, err=str(exc))
        return []

    out: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(MAX_CONCURRENT_PER_EXCHANGE)

    async def _one(base: str) -> None:
        symbol = f"{base}/USDT:USDT"
        async with sem:
            try:
                data = await asyncio.wait_for(
                    exchange.fetch_funding_rate(symbol), timeout=FETCH_TIMEOUT
                )
            except Exception as exc:
                log.warning("funding.fetch_failed", exchange=name, base=base, err=str(exc))
                return
        try:
            rate = data.get("fundingRate")
            if rate is None:
                return
            out.append({"base": base, "exchange": name, "rate": float(rate)})
        except Exception as exc:
            log.warning("funding.normalize_failed", exchange=name, base=base, err=str(exc))

    try:
        await asyncio.gather(*[_one(base) for base in bases])
    finally:
        try:
            await exchange.close()
        except Exception as exc:
            log.warning("funding.close_failed", exchange=name, err=str(exc))
    return out


async def fetch_funding_rates_for_pumps(pumps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fetch current funding rate per (base, exchange) for each given pump entry.

    Groups by exchange so each exchange gets one reused connection per scan.
    Exchanges that don't support fetch_funding_rate are silently skipped via
    the per-symbol exception handler in _fetch_exchange_funding.
    """
    by_exchange: dict[str, list[str]] = {}
    for pump in pumps:
        base = pump["base"]
        for ex in pump.get("exchanges", []):
            name = ex["exchange"]
            if name not in EXCHANGE_FACTORIES:
                continue
            by_exchange.setdefault(name, []).append(base)

    if not by_exchange:
        return []

    results = await asyncio.gather(
        *[_fetch_exchange_funding(name, bases) for name, bases in by_exchange.items()],
        return_exceptions=True,
    )
    snapshots: list[dict[str, Any]] = []
    for name, result in zip(by_exchange, results, strict=True):
        if isinstance(result, BaseException):
            log.warning("funding.exchange_task_failed", exchange=name, err=str(result))
        else:
            snapshots.extend(result)
    log.info("funding.fetched", count=len(snapshots))
    return snapshots
