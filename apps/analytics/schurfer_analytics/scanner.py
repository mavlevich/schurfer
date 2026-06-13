import asyncio
import json
import time
from typing import Any

import ccxt.async_support as ccxt
import redis.asyncio as aioredis
import structlog

log = structlog.get_logger()

REDIS_KEY = "pumps:latest"
REDIS_TTL = 300  # 5 min — expire if scanner crashes

_SWAP: dict[str, Any] = {"enableRateLimit": True, "options": {"defaultType": "swap"}}

_FACTORIES: dict[str, Any] = {
    "binance": lambda: ccxt.binance(_SWAP),
    "bybit": lambda: ccxt.bybit(_SWAP),
    "okx": lambda: ccxt.okx(_SWAP),
    "gate": lambda: ccxt.gate(_SWAP),
    "bitget": lambda: ccxt.bitget(_SWAP),
    "mexc": lambda: ccxt.mexc(_SWAP),
    "kucoin": lambda: ccxt.kucoinfutures(_SWAP),
    "bingx": lambda: ccxt.bingx(_SWAP),
    "coinex": lambda: ccxt.coinex(_SWAP),
    "phemex": lambda: ccxt.phemex(_SWAP),
    "cryptocom": lambda: ccxt.cryptocom(_SWAP),
    "htx": lambda: ccxt.htx(_SWAP),
}


async def _fetch(
    name: str, exchange: Any, min_pct: float
) -> tuple[list[dict[str, Any]], str | None]:
    """Returns (results, error) — error is None on success."""
    try:
        if not exchange.has.get("fetchTickers"):
            log.warning("exchange.no_fetch_tickers", exchange=name)
            return [], None
        tickers: dict[str, Any] = await exchange.fetch_tickers()
        out = []
        for sym, t in tickers.items():
            if not sym.endswith("/USDT:USDT"):
                continue
            pct = t.get("percentage")
            if pct is None or float(pct) < min_pct:
                continue
            out.append(
                {
                    "base": sym.split("/")[0],
                    "exchange": name,
                    "symbol": t.get("info", {}).get("symbol", sym),
                    "price": str(t.get("last") or ""),
                    "change_pct": round(float(pct), 2),
                    "high_24h": str(t.get("high") or ""),
                    "volume_24h_usd": float(t.get("quoteVolume") or 0),
                }
            )
        log.info("exchange.scanned", exchange=name, pumps=len(out))
        return out, None
    except Exception as exc:
        log.warning("exchange.failed", exchange=name, err=str(exc))
        return [], str(exc)


def _dedup(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group entries by base asset, keep all exchanges, sort by max pump."""
    by_base: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        base = e["base"]
        row = {k: v for k, v in e.items() if k != "base"}
        by_base.setdefault(base, []).append(row)

    pumps = [
        {
            "base": base,
            "max_change_pct": max(r["change_pct"] for r in rows),
            "exchanges": sorted(rows, key=lambda r: r["change_pct"], reverse=True),
        }
        for base, rows in by_base.items()
    ]
    return sorted(pumps, key=lambda p: p["max_change_pct"], reverse=True)


async def run_once(
    exchange_names: list[str],
    min_pct: float,
    rdb: aioredis.Redis,
) -> int:
    """Scan all exchanges, deduplicate, store result in Redis. Returns pump count."""
    unknown = [n for n in exchange_names if n not in _FACTORIES]
    if unknown:
        log.warning("scanner.unknown_exchanges", unknown=unknown)

    exchanges = {n: _FACTORIES[n]() for n in exchange_names if n in _FACTORIES}
    if not exchanges:
        log.error("scanner.no_valid_exchanges")
        return 0

    try:
        results: list[tuple[list[dict[str, Any]], str | None]] = await asyncio.gather(
            *[_fetch(name, ex, min_pct) for name, ex in exchanges.items()]
        )
        errors: dict[str, str] = {}
        flat: list[dict[str, Any]] = []
        for name, (entries, err) in zip(exchanges, results, strict=True):
            if err is not None:
                errors[name] = err
            else:
                flat.extend(entries)

        # All sources failed — preserve the last known-good snapshot in Redis
        if errors and len(errors) == len(exchanges):
            log.error("scanner.all_failed", errors=errors)
            return 0

        pumps = _dedup(flat)
        payload = json.dumps(
            {
                "ts": int(time.time() * 1000),
                "count": len(pumps),
                "min_change_pct": min_pct,
                "pumps": pumps,
                "errors": errors,
                "scanned": [n for n in exchanges if n not in errors],
            }
        )
        await rdb.set(REDIS_KEY, payload, ex=REDIS_TTL)
        log.info("scanner.stored", count=len(pumps), min_pct=min_pct, failed=len(errors))
        return len(pumps)
    finally:
        await asyncio.gather(
            *[ex.close() for ex in exchanges.values()],
            return_exceptions=True,
        )
