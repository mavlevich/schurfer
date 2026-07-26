import asyncio
import json
import math
import time
from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis
import structlog

from .exchange_registry import EXCHANGE_FACTORIES
from .instruments import instrument_metadata

log = structlog.get_logger()

REDIS_KEY = "pumps:latest"
REDIS_TTL = 300  # 5 min — expire if scanner crashes
MAX_TICKER_AGE_MS = 15 * 60 * 1000
MAX_TICKER_FUTURE_SKEW_MS = 5 * 60 * 1000


@dataclass(frozen=True)
class ScanBatch:
    """One complete exchange scan, not yet published to Redis."""

    pumps: list[dict[str, Any]]
    errors: dict[str, str]
    below_updates: dict[str, float]
    tracked_pumps: list[dict[str, Any]]
    scanned: tuple[str, ...]


def _positive_float(value: Any) -> float | None:
    """Return a finite positive float, otherwise None."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _volume_24h_usd(ticker: dict[str, Any]) -> tuple[float | None, str]:
    """Normalize 24h quote volume without turning unavailable data into zero."""
    quote_volume = _positive_float(ticker.get("quoteVolume"))
    if quote_volume is not None:
        return quote_volume, "quote_volume"
    return None, "unavailable"


def _ticker_timestamp_ms(name: str, ticker: dict[str, Any]) -> int | None:
    """Return unified ticker time, with a narrow LBank raw-field fallback."""
    value = ticker.get("timestamp")
    if value is None and name == "lbank":
        info = ticker.get("info")
        if isinstance(info, dict):
            value = info.get("lastTime")
            if value is not None:
                parsed = float(value)
                if not math.isfinite(parsed):
                    raise ValueError("non-finite LBank lastTime")
                # LBank contract tickers currently expose Unix seconds.
                value = parsed * 1000 if parsed < 1_000_000_000_000 else parsed
    if value is None:
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError("invalid ticker timestamp")
    return int(parsed)


async def _fetch(
    name: str,
    exchange: Any,
    min_pct: float,
    extra_bases: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Returns (above_threshold, below_threshold_tracked, error).

    extra_bases: tracked tokens to include even if below min_pct (watch-list).
    """
    try:
        if not exchange.has.get("fetchTickers"):
            log.warning("exchange.no_fetch_tickers", exchange=name)
            return [], [], None
        tickers: dict[str, Any] = await exchange.fetch_tickers()
        above: list[dict[str, Any]] = []
        below: list[dict[str, Any]] = []
        stale = 0
        inactive = 0
        now_ms = int(time.time() * 1000)
        markets = exchange.markets if isinstance(exchange.markets, dict) else {}
        for sym, t in tickers.items():
            if not sym.endswith("/USDT:USDT"):
                continue
            market = markets.get(sym)
            if market is not None:
                market_info = market.get("info")
                trading_disabled = (
                    isinstance(market_info, dict) and market_info.get("tradeSwitch") is False
                )
                if market.get("active") is False or trading_disabled:
                    inactive += 1
                    continue
            try:
                timestamp = _ticker_timestamp_ms(name, t)
            except (TypeError, ValueError):
                stale += 1
                continue
            if timestamp is not None:
                ticker_age_ms = now_ms - timestamp
                if ticker_age_ms > MAX_TICKER_AGE_MS or ticker_age_ms < -MAX_TICKER_FUTURE_SKEW_MS:
                    stale += 1
                    continue
            pct = t.get("percentage")
            if pct is None:
                continue
            pct_f = round(float(pct), 2)
            # Sanity cap: values above 5000% indicate a data error (e.g. BingX
            # stock-index futures reporting absolute price as a percentage).
            if abs(pct_f) > 5000:
                continue
            base = sym.split("/")[0]
            volume_24h_usd, volume_24h_source = _volume_24h_usd(t)
            ticker_info = t.get("info")
            ticker_info = ticker_info if isinstance(ticker_info, dict) else {}
            entry = {
                "base": base,
                "exchange": name,
                "symbol": ticker_info.get("symbol") or sym,
                "price": str(t.get("last") or ""),
                "change_pct": pct_f,
                "high_24h": str(t.get("high") or ""),
                "volume_24h_usd": volume_24h_usd,
                "volume_24h_source": volume_24h_source,
                "ticker_timestamp_ms": timestamp,
                # Point-in-time fact: when Schurfer finished observing this venue's
                # ticker batch. Unlike a rolling 24h high, this cannot be rebuilt.
                "observed_at_ms": now_ms,
                **instrument_metadata(name, sym, market),
            }
            if pct_f >= min_pct:
                above.append(entry)
            elif base in extra_bases:
                below.append(entry)
        log.info(
            "exchange.scanned",
            exchange=name,
            pumps=len(above),
            tracked=len(below),
            stale=stale,
            inactive=inactive,
        )
        return above, below, None
    except Exception as exc:
        log.warning("exchange.failed", exchange=name, err=str(exc))
        return [], [], str(exc)


