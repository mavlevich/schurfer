import asyncio
from dataclasses import asdict
from typing import Any

import psycopg
import structlog
from psycopg.rows import dict_row

from . import journal, liquidity, paper
from .config import Config

log = structlog.get_logger()

_SCAN_INTERVAL = 60
_SIZE_USD = 100.0
_STRATEGY_VERSION = "2"

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


async def run_liquidation_cascade_scanner(exchanges: dict[str, Any], rdb: Any, cfg: Config) -> None:
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
                from . import symbols

                raw_symbol = c["symbol"]

                # We need the CCXT client to resolve exact symbol
                ex = exchanges.get("bybit")
                if not ex:
                    continue

                route = await symbols.resolve_route(
                    cfg.db_url,
                    source_exchange,
                    raw_symbol,
                    "bybit",
                )
                if not route:
                    log.warning(
                        "liquidation_cascade.unresolved_route",
                        source_exchange=source_exchange,
                        raw=raw_symbol,
                    )
                    continue
                try:
                    instrument = symbols.resolve_execution_instrument(
                        ex,
                        route.execution_native_id,
                    )
                except (RuntimeError, ValueError) as e:
                    log.warning(
                        "liquidation_cascade.unresolved_symbol",
                        raw=route.execution_native_id,
                        err=str(e),
                    )
                    continue

                # Fetch executable price from target exchange, not source bar
                try:
                    ticker = await ex.fetch_ticker(instrument.symbol)
                    last_price = float(ticker["last"])
                except Exception as e:
                    log.warning(
                        "liquidation_cascade.ticker_failed", symbol=instrument.symbol, err=str(e)
                    )
                    continue

                price_drop = (float(c["close_price"]) - float(c["price_15m_ago"])) / float(
                    c["price_15m_ago"]
                )
                oi_drop = (float(c["open_interest"]) - float(c["oi_15m_ago"])) / float(
                    c["oi_15m_ago"]
                )

                # Deduplicate: check if a trade is already open
                open_id = await journal.find_open_trade_id(
                    cfg.db_url, exchange="bybit", symbol=instrument.symbol
                )
                if open_id:
                    continue

                # v2: capture pre-trade order-book quality (colleague review) --
                # v1 never measured this at all, so entry_slippage_bps was
                # permanently None for every liquidation_cascade trade and net
                # accounting could never reach "complete" no matter what else
                # was fixed (verified against production: 0 of 26 v1 trades
                # ever had a market_quality snapshot). depth_target is the
                # gate's own safety margin (a multiple of the real size); the
                # entry itself is still priced off the plain ticker last_price
                # below, not a VWAP walk, so this is a genuine as-yet-unpaid
                # impact cost -- entry_price_includes_impact is deliberately
                # NOT set (unlike early_momentum's VWAP-priced entry).
                depth_target = liquidity.depth_target_usd(_SIZE_USD, cfg.liquidity_depth_multiplier)
                snap = await liquidity.snapshot(
                    ex, instrument.symbol, required_depth_usd=depth_target
                )
                quality = liquidity.check_market_quality(
                    snap,
                    target_usd=depth_target,
                    max_spread_bps=cfg.max_spread_bps,
                    max_impact_bps=cfg.max_liquidity_impact_bps,
                )
                if cfg.require_market_quality and not quality.allowed:
                    log.info(
                        "liquidation_cascade.market_quality_gate_skip",
                        symbol=instrument.symbol,
                        reason=quality.reason,
                    )
                    continue

                log.info(
                    "liquidation_cascade.trigger",
                    symbol=instrument.symbol,
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
                    instrument=instrument,
                    price=last_price,
                    size_usd=_SIZE_USD,
                    leverage=5,
                    score=100,
                    setup_context={
                        "strategy": f"liquidation_cascade_v{_STRATEGY_VERSION}",
                        "price_drop_pct": round(price_drop * 100, 2),
                        "oi_drop_pct": round(oi_drop * 100, 2),
                        "signal_source": source_exchange,
                        "source_symbol": raw_symbol,
                        "market_quality": asdict(quality),
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
