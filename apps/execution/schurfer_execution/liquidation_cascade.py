import asyncio
from typing import Any

import psycopg
import structlog
from psycopg.rows import dict_row

from apps.execution.schurfer_execution import journal

from . import paper
from .config import Config

log = structlog.get_logger()

_SCAN_INTERVAL = 60

# We look for a 5%+ price drop and 15%+ OI drop over a 15-minute window.
_SQL_SCANNER = """
WITH recent_bars AS (
    SELECT
        exchange,
        symbol,
        bucket_start,
        close_price,
        open_interest
    FROM timeseries.bybit_momentum_bars_1m
    WHERE bucket_start >= NOW() - INTERVAL '25 minutes'
      AND open_interest IS NOT NULL
),
rolling AS (
    SELECT
        exchange,
        symbol,
        bucket_start,
        close_price,
        open_interest,
        LAG(close_price, 15) OVER w AS price_15m_ago,
        LAG(open_interest, 15) OVER w AS oi_15m_ago
    FROM recent_bars
    WINDOW w AS (
        PARTITION BY exchange, symbol
        ORDER BY bucket_start
    )
),
latest AS (
    SELECT DISTINCT ON (exchange, symbol) *
    FROM rolling
    ORDER BY exchange, symbol, bucket_start DESC
)
SELECT *
FROM latest
WHERE price_15m_ago > 0
  AND oi_15m_ago > 0
  AND (close_price - price_15m_ago) / price_15m_ago <= -0.05
  AND (open_interest - oi_15m_ago) / oi_15m_ago <= -0.15;
"""


async def run_liquidation_cascade_scanner(rdb: Any, cfg: Config) -> None:
    """Scans for Liquidation Cascades and immediately opens trades."""
    if not cfg.db_url:
        log.warning("liquidation_cascade.scanner_disabled", reason="no db_url")
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
                source_exchange = c["exchange"]
                symbol = c["symbol"]
                base = symbol.split("/")[
                    0
                ]  # e.g. "BTC/USDT:USDT" -> "BTC", or "BTCUSDT" -> "BTCUSDT" (clean native)

                # Natively from Binance or Bybit it might be just 'BTCUSDT' in the DB
                if "/" not in base and base.endswith("USDT"):
                    base = base[:-4]

                last_price = float(c["close_price"])
                price_drop = (last_price - float(c["price_15m_ago"])) / float(c["price_15m_ago"])
                oi_drop = (float(c["open_interest"]) - float(c["oi_15m_ago"])) / float(
                    c["oi_15m_ago"]
                )

                # Deduplicate: check if a trade is already open
                open_id = await journal.find_open_trade_id(cfg.db_url, exchange="bybit", base=base)
                if open_id:
                    continue

                log.info(
                    "liquidation_cascade.trigger",
                    base=base,
                    price=last_price,
                    price_drop_pct=round(price_drop * 100, 2),
                    oi_drop_pct=round(oi_drop * 100, 2),
                )

                # Based on the ML Grid Search optimal parameters
                exit_params = {
                    "initial_sl_pct": 3.0,
                    "activation_pct": 10.0,  # Required by schema, TP hits first
                    "trail_pct": 10.0,
                    "trail_tighten_pct": 10.0,
                    "tighten_after_min": 60.0,
                    "max_hold_min": 60.0,  # 1 hour optimal hold
                    "take_profit_pct": 5.0,  # 5% target
                }

                await paper.open_paper(
                    rdb,
                    base=base,
                    exchange="bybit",  # Execute on Bybit
                    price=last_price,
                    size_usd=100.0,
                    leverage=5,
                    score=100,
                    setup_context={
                        "strategy": "liquidation_cascade_v1",
                        "price_drop_pct": round(price_drop * 100, 2),
                        "oi_drop_pct": round(oi_drop * 100, 2),
                        "signal_source": source_exchange,
                    },
                    cfg=cfg,
                    side="long",
                    exit_params=exit_params,
                )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("liquidation_cascade.scanner_error", err=str(exc))

        await asyncio.sleep(_SCAN_INTERVAL)