def _aggregate_below_updates(
    flat_below: list[dict[str, Any]],
    live_bases: set[str],
) -> dict[str, float]:
    """Max current % per tracked base, excluding bases still live above threshold."""
    updates: dict[str, float] = {}
    for entry in flat_below:
        base = entry["base"]
        if base in live_bases:
            continue
        pct = entry["change_pct"]
        if pct > updates.get(base, float("-inf")):
            updates[base] = pct
    return updates


def _tracked_pumps(
    flat_below: list[dict[str, Any]],
    live_bases: set[str],
) -> list[dict[str, Any]]:
    """Pumps-shaped entries for tracked tokens that fell below threshold.

    Excludes bases still live above threshold (those are already in `pumps`).
    Used to keep recording OI through the retrace phase, not just while live.
    """
    return [p for p in _dedup(flat_below) if p["base"] not in live_bases]


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
    extra_bases: frozenset[str] = frozenset(),
) -> ScanBatch | None:
    """Scan all exchanges and return an unpublished batch.

    Persistence and publication are deliberately separate: the caller first creates
    or updates every pump_event, attaches its id, and only then publishes pumps:latest.
    On total exchange failure returns None so the last known-good Redis snapshot stays.
    """
    unknown = [n for n in exchange_names if n not in EXCHANGE_FACTORIES]
    if unknown:
        log.warning("scanner.unknown_exchanges", unknown=unknown)

    exchanges = {n: EXCHANGE_FACTORIES[n]() for n in exchange_names if n in EXCHANGE_FACTORIES}
    if not exchanges:
        log.error("scanner.no_valid_exchanges")
        return None

    try:
        results: list[
            tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]
        ] = await asyncio.gather(
            *[_fetch(name, ex, min_pct, extra_bases) for name, ex in exchanges.items()]
        )
        errors: dict[str, str] = {}
        flat: list[dict[str, Any]] = []
        flat_below: list[dict[str, Any]] = []
        for name, (above, below, err) in zip(exchanges, results, strict=True):
            if err is not None:
                errors[name] = err
            else:
                flat.extend(above)
                flat_below.extend(below)

        # All sources failed — preserve the last known-good snapshot in Redis
        if errors and len(errors) == len(exchanges):
            log.error("scanner.all_failed", errors=errors)
            return None

        pumps = _dedup(flat)
        live_bases = {p["base"] for p in pumps}
        below_updates = _aggregate_below_updates(flat_below, live_bases)
        tracked_pumps = _tracked_pumps(flat_below, live_bases)

        return ScanBatch(
            pumps=pumps,
            errors=errors,
            below_updates=below_updates,
            tracked_pumps=tracked_pumps,
            scanned=tuple(n for n in exchanges if n not in errors),
        )
    finally:
        await asyncio.gather(
            *[ex.close() for ex in exchanges.values()],
            return_exceptions=True,
        )


async def publish(batch: ScanBatch, min_pct: float, rdb: aioredis.Redis) -> None:
    """Atomically replace pumps:latest with a fully attributed scan batch."""
    published_at_ms = int(time.time() * 1000)
    payload = json.dumps(
        {
            # Keep ts for old consumers; published_at_ms names the event precisely.
            "ts": published_at_ms,
            "published_at_ms": published_at_ms,
            "count": len(batch.pumps),
            "min_change_pct": min_pct,
            "pumps": batch.pumps,
            "errors": batch.errors,
            "scanned": list(batch.scanned),
        }
    )
    await rdb.set(REDIS_KEY, payload, ex=REDIS_TTL)
    log.info(
        "scanner.stored",
        count=len(batch.pumps),
        min_pct=min_pct,
        failed=len(batch.errors),
    )
