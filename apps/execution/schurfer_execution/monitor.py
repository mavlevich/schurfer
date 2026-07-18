from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

import structlog

from . import exit as exit_module
from . import journal, notify
from .account import fetch_positions
from .orders import close_position

if TYPE_CHECKING:
    from .config import Config

log = structlog.get_logger()

_INTERVAL_SECONDS = 30
_TRADE_ID_KEY = "trade:id:{exchange}:{base}"
_SL_ORDER_KEY_PATTERN = "position:sl_order_id:*"


async def run_position_monitor(
    exchanges: dict[str, Any],
    rdb: Any,
    cfg: Config,
) -> None:
    while True:
        await asyncio.sleep(_INTERVAL_SECONDS)
        try:
            await _tick(exchanges, rdb, cfg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("position_monitor.error", err=str(e))


async def _tick(exchanges: dict[str, Any], rdb: Any, cfg: Config) -> None:
    positions, failed = await fetch_positions(exchanges)
    live_pairs = {
        (pos["exchange"], pos["base"]) for pos in positions if pos["exchange"] not in failed
    }
    for pos in positions:
        if pos["exchange"] in failed:
            continue
        try:
            await _check_exit(pos, rdb, cfg, exchanges)
        except Exception as e:
            log.error(
                "position_monitor.check_exit_error",
                base=pos.get("base"),
                exchange=pos.get("exchange"),
                err=str(e),
            )

    # An exchange-native stop-loss can fill on its own, outside of _check_exit's
    # control. When that happens the position simply vanishes from fetch_positions()
    # — nothing else notices. Reconcile any tracked position whose SL order id is
    # still in Redis but which no longer has a live exchange position.
    try:
        await _reconcile_vanished_positions(exchanges, rdb, cfg, live_pairs)
    except Exception as e:
        log.error("position_monitor.reconcile_scan_error", err=str(e))

    # Retry any close that was confirmed on the exchange but couldn't be
    # committed to the journal yet (DB was unreachable at the time).
    try:
        await _retry_pending_closes(rdb, cfg)
    except Exception as e:
        log.error("position_monitor.pending_close_scan_error", err=str(e))


async def _check_exit(
    position: dict[str, Any],
    rdb: Any,
    cfg: Config,
    exchanges: dict[str, Any],
) -> None:
    exchange = position["exchange"]
    base = position["base"]
    side = position["side"]
    entry = position["entry_price"]
    mark = position.get("mark_price", 0.0)

    if entry <= 0 or mark <= 0:
        return

    opened_at_raw = await rdb.get(f"position:opened_at:{exchange}:{base}")
    opened_at = float(opened_at_raw) if opened_at_raw else time.time()

    params_raw = await rdb.get(exit_module.params_key(exchange, base))
    params = exit_module.load_exit_params(params_raw)

    bp_key = exit_module.best_price_key(exchange, base)
    reason = await exit_module.check_exit(
        side=side,
        entry_price=entry,
        current_price=mark,
        opened_at=opened_at,
        params=params,
        rdb=rdb,
        bp_key=bp_key,
    )

    if not reason:
        return

    result = await close_position(
        exchanges=exchanges,
        exchange=exchange,
        base=base,
        reason=reason,
        rdb=rdb,
    )

    if result.get("closed"):
        exit_price = float(result.get("exit_price") or mark)
        # The exchange-side close is confirmed at this point regardless of the
        # journal write outcome below — safe to stop monitoring this position.
        await rdb.delete(bp_key)
        await rdb.delete(exit_module.params_key(exchange, base))

        trade_id_key = _TRADE_ID_KEY.format(exchange=exchange, base=base.upper())
        trade_id_raw = await rdb.get(trade_id_key)
        if trade_id_raw and cfg.db_url:
            trade_id = int(trade_id_raw)
            committed = await journal.try_commit_close(
                cfg.db_url,
                rdb,
                exchange=exchange,
                base=base.upper(),
                trade_id=trade_id,
                exit_order_id=result.get("order_id"),
                exit_price=exit_price,
                reason=reason,
            )
            # Only drop the pointer once the close is durably recorded, and only
            # if it still points at this trade — otherwise a DB outage at close
            # time permanently loses this trade's realized PnL, or a slow retry
            # could delete a newer trade's pointer for the same symbol.
            # If not committed, journal.try_commit_close already wrote a durable
            # journal:pending_close marker that _retry_pending_closes will retry.
            if committed:
                await journal.delete_trade_id_if_matches(rdb, trade_id_key, trade_id)
            else:
                log.error(
                    "position_monitor.journal_close_failed_pending_retry",
                    base=base,
                    exchange=exchange,
                    trade_id=trade_id,
                )

        creds = notify.credentials(cfg)
        if creds:
            pnl_pct_final = (
                (entry - exit_price) / entry * 100
                if side == "short"
                else (exit_price - entry) / entry * 100
            )
            await notify.notify_close(
                *creds,
                base=base,
                exchange=exchange,
                entry_price=entry,
                exit_price=exit_price,
                pnl_pct=pnl_pct_final,
                reason=reason,
                paper=False,
            )


def _parse_sl_key(key: str) -> tuple[str, str] | None:
    # position:sl_order_id:{exchange}:{base}
    parts = key.split(":")
    if len(parts) != 4:
        return None
    return parts[2], parts[3]


async def _reconcile_vanished_positions(
    exchanges: dict[str, Any],
    rdb: Any,
    cfg: Config,
    live_pairs: set[tuple[str, str]],
) -> None:
    async for raw_key in rdb.scan_iter(match=_SL_ORDER_KEY_PATTERN):
        key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
        parsed = _parse_sl_key(key)
        if parsed is None:
            continue
        exchange, base = parsed
        if (exchange, base) in live_pairs:
            continue  # position still open on the exchange — nothing to reconcile

        try:
            await _reconcile_one(exchange, base, exchanges, rdb, cfg)
        except Exception as e:
            log.error("position_monitor.reconcile_error", base=base, exchange=exchange, err=str(e))


async def _resolve_fill_price(ex: Any, symbol: str, order: dict[str, Any]) -> float | None:
    """Best-effort determination of what price a filled order actually
    executed at. Returns None (never 0) if it genuinely can't be determined —
    callers must not fabricate a price, since exit_price=0 on a short reads
    as a false +100% profit."""
    price = order.get("average") or order.get("price")
    if price:
        return float(price)
    try:
        trades = await ex.fetch_order_trades(order.get("id"), symbol)
    except Exception:
        return None
    if not trades:
        return None
    total_cost = sum(float(t.get("price", 0)) * float(t.get("amount", 0)) for t in trades)
    total_amount = sum(float(t.get("amount", 0)) for t in trades)
    if total_amount <= 0:
        return None
    return total_cost / total_amount


async def _reconcile_one(
    exchange: str,
    base: str,
    exchanges: dict[str, Any],
    rdb: Any,
    cfg: Config,
) -> None:
    sl_key = f"position:sl_order_id:{exchange}:{base}"
    sl_order_id_raw = await rdb.get(sl_key)
    if not sl_order_id_raw:
        return
    sl_order_id = (
        sl_order_id_raw.decode() if isinstance(sl_order_id_raw, bytes) else sl_order_id_raw
    )

    ex = exchanges.get(exchange)
    if not ex:
        return
    symbol = f"{base}/USDT:USDT"

    order = await ex.fetch_order(sl_order_id, symbol)
    if order.get("status") != "closed":
        # Not filled yet (still resting, canceled, etc.) — nothing to reconcile.
        # Could also mean the position was closed some other way while the SL
        # order is still resting; left for a human to clean up if it persists.
        log.warning(
            "position_monitor.reconcile.sl_not_filled",
            base=base,
            exchange=exchange,
            order_id=sl_order_id,
            status=order.get("status"),
        )
        return

    exit_price = await _resolve_fill_price(ex, symbol, order)
    if exit_price is None or exit_price <= 0:
        # Filled, but we can't determine at what price. Do NOT fabricate a
        # value (0 would read as a false +100% profit on a short) and do NOT
        # clean up any state — leave everything for the next tick, which will
        # re-check this same (still 'closed') order and try again. The real
        # PnL impact is unknown at this point, so any currently-valid
        # readiness lease is stale — revoke it now, don't wait for the
        # tracker's next tick to notice.
        await journal.revoke_pnl_readiness(rdb)
        log.error(
            "position_monitor.reconcile.exit_price_unresolved",
            base=base,
            exchange=exchange,
            order_id=sl_order_id,
        )
        return

    trade_id_key = _TRADE_ID_KEY.format(exchange=exchange, base=base)
    trade_id_raw = await rdb.get(trade_id_key)
    if not (trade_id_raw and cfg.db_url):
        # Nothing to commit to (no journal entry tracked, or no DB configured) —
        # still safe to stop monitoring this position below.
        log.warning(
            "position_monitor.reconcile.no_trade_id",
            base=base,
            exchange=exchange,
            order_id=sl_order_id,
        )

    # entry/side are read only for the notification below — best-effort, not
    # required for the journal commit itself, which loads them from the
    # trade's own DB row by trade_id. If this Redis cache was evicted, the
    # close is still fully recoverable.
    entry_raw = await rdb.get(exit_module.entry_key(exchange, base))
    side_raw = await rdb.get(exit_module.side_key(exchange, base))
    entry_price = float(entry_raw) if entry_raw else 0.0
    side = (side_raw.decode() if isinstance(side_raw, bytes) else side_raw) or "short"

    log.warning(
        "position_monitor.reconcile.exchange_sl_triggered",
        base=base,
        exchange=exchange,
        order_id=sl_order_id,
        exit_price=exit_price,
    )

    # The exchange-side event (SL fill) is confirmed at this point regardless
    # of the journal write outcome below — safe to stop monitoring this
    # position now. The trade-id pointer is handled separately below.
    await rdb.delete(sl_key)
    await rdb.delete(f"position:opened_at:{exchange}:{base}")
    await rdb.delete(exit_module.best_price_key(exchange, base))
    await rdb.delete(exit_module.params_key(exchange, base))
    await rdb.delete(exit_module.entry_key(exchange, base))
    await rdb.delete(exit_module.side_key(exchange, base))

    if trade_id_raw and cfg.db_url:
        trade_id = int(trade_id_raw)
        committed = await journal.try_commit_close(
            cfg.db_url,
            rdb,
            exchange=exchange,
            base=base,
            trade_id=trade_id,
            exit_order_id=sl_order_id,
            exit_price=exit_price,
            reason="exchange_stop_loss_triggered",
        )
        if committed:
            await journal.delete_trade_id_if_matches(rdb, trade_id_key, trade_id)
        else:
            log.error(
                "position_monitor.reconcile.journal_close_failed_pending_retry",
                base=base,
                exchange=exchange,
                trade_id=trade_id,
            )

    creds = notify.credentials(cfg)
    if creds and entry_price > 0:
        pnl_pct = (
            (entry_price - exit_price) / entry_price * 100
            if side == "short"
            else (exit_price - entry_price) / entry_price * 100
        )
        await notify.notify_close(
            *creds,
            base=base,
            exchange=exchange,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl_pct=pnl_pct,
            reason="exchange_stop_loss_triggered",
            paper=False,
        )


async def _retry_pending_closes(rdb: Any, cfg: Config) -> None:
    if not cfg.db_url:
        return
    async for raw_key in rdb.scan_iter(match=journal.pending_close_key_pattern()):
        key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
        parsed = journal.parse_pending_close_key(key)
        if parsed is None:
            continue
        exchange, base, trade_id = parsed
        try:
            await _retry_one_pending_close(exchange, base, trade_id, rdb, cfg)
        except Exception as e:
            log.error(
                "position_monitor.pending_close_retry_error",
                base=base,
                exchange=exchange,
                trade_id=trade_id,
                err=str(e),
            )


async def _retry_one_pending_close(
    exchange: str, base: str, trade_id: int, rdb: Any, cfg: Config
) -> None:
    key = f"journal:pending_close:{exchange}:{base}:{trade_id}"
    raw = await rdb.get(key)
    if not raw:
        return
    data = json.loads(raw)
    committed = await journal.try_commit_close(
        cfg.db_url,  # type: ignore[arg-type]
        rdb,
        exchange=exchange,
        base=base,
        trade_id=data["trade_id"],
        exit_order_id=data["exit_order_id"],
        exit_price=data["exit_price"],
        reason=data["reason"],
    )
    if committed:
        trade_id_key = _TRADE_ID_KEY.format(exchange=exchange, base=base)
        await journal.delete_trade_id_if_matches(rdb, trade_id_key, trade_id)
        log.info("position_monitor.pending_close_committed", base=base, exchange=exchange)
