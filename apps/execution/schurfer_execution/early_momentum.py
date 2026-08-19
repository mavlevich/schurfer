import asyncio
import json
import time
from typing import Any

import psycopg
import structlog
from psycopg.rows import dict_row

from . import paper
from .config import Config

log = structlog.get_logger()

_WATCH_PREFIX = "market:early_momentum:watch:{base}"
_SCAN_INTERVAL = 60
_TRIGGER_INTERVAL = 60

# We need a robust CTE to find accumulation candidates for the last 120 minutes.
_SQL_SCANNER = """
WITH recent_bars AS (
    SELECT
        symbol,
        bucket_start,
        close_price,
        open_interest,
        buy_total_notional_usd,
        sell_total_notional_usd
    FROM timeseries.bybit_momentum_bars_1m
    WHERE bucket_start >= NOW() - INTERVAL '125 minutes'
      AND open_interest IS NOT NULL
),
rolling AS (
    SELECT
        symbol,
        bucket_start,
        close_price,
        open_interest,
        FIRST_VALUE(open_interest) OVER w AS oi_start_2h,
        MAX(close_price) OVER w AS price_max_2h,
        MIN(close_price) OVER w AS price_min_2h,
        SUM(buy_total_notional_usd) OVER w AS buy_vol_2h,
        SUM(sell_total_notional_usd) OVER w AS sell_vol_2h
    FROM recent_bars
    WINDOW w AS (
        PARTITION BY symbol
        ORDER BY bucket_start
        ROWS BETWEEN 120 PRECEDING AND CURRENT ROW
    )
),
latest AS (
    -- Only take the single most recent row for each symbol to evaluate current state
    SELECT DISTINCT ON (symbol) *
    FROM rolling
    ORDER BY symbol, bucket_start DESC
)
SELECT *
FROM latest
WHERE oi_start_2h > 0
  AND price_min_2h > 0
  AND (open_interest - oi_start_2h) / oi_start_2h > 0.05
  AND (buy_vol_2h - sell_vol_2h) > 0
  AND (price_max_2h - price_min_2h) / price_min_2h < 0.03;
"""


async def run_early_momentum_scanner(rdb: Any, cfg: Config) -> None:
    """Scans for accumulation candidates and adds them to a watch list."""
    if not cfg.db_url:
        log.warning("early_momentum.scanner_disabled", reason="no db_url")
        return

    while True:
        try:
            async with (
                await psycopg.AsyncConnection.connect(cfg.db_url) as conn,
                conn.cursor(row_factory=dict_row) as cur,
            ):
                await cur.execute(_SQL_SCANNER)
                candidates = await cur.fetchall()

            for c in candidates:
                base = c["symbol"].split("/")[0]  # e.g. "BTC/USDT:USDT" -> "BTC"
                ceiling = float(c["price_max_2h"])
                key = _WATCH_PREFIX.format(base=base)

                # Check if we already have a watch for this base to avoid spamming logs
                exists = await rdb.exists(key)
                if not exists:
                    log.info(
                        "early_momentum.watch_added",
                        base=base,
                        ceiling=ceiling,
                        oi_growth=round(
                            (c["open_interest"] - c["oi_start_2h"]) / c["oi_start_2h"] * 100, 2
                        ),
                    )

                # Write to redis with 60 minute TTL
                data = {
                    "ceiling": ceiling,
                    "symbol": c["symbol"],
                    "bucket_start": str(c["bucket_start"]),
                    "added_at": time.time(),
                }
                await rdb.set(key, json.dumps(data), ex=3600)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("early_momentum.scanner_error", err=str(exc))

        await asyncio.sleep(_SCAN_INTERVAL)


async def run_early_momentum_trigger(exchanges: dict[str, Any], rdb: Any, cfg: Config) -> None:
    """Polls watched bases, checking for breakout to trigger paper trades."""
    exchange = "bybit"  # Hardcoded for now since momentum flow is Bybit only

    while True:
        try:
            keys = [k async for k in rdb.scan_iter("market:early_momentum:watch:*")]
            if not keys:
                await asyncio.sleep(_TRIGGER_INTERVAL)
                continue

            ex = exchanges.get(exchange)
            if not ex:
                log.error("early_momentum.exchange_not_found", exchange=exchange)
                await asyncio.sleep(_TRIGGER_INTERVAL)
                continue

            # Optimize by fetching all tickers at once (ccxt supports this on some exchanges)
            # or just iterate. Since Bybit has a global ticker endpoint:
            tickers = await ex.fetch_tickers()

            for key in keys:
                raw = await rdb.get(key)
                if not raw:
                    continue

                data = json.loads(raw)
                base = (
                    key.decode("utf-8").split(":")[-1]
                    if isinstance(key, bytes)
                    else key.split(":")[-1]
                )
                symbol = data["symbol"]
                ceiling = data["ceiling"]

                ticker = tickers.get(symbol)
                if not ticker:
                    continue

                last_price = float(ticker.get("last") or 0)

                if last_price > ceiling:
                    # Breakout!
                    log.info(
                        "early_momentum.breakout", base=base, ceiling=ceiling, price=last_price
                    )

                    # Delete the watch key so we don't trigger it again
                    await rdb.delete(key)

                    # Hardcoded best parameters from backtest
                    exit_params = {
                        "initial_sl_pct": 10.0,
                        "activation_pct": 10.0,  # Not used since TP hits first, but required
                        "trail_pct": 10.0,
                        "trail_tighten_pct": 10.0,
                        "tighten_after_min": 240.0,
                        "max_hold_min": 240.0,  # 4 hours
                        "take_profit_pct": 4.0,  # The holy grail parameter
                    }

                    # Size of paper trade - standard $100
                    await paper.open_paper(
                        rdb,
                        base=base,
                        exchange=exchange,
                        price=last_price,
                        size_usd=100.0,
                        leverage=2,
                        score=100,  # Synthetic score
                        setup_context={"strategy": "early_momentum_v1", "breakout_price": ceiling},
                        cfg=cfg,
                        side="long",
                        exit_params=exit_params,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("early_momentum.trigger_error", err=str(exc))

        await asyncio.sleep(_TRIGGER_INTERVAL)
