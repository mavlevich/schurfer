import asyncio
from dataclasses import asdict
from typing import Any

import psycopg
import structlog
from psycopg.rows import dict_row

from . import execution_intent, journal, liquidity
from .config import Config
from .execution_intent import (
    Broker,
    ExecutionIntent,
    StrategyIdentity,
)

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


async def run_liquidation_cascade_scanner(
    exchanges: dict[str, Any],
    rdb: Any,
    cfg: Config,
    broker: Broker | None = None,
    tracker: Any = None,
) -> None:
    """Scans for Liquidation Cascades and immediately opens trades."""
    if not cfg.db_url:
        log.warning("liquidation_cascade.scanner_disabled", reason="no db_url")
        return

    if broker is None:
        mode = execution_intent.resolve_mode(cfg, execution_intent.STRATEGY_LIQUIDATION_CASCADE)
        broker = execution_intent.build_broker(mode, exchanges=exchanges)

    # LIQUIDATION_CASCADE_MODE=disabled must stop the scanner outright, not
    # just make the eventual broker.open() call reject after a full tick's
    # worth of DB/liquidity work already ran for nothing (colleague review,
    # P1). broker.mode is fixed for the process lifetime (resolved once,
    # above or by the caller), so this only needs checking once, before the
    # loop even starts.
    if broker.mode is execution_intent.TradingMode.DISABLED:
        log.info("liquidation_cascade.disabled")
        return

    while True:
        if tracker:
            tracker.tick_started()
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

                # Deterministic per-candidate key (bucket_start is this row's
                # own decision timestamp) -- unused by PaperBroker today, but
                # every ExecutionIntent requires one, and a future ShadowBroker
                # needs it to dedupe the same cascade re-emitted every scan
                # tick while the condition holds (colleague review).
                idempotency_key = (
                    f"liquidation_cascade:v{_STRATEGY_VERSION}:{instrument.exchange}:"
                    f"{instrument.native_market_id}:{c['bucket_start'].isoformat()}"
                )
                setup_context = {
                    "strategy": f"liquidation_cascade_v{_STRATEGY_VERSION}",
                    "price_drop_pct": round(price_drop * 100, 2),
                    "oi_drop_pct": round(oi_drop * 100, 2),
                    "signal_source": source_exchange,
                    "source_symbol": raw_symbol,
                    "market_quality": asdict(quality),
                }
                # journal.strategy_identity(setup_context) is the SAME pure
                # parser journal.open_trade will use to register this
                # trade's app.strategies row -- deriving StrategyIdentity
                # from it directly (rather than hand-building name/version
                # from _STRATEGY_VERSION) is what keeps the two from
                # silently disagreeing (colleague review; this file's own
                # values happened to already match, but duplicating the
                # rsplit("_v", 1) convention in two places is exactly what
                # let pump_short's copy drift out of sync).
                strategy_name, strategy_version = journal.strategy_identity(setup_context)
                intent = ExecutionIntent(
                    strategy=StrategyIdentity(name=strategy_name, version=strategy_version),
                    instrument=instrument,
                    side="long",
                    size_usd=_SIZE_USD,
                    leverage=5,
                    score=100,
                    setup_context=setup_context,
                    idempotency_key=idempotency_key,
                    price=last_price,
                    exit_params=exit_params,
                )
                result = await broker.open(intent, cfg=cfg, rdb=rdb)
                if result.status is execution_intent.ExecutionStatus.SHADOW_RECORDED:
                    # LIQUIDATION_CASCADE_MODE=shadow: evidence recorded, no
                    # position opened -- not a rejection. The same candidate
                    # can re-quote every scan tick while the condition holds
                    # (bounded by _SQL_SCANNER's 25-minute window); the
                    # deterministic decision_id ShadowBroker derives from
                    # idempotency_key collapses every repeat to one row via
                    # ON CONFLICT DO NOTHING, so this loop does not need its
                    # own dedup for that.
                    log.info("liquidation_cascade.shadow_recorded", symbol=instrument.symbol)
                elif result.status is not execution_intent.ExecutionStatus.PAPER_OPENED:
                    log.info(
                        "liquidation_cascade.broker_rejected",
                        symbol=instrument.symbol,
                        reason=result.reason,
                    )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if tracker:
                tracker.tick_failed(exc)
            log.error("liquidation_cascade.scanner_error", err=str(exc))
        else:
            if tracker:
                tracker.tick_succeeded()

        await asyncio.sleep(_SCAN_INTERVAL)
