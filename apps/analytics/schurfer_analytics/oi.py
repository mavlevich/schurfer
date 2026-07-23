"""Open interest fetching: per-exchange OI for live and tracked pumps."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from .exchange_registry import EXCHANGE_FACTORIES

log = structlog.get_logger()

# Bounds each network call so one slow/hanging exchange can't stall the scan loop.
# ccxt's enableRateLimit (set on every factory) already paces concurrent calls to
# a single exchange instance, so this only guards against outright hangs.
FETCH_TIMEOUT = 10

# Caps concurrent in-flight requests per exchange. ccxt's enableRateLimit paces
# *dispatch*, but doesn't cap how many requests are in flight at once — with
# many live/tracked symbols on one exchange, an unbounded gather() can still
# spike load and make a scan noisy. A small semaphore bounds that regardless
# of how many symbols are being polled.
MAX_CONCURRENT_PER_EXCHANGE = 5
OPEN_INTEREST_MAX_AGE_MS = 15 * 60 * 1000
OPEN_INTEREST_MAX_FUTURE_SKEW_MS = 5 * 60 * 1000

OpenInterestFetcher = Callable[[Any, str], Awaitable[dict[str, Any]]]


def _parse_xt_open_interest(response: Any, *, now_ms: int | None = None) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValueError("XT open interest response must be an object")

    return_code = response.get("returnCode")
    if isinstance(return_code, bool) or not isinstance(return_code, int | str):
        raise ValueError("XT open interest return code is invalid")
    try:
        return_code_int = int(return_code)
    except (TypeError, ValueError) as exc:
        raise ValueError("XT open interest return code is invalid") from exc
    if return_code_int != 0:
        message = response.get("msgInfo") or "unknown exchange error"
        error = response.get("error")
        if isinstance(error, dict) and error.get("msg"):
            message = error["msg"]
        raise ValueError(f"XT open interest error {return_code_int}: {message}")

    result = response.get("result")
    if not isinstance(result, dict):
        raise ValueError("XT open interest response is missing result")

    amount = result.get("openInterest")
    value_usd = result.get("openInterestUsd")
    timestamp = result.get("time")
    if amount is None and value_usd is None:
        raise ValueError("XT open interest response has no amount or USD value")
    if timestamp is None:
        raise ValueError("XT open interest response has no timestamp")

    try:
        timestamp_ms = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise ValueError("XT open interest timestamp is invalid") from exc
    if timestamp_ms <= 0:
        raise ValueError("XT open interest timestamp must be positive")
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    age_ms = current_ms - timestamp_ms
    if age_ms > OPEN_INTEREST_MAX_AGE_MS:
        raise ValueError(f"XT open interest response is stale by {age_ms} ms")
    if age_ms < -OPEN_INTEREST_MAX_FUTURE_SKEW_MS:
        raise ValueError(f"XT open interest timestamp is {-age_ms} ms in the future")

    return {
        "openInterestAmount": amount,
        "openInterestValue": value_usd,
        "timestamp": timestamp_ms,
    }


async def _fetch_xt_open_interest(exchange: Any, symbol: str) -> dict[str, Any]:
    market = exchange.market(symbol)
    response = await exchange.public_linear_get_future_market_v1_public_contract_open_interest(
        {"symbol": market["id"]}
    )
    return _parse_xt_open_interest(response)


OPEN_INTEREST_FALLBACKS: dict[str, OpenInterestFetcher] = {
    "xt": _fetch_xt_open_interest,
}


def _supports_native_open_interest(exchange: Any) -> bool:
    capabilities = getattr(exchange, "has", None)
    return isinstance(capabilities, dict) and bool(capabilities.get("fetchOpenInterest"))


async def _fetch_open_interest(name: str, exchange: Any, symbol: str) -> dict[str, Any]:
    if _supports_native_open_interest(exchange):
        response = await exchange.fetch_open_interest(symbol)
    else:
        fallback_fetcher = OPEN_INTEREST_FALLBACKS.get(name)
        if fallback_fetcher is None:
            raise NotImplementedError(f"{name} open interest is not supported")
        response = await fallback_fetcher(exchange, symbol)
    if not isinstance(response, dict):
        raise ValueError(f"{name} open interest response must be an object")
    return response


async def _fetch_exchange_oi(name: str, items: list[tuple[str, float]]) -> list[dict[str, Any]]:
    """items: (base, last_price) pairs to fetch OI for on this exchange."""
    exchange = EXCHANGE_FACTORIES[name]()
    out: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(MAX_CONCURRENT_PER_EXCHANGE)
    fallback_fetcher = OPEN_INTEREST_FALLBACKS.get(name)
    if fallback_fetcher is None and not _supports_native_open_interest(exchange):
        log.debug("oi.unsupported", exchange=name, symbols=len(items))
        await exchange.close()
        return []

    async def _one(base: str, price: float) -> None:
        symbol = f"{base}/USDT:USDT"
        async with sem:
            try:
                data = await asyncio.wait_for(
                    _fetch_open_interest(name, exchange, symbol), timeout=FETCH_TIMEOUT
                )
            except Exception as exc:
                log.warning("oi.fetch_failed", exchange=name, base=base, err=str(exc))
                return

        # Separate try: a malformed response (bad type, missing markets entry)
        # must only drop this one symbol, not raise out of gather() and take
        # down the whole scan loop.
        try:
            oi_usd = data.get("openInterestValue")
            if oi_usd is None:
                amount = data.get("openInterestAmount")
                if amount is not None and price > 0:
                    # openInterestAmount is in contracts, not always 1:1 with the
                    # base asset (e.g. OKX contractSize=0.01) — must scale first.
                    # exchange.markets is None until load_markets() succeeds at
                    # least once, so it can't be assumed to be a dict here.
                    markets = getattr(exchange, "markets", None) or {}
                    contract_size = markets.get(symbol, {}).get("contractSize") or 1
                    oi_usd = float(amount) * price * float(contract_size)
        except Exception as exc:
            log.warning("oi.normalize_failed", exchange=name, base=base, err=str(exc))
            return

        if oi_usd is not None:
            out.append({"base": base, "exchange": name, "oi_usd": float(oi_usd)})

    try:
        try:
            await asyncio.wait_for(exchange.load_markets(), timeout=FETCH_TIMEOUT)
        except Exception as exc:
            log.warning("oi.load_markets_failed", exchange=name, err=str(exc))
        await asyncio.gather(*[_one(base, price) for base, price in items])
    finally:
        await exchange.close()
    return out


async def fetch_oi_for_pumps(pumps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fetch current OI (USD) per (base, exchange) for each given pump entry.

    Accepts both live (above-threshold) and tracked (faded, still-open-episode)
    pump entries in the same pumps-shaped format — callers should pass both so
    OI keeps being recorded through the retrace phase, not just while pumping.
    Groups by exchange so each exchange gets one reused connection per scan
    instead of one per token.
    """
    by_exchange: dict[str, list[tuple[str, float]]] = {}
    for pump in pumps:
        base = pump["base"]
        for ex in pump.get("exchanges", []):
            name = ex["exchange"]
            if name not in EXCHANGE_FACTORIES:
                continue
            try:
                price = float(ex["price"])
            except (TypeError, ValueError):
                price = 0.0
            by_exchange.setdefault(name, []).append((base, price))

    if not by_exchange:
        return []

    results = await asyncio.gather(
        *[_fetch_exchange_oi(name, items) for name, items in by_exchange.items()]
    )
    snapshots = [row for rows in results for row in rows]
    log.info("oi.fetched", count=len(snapshots))
    return snapshots
