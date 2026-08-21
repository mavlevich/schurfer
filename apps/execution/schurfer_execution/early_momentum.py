import asyncio
import json
import time
from dataclasses import asdict
from typing import Any

import psycopg
import structlog
from psycopg.rows import dict_row

from . import journal, liquidity, paper
from .config import Config

log = structlog.get_logger()

_WATCH_PREFIX = "market:early_momentum:watch:{exchange}:{base}"
_SCAN_INTERVAL = 60
_TRIGGER_INTERVAL = 60
# Size of the paper trade this strategy opens on every breakout. Kept as a
# module constant (rather than only inline in the open_paper() call) since
# the entry liquidity gate below needs to size its depth check to the exact
# same notional.
_SIZE_USD = 100.0

# We need a robust CTE to find accumulation candidates for the last 120 minutes.
_SQL_SCANNER = """
WITH recent_bars AS (
    SELECT
        exchange,
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
        exchange,
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
        PARTITION BY exchange, symbol
        ORDER BY bucket_start
        ROWS BETWEEN 120 PRECEDING AND CURRENT ROW
    )
),
latest AS (
    -- Only take the single most recent row for each exchange+symbol to evaluate current state
    SELECT DISTINCT ON (exchange, symbol) *
    FROM rolling
    ORDER BY exchange, symbol, bucket_start DESC
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
                source_exchange = c["exchange"]
                raw_symbol = c["symbol"]
                ceiling = float(c["price_max_2h"])
                key = _WATCH_PREFIX.format(exchange=source_exchange, base=raw_symbol)

                # Check if we already have a watch for this base to avoid spamming logs
                exists = await rdb.exists(key)
                if not exists:
                    log.info(
                        "early_momentum.watch_added",
                        base=raw_symbol,
                        ceiling=ceiling,
                        oi_growth=round(
                            (c["open_interest"] - c["oi_start_2h"]) / c["oi_start_2h"] * 100, 2
                        ),
                    )

                # Write to redis with 60 minute TTL
                data = {
                    "ceiling": ceiling,
                    "symbol": c["symbol"],
                    "source_exchange": source_exchange,
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
    if not cfg.db_url:
        log.warning("early_momentum.trigger_disabled", reason="no db_url")
        return

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
                from . import symbols

                raw_symbol = (
                    key.decode("utf-8").split(":")[-1]
                    if isinstance(key, bytes)
                    else key.split(":")[-1]
                )
                source_exchange = data.get("source_exchange", "bybit")
                ceiling = data["ceiling"]

                route = await symbols.resolve_route(
                    cfg.db_url,
                    source_exchange,
                    raw_symbol,
                    exchange,
                )
                if not route:
                    log.warning(
                        "early_momentum.unresolved_route",
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
                        "early_momentum.unresolved_symbol",
                        raw=route.execution_native_id,
                        err=str(e),
                    )
                    continue

                ticker = tickers.get(instrument.symbol)
                if not ticker:
                    continue

                last_price = float(ticker.get("last") or 0)

                if last_price > ceiling:
                    # Deduplicate: check if a trade is already open
                    if cfg.db_url:
                        open_id = await journal.find_open_trade_id(
                            cfg.db_url, exchange=exchange, symbol=instrument.symbol
                        )
                        if open_id:
                            log.info("early_momentum.already_open", symbol=instrument.symbol)
                            await rdb.delete(key)
                            continue

                    # Breakout!

                    log.info(
                        "early_momentum.breakout",
                        base=raw_symbol,
                        ceiling=ceiling,
                        price=last_price,
                    )

                    # Delete the watch key so we don't trigger it again --
                    # this happens whether or not the entry below actually
                    # ends up filled; a thin book at this instant is not
                    # expected to still be thin the next time this base
                    # accumulates and breaks out.
                    await rdb.delete(key)

                    # LONG entry buys, so it prices off the ask side of a
                    # fresh order book at the actual requested notional --
                    # never the last-trade ticker print, which says nothing
                    # about what this size could actually fill at.
                    #
                    # depth_target (a multiple of the real size) is only the
                    # market-quality gate's safety margin -- it checks depth
                    # exists beyond just the immediate need. The VWAP actually
                    # priced and stored below must be measured at _SIZE_USD,
                    # the real trade size, not the gate's larger notional
                    # (colleague review, 2026-08-21: using depth_target there
                    # priced the entry as if it were twice the actual size).
                    depth_target = liquidity.depth_target_usd(
                        _SIZE_USD, cfg.liquidity_depth_multiplier
                    )
                    snap = await liquidity.snapshot(
                        ex, instrument.symbol, required_depth_usd=depth_target
                    )
                    quality = liquidity.check_market_quality(
                        snap,
                        target_usd=depth_target,
                        max_spread_bps=cfg.max_spread_bps,
                        max_impact_bps=cfg.max_liquidity_impact_bps,
                    )
                    if not quality.allowed:
                        log.info(
                            "early_momentum.market_quality_gate_skip",
                            base=raw_symbol,
                            symbol=instrument.symbol,
                            reason=quality.reason,
                        )
                        continue
                    entry_vwap, entry_impact_bps, entry_filled_usd = liquidity.quote_for_side(
                        snap, position_side="long", leg="entry", target_usd=_SIZE_USD
                    )
                    if entry_vwap is None:
                        log.warning(
                            "early_momentum.entry_quote_unavailable",
                            base=raw_symbol,
                            symbol=instrument.symbol,
                        )
                        continue

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

                    await paper.open_paper(
                        rdb,
                        instrument=instrument,
                        price=entry_vwap,
                        size_usd=_SIZE_USD,
                        leverage=5,
                        score=100,  # Synthetic score
                        setup_context={
                            # v2: clean-execution evidence cohort (executable
                            # entry quote, market-quality gate, side-aware
                            # exit accounting). Same signal/exit_params as v1
                            # -- this is a measurement change, not a trading
                            # rule change.
                            "strategy": "early_momentum_v2",
                            "breakout_price": ceiling,
                            "signal_source": source_exchange,
                            "source_symbol": data.get("symbol"),
                            # quality/market_quality reflects the gate's
                            # safety-margined depth_target notional; these
                            # two fields are the actual entry-side reading at
                            # the real trade size (_SIZE_USD), which
                            # entry_vwap above was priced from -- kept as
                            # first-class evidence rather than discarded
                            # (colleague review, 2026-08-21).
                            "market_quality": asdict(quality),
                            "entry_vwap_impact_bps": entry_impact_bps,
                            "entry_vwap_filled_usd": entry_filled_usd,
                            # entry_vwap above already walked the ask book to
                            # this notional, so its gap from mid IS the entry
                            # impact cost -- accounting_contract must not
                            # also charge market_quality's ask_impact_bps a
                            # second time on top of it (see journal.py).
                            "entry_price_includes_impact": True,
                        },
                        cfg=cfg,
                        side="long",
                        exit_params=exit_params,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("early_momentum.trigger_error", err=str(exc))

        await asyncio.sleep(_TRIGGER_INTERVAL)
