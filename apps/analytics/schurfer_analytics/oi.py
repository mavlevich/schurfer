"""Open interest fetching: per-exchange OI for live and tracked pumps."""

import asyncio
from typing import Any

import structlog

from .scanner import EXCHANGE_FACTORIES

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


async def _fetch_exchange_oi(name: str, items: list[tuple[str, float]]) -> list[dict[str, Any]]:
    """items: (base, last_price) pairs to fetch OI for on this exchange."""
    exchange = EXCHANGE_FACTORIES[name]()
    out: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(MAX_CONCURRENT_PER_EXCHANGE)

    async def _one(base: str, price: float) -> None:
        symbol = f"{base}/USDT:USDT"
        async with sem:
            try:
                data = await asyncio.wait_for(
                    exchange.fetch_open_interest(symbol), timeout=FETCH_TIMEOUT
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
