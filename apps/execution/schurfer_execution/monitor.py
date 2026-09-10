from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from . import exit as exit_module
from . import incidents, journal, notify, order_attempts, symbols
from .account import fetch_positions
from .fill_price import FILL_NONE, FILL_UNRESOLVED, order_is_terminal, resolve_fill_price
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
    tracker: Any = None,
) -> None:
    while True:
        if tracker:
            tracker.tick_started()
        await asyncio.sleep(_INTERVAL_SECONDS)
        try:
            await _tick(exchanges, rdb, cfg)
            if tracker:
                tracker.tick_succeeded()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if tracker:
                tracker.tick_failed(e)
            log.error("position_monitor.error", err=str(e))


async def _tick(exchanges: dict[str, Any], rdb: Any, cfg: Config) -> None:
    blocked_pairs: set[tuple[str, str]] = set()
    if cfg.db_url:
        try:
            blocked_pairs = await _recover_close_attempts(exchanges, rdb, cfg)
        except Exception as exc:
            # A durable close intent whose exchange outcome cannot be audited
            # must block duplicate exit submission for that instrument.
            log.error("position_monitor.close_attempt_recovery_failed", err=str(exc))

    positions, failed = await fetch_positions(exchanges)
    live_pairs = {
        (pos["exchange"], pos["base"]) for pos in positions if pos["exchange"] not in failed
    }
    for pos in positions:
        if pos["exchange"] in failed:
            continue
        if (pos["exchange"], pos["base"]) in blocked_pairs:
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


def _order_client_id(order: dict[str, Any]) -> str | None:
    info = order.get("info")
    info = info if isinstance(info, dict) else {}
    value = (
        order.get("clientOrderId")
        or info.get("clientOrderId")
        or info.get("clientOid")
        or info.get("orderLinkId")
    )
    return str(value) if value is not None else None


async def _find_close_attempt_order(
    ex: Any, attempt: order_attempts.CloseAttempt
) -> dict[str, Any] | None:
    has = ex.has if isinstance(ex.has, dict) else {}
    if attempt.order_id and has.get("fetchOrder"):
        try:
            order = await ex.fetch_order(attempt.order_id, attempt.symbol)
            if isinstance(order, dict):
                return order
        except Exception as exc:
            log.warning(
                "position_monitor.close_attempt.fetch_order_failed",
                attempt_id=attempt.id,
                exchange=attempt.exchange,
                err=str(exc),
            )
    for capability, method_name in (
        ("fetchOpenOrders", "fetch_open_orders"),
        ("fetchClosedOrders", "fetch_closed_orders"),
    ):
        if not has.get(capability):
            continue
        try:
            orders = await getattr(ex, method_name)(attempt.symbol)
        except Exception as exc:
            log.warning(
                "position_monitor.close_attempt.fetch_orders_failed",
                attempt_id=attempt.id,
                exchange=attempt.exchange,
                capability=capability,
                err=str(exc),
            )
            continue
        for order in orders:
            if isinstance(order, dict) and _order_client_id(order) == attempt.client_order_id:
                return order
    return None


async def _recover_close_attempts(
    exchanges: dict[str, Any], rdb: Any, cfg: Config
) -> set[tuple[str, str]]:
    """Resolve close intents left between exchange and local durable boundaries.

    The return value contains instruments whose outstanding order could not yet
    be classified.  The normal exit loop skips those pairs for this tick so it
    cannot submit a duplicate close while the first outcome is still unknown.
    """
    assert cfg.db_url
    blocked: set[tuple[str, str]] = set()
    for attempt in await order_attempts.load_recoverable_close_attempts(cfg.db_url):
        pair = (attempt.exchange, attempt.base)
        ex = exchanges.get(attempt.exchange)
        if ex is None:
            blocked.add(pair)
            continue
        if attempt.status == order_attempts.STATUS_COMPLETED and attempt.trade_id is not None:
            aggregate_price = await journal.aggregate_close_fill_price(
                cfg.db_url, trade_id=attempt.trade_id
            )
            if aggregate_price is None:
                blocked.add(pair)
                await journal.revoke_pnl_readiness(rdb)
                continue
            await _finalize_recovered_close(
                attempt=attempt,
                ex=ex,
                rdb=rdb,
                cfg=cfg,
                trade_id=attempt.trade_id,
                order_id=attempt.order_id or attempt.client_order_id,
                aggregate_price=aggregate_price,
            )
            continue
        order = await _find_close_attempt_order(ex, attempt)
        if order is None:
            blocked.add(pair)
            continue
        if order.get("id") is not None and attempt.order_id is None:
            await order_attempts.mark_accepted(cfg.db_url, attempt.id, order_id=str(order["id"]))
        resolution = await resolve_fill_price(
            ex,
            symbol=attempt.symbol,
            order=order,
            requested_amount=attempt.requested_amount,
        )
        if resolution.status == FILL_UNRESOLVED:
            blocked.add(pair)
            continue
        if resolution.status == FILL_NONE:
            if order_is_terminal(order):
                await order_attempts.mark_failed(
                    cfg.db_url,
                    attempt.id,
                    error=f"exchange reports no fill for close order {order.get('id')}",
                )
            else:
                blocked.add(pair)
            continue

        position_side = str(attempt.context.get("position_side") or "")
        try:
            positions = await ex.fetch_positions()
        except Exception:
            blocked.add(pair)
            continue
        remaining = sum(
            float(position.get("contracts") or 0)
            for position in positions
            if position.get("symbol") == attempt.symbol
            and position.get("side") == position_side
            and float(position.get("contracts") or 0) > 0
        )
        filled_amount = resolution.filled_amount
        if filled_amount is None:
            filled_amount = max(0.0, attempt.requested_amount - remaining)
        if filled_amount <= 0 or resolution.price is None:
            blocked.add(pair)
            continue

        trade_id = attempt.trade_id
        if trade_id is None:
            trade_id = await journal.find_open_trade_id(
                cfg.db_url,
                exchange=attempt.exchange,
                symbol=attempt.symbol,
            )
        if trade_id is None:
            blocked.add(pair)
            await journal.revoke_pnl_readiness(rdb)
            continue
        tolerance = max(attempt.requested_amount * 0.001, 1e-12)
        position_terminal = remaining <= tolerance
        order_filled_in_full = filled_amount >= attempt.requested_amount - tolerance
        terminal = position_terminal and order_filled_in_full
        leg_remaining = (
            0.0 if terminal else max(remaining, max(0.0, attempt.requested_amount - filled_amount))
        )
        if not terminal and not order_is_terminal(order):
            # A cumulative partial fill can still grow. Do not freeze it into
            # the append-only ledger or release this pair for another close
            # until the original order itself is terminal.
            blocked.add(pair)
            await journal.revoke_pnl_readiness(rdb)
            continue
        executed_at = resolution.executed_at or attempt.created_at
        execution_time_source = resolution.execution_time_source
        if execution_time_source is None and attempt.created_at is not None:
            execution_time_source = "local.close_attempt_created_at"
        aggregate_price = await journal.record_close_fill(
            cfg.db_url,
            trade_id=trade_id,
            exchange=attempt.exchange,
            order_id=str(order.get("id") or attempt.client_order_id),
            fill_price=resolution.price,
            filled_amount=filled_amount,
            requested_amount=attempt.requested_amount,
            remaining_amount=leg_remaining,
            terminal=terminal,
            fill_source=resolution.source,
            executed_at=executed_at,
            execution_time_source=execution_time_source,
        )
        if aggregate_price is None:
            blocked.add(pair)
            await journal.revoke_pnl_readiness(rdb)
            continue
        if not terminal:
            partial_recorded = await order_attempts.mark_partial(
                cfg.db_url,
                attempt.id,
                trade_id=trade_id,
                filled_amount=filled_amount,
            )
            await journal.revoke_pnl_readiness(rdb)
            if not partial_recorded:
                blocked.add(pair)
            continue

        await order_attempts.mark_completed(
            cfg.db_url,
            attempt.id,
            trade_id=trade_id,
            filled_amount=filled_amount,
        )

        await _finalize_recovered_close(
            attempt=attempt,
            ex=ex,
            rdb=rdb,
            cfg=cfg,
            trade_id=trade_id,
            order_id=str(order.get("id") or attempt.client_order_id),
            aggregate_price=aggregate_price,
        )
    return blocked


async def _finalize_recovered_close(
    *,
    attempt: order_attempts.CloseAttempt,
    ex: Any,
    rdb: Any,
    cfg: Config,
    trade_id: int,
    order_id: str,
    aggregate_price: float,
) -> None:
    """Finish local state from a durable terminal close leg."""
    assert cfg.db_url

    sl_key = f"position:sl_order_id:{attempt.exchange}:{attempt.base}"
    sl_order_id = attempt.context.get("sl_order_id")
    if sl_order_id:
        try:
            await ex.cancel_order(str(sl_order_id), attempt.symbol)
        except Exception as exc:
            log.error(
                "position_monitor.close_attempt.sl_cancel_failed",
                attempt_id=attempt.id,
                order_id=sl_order_id,
                err=str(exc),
            )
        else:
            await rdb.delete(sl_key)
    await rdb.delete(f"position:opened_at:{attempt.exchange}:{attempt.base}")
    await rdb.delete(exit_module.best_price_key(attempt.exchange, attempt.base))
    await rdb.delete(exit_module.params_key(attempt.exchange, attempt.base))
    await rdb.delete(exit_module.entry_key(attempt.exchange, attempt.base))
    await rdb.delete(exit_module.side_key(attempt.exchange, attempt.base))
    await rdb.delete(exit_module.size_usd_key(attempt.exchange, attempt.base))

    reason = str(attempt.context.get("reason") or "recovered_close")
    executed_at, execution_time_source = await journal.terminal_close_execution_time(
        cfg.db_url, trade_id=trade_id
    )
    if executed_at is None and attempt.created_at is not None:
        executed_at = attempt.created_at
        execution_time_source = "local.close_attempt_created_at"
    committed = await journal.try_commit_close(
        cfg.db_url,
        rdb,
        exchange=attempt.exchange,
        base=attempt.base,
        trade_id=trade_id,
        exit_order_id=order_id,
        exit_price=aggregate_price,
        reason=reason,
        executed_at=executed_at,
        execution_time_source=execution_time_source,
    )
    if committed:
        trade_id_key = _TRADE_ID_KEY.format(exchange=attempt.exchange, base=attempt.base)
        await journal.delete_trade_id_if_matches(rdb, trade_id_key, trade_id)
    log.warning(
        "position_monitor.close_attempt.recovered_terminal",
        attempt_id=attempt.id,
        exchange=attempt.exchange,
        base=attempt.base,
        journal_committed=committed,
    )


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
    opened_at = float(opened_at_raw) if opened_at_raw else None
    if opened_at is None and cfg.db_url:
        recovered_entry_at = await journal.find_open_trade_entry_at(
            cfg.db_url, exchange=exchange, symbol=position["symbol"]
        )
        if recovered_entry_at is not None:
            opened_at = recovered_entry_at.timestamp()
            await rdb.set(
                f"position:opened_at:{exchange}:{base}",
                str(opened_at),
                ex=86400 * 7,
            )
            log.warning(
                "position_monitor.opened_at_recovered",
                base=base,
                exchange=exchange,
                source="journal.entry_at",
            )
    if opened_at is None:
        # Continue price/protection servicing, but never guess an age.  An
        # elapsed value of zero deliberately disables age-based exits for
        # this tick while TP/SL/trailing checks still run.
        opened_at = time.time()
        log.error(
            "position_monitor.opened_at_unresolved",
            base=base,
            exchange=exchange,
            age_exit_suppressed=True,
        )

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
        symbol=position["symbol"],
        reason=reason,
        rdb=rdb,
        cfg=cfg,
    )

    if result.get("closed"):
        # The exchange-side close is confirmed at this point regardless of the
        # journal write outcome below — safe to stop monitoring this position.
        await rdb.delete(bp_key)
        await rdb.delete(exit_module.params_key(exchange, base))

        exit_price = result.get("exit_price")
        if exit_price is None:
            # close_position already created a durable incident and revoked PnL
            # readiness. Never fabricate a price from the current mark to keep
            # this notify/journal path running — the incident worker completes
            # the journal close once resolve_fill_price confirms a real price.
            log.warning(
                "position_monitor.close_fill_unresolved",
                base=base,
                exchange=exchange,
                incident_id=result.get("incident_id"),
            )
            return
        exit_price = float(exit_price)

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
                executed_at=result.get("executed_at"),
                execution_time_source=result.get("execution_time_source"),
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
        elif cfg.db_url:
            # No trade-id pointer at close time — most likely the matching
            # open's own journal write is itself still deferred behind an
            # unresolved-fill incident (or the Redis cache was evicted).
            # Losing this close silently would leave a real exit unrecorded
            # forever; track it the same durable way as an unresolved fill so
            # the incident worker can complete it once a trade_id is
            # available (see incidents.has_pending_open / _complete_close).
            await journal.revoke_pnl_readiness(rdb)
            incident_id = await incidents.create_incident(
                cfg.db_url,
                exchange=exchange,
                base=base,
                operation="close",
                order_id=str(result.get("order_id") or f"unknown:{uuid.uuid4()}"),
                trade_id=None,
                context={"reason": reason},
            )
            if incident_id is not None and await incidents.claim_creation_notification(
                cfg.db_url, incident_id
            ):
                creds = notify.credentials(cfg)
                if creds:
                    await notify.notify_alert(
                        *creds,
                        text=(
                            f"Close confirmed for {base} on {exchange} but no trade-id "
                            f"pointer was available to journal it against (order "
                            f"{result.get('order_id')}). Tracking as incident {incident_id} "
                            "until a trade can be found."
                        ),
                    )
            log.error(
                "position_monitor.close_missing_trade_id",
                base=base,
                exchange=exchange,
                incident_id=incident_id,
            )

        creds = notify.credentials(cfg)
        if creds:
            pnl_pct_final = (
                (entry - exit_price) / entry * 100
                if side == "short"
                else (exit_price - entry) / entry * 100
            )
            # fetch_positions' own size_usd is the position's CURRENT
            # mark-to-market notional (contracts * mark_price), not the
            # entry-time size -- for a short in profit, price has dropped,
            # so that notional has already shrunk with it. Multiplying the
            # shrunk notional by the percent gain understates the real
            # dollar profit (and the mirror-image overstates a loss). The
            # entry-time size cached at open (same value notify_open showed)
            # is the correct multiplier; read it the same way the
            # reconcile path already does.
            size_usd_raw = await rdb.get(exit_module.size_usd_key(exchange, base))
            size_usd = float(size_usd_raw) if size_usd_raw else None
            pnl_usd_final = size_usd * pnl_pct_final / 100 if size_usd else None
            await notify.notify_close(
                *creds,
                base=base,
                exchange=exchange,
                entry_price=entry,
                exit_price=exit_price,
                pnl_pct=pnl_pct_final,
                pnl_usd=pnl_usd_final,
                reason=reason,
                paper=False,
            )
        await rdb.delete(exit_module.entry_key(exchange, base))
        await rdb.delete(exit_module.side_key(exchange, base))
        await rdb.delete(exit_module.size_usd_key(exchange, base))


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
    try:
        instrument = symbols.resolve_execution_instrument(ex, base)
        symbol = instrument.symbol
    except (RuntimeError, ValueError) as e:
        log.warning("position_monitor.unresolved_symbol", base=base, err=str(e))
        return

    order = await ex.fetch_order(sl_order_id, symbol)
    if order.get("status") != "closed":
        # This function is reached only after the exchange snapshot proved the
        # position vanished.  A still-resting reduce-only stop is therefore a
        # cleanup artifact from a terminal market close whose first cancel
        # failed.  Retry the cancel and retain its Redis id on failure so it
        # can never be orphaned onto a later position.
        try:
            await ex.cancel_order(sl_order_id, symbol)
        except Exception as exc:
            log.error(
                "position_monitor.reconcile.dangling_sl_cancel_failed",
                base=base,
                exchange=exchange,
                order_id=sl_order_id,
                status=order.get("status"),
                err=str(exc),
            )
            return
        await rdb.delete(sl_key)
        log.info(
            "position_monitor.reconcile.dangling_sl_cancelled",
            base=base,
            exchange=exchange,
            order_id=sl_order_id,
        )
        return

    resolution = await resolve_fill_price(ex, symbol=symbol, order=order)
    if resolution.status == FILL_NONE:
        # The stop order reports status "closed" with no executed volume, which
        # is self-contradictory: either it did not really fill or the exchange
        # payload is wrong. Do not commit a close off it (ENG-022 / audit C-3).
        # Left for the next reconciliation tick, which reads the live position
        # rather than this order.
        log.error(
            "position_monitor.reconcile.sl_closed_without_fill",
            base=base,
            exchange=exchange,
            order_id=sl_order_id,
        )
        return

    if resolution.status == FILL_UNRESOLVED:
        # Filled, but we can't determine at what price. Do NOT fabricate a
        # value (0 would read as a false +100% profit on a short). Unlike the
        # old ad-hoc retry, this is now a durable, alerted incident instead of
        # a silent in-memory retry that could loop forever with no one
        # noticing — see incidents.create_incident.
        await journal.revoke_pnl_readiness(rdb)
        trade_id_for_incident_raw = await rdb.get(f"trade:id:{exchange}:{base}")
        try:
            trade_id_for_incident = (
                int(trade_id_for_incident_raw) if trade_id_for_incident_raw else None
            )
        except (TypeError, ValueError):
            # A corrupt/unexpected cache value must not crash reconciliation —
            # the incident worker can still find the trade later by exchange+base.
            trade_id_for_incident = None
        incident_id = None
        if cfg.db_url:
            incident_id = await incidents.create_incident(
                cfg.db_url,
                exchange=exchange,
                base=base,
                operation="close",
                order_id=sl_order_id,
                trade_id=trade_id_for_incident,
                context={"reason": "exchange_stop_loss_triggered"},
            )
            if incident_id is not None and await incidents.claim_creation_notification(
                cfg.db_url, incident_id
            ):
                creds = notify.credentials(cfg)
                if creds:
                    await notify.notify_alert(
                        *creds,
                        text=(
                            f"Fill price unresolved for exchange-triggered stop-loss "
                            f"on {base}/{exchange} (order {sl_order_id}). PnL is "
                            f"unknown until reconciled. See incident {incident_id}."
                        ),
                    )
        log.error(
            "position_monitor.reconcile.exit_price_unresolved",
            base=base,
            exchange=exchange,
            order_id=sl_order_id,
            incident_id=incident_id,
        )
        return
    exit_price = resolution.price
    # FILL_UNRESOLVED and FILL_NONE, the two price-less statuses, both returned
    # above.
    assert exit_price is not None

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
    size_usd_raw = await rdb.get(exit_module.size_usd_key(exchange, base))
    entry_price = float(entry_raw) if entry_raw else 0.0
    side = (side_raw.decode() if isinstance(side_raw, bytes) else side_raw) or "short"
    size_usd = float(size_usd_raw) if size_usd_raw else None

    log.warning(
        "position_monitor.reconcile.exchange_sl_triggered",
        base=base,
        exchange=exchange,
        order_id=sl_order_id,
        exit_price=exit_price,
    )

    executed_at = resolution.executed_at or datetime.now(tz=UTC)
    execution_time_source = resolution.execution_time_source or "local.reconciliation_observed_at"

    if trade_id_raw and cfg.db_url:
        trade_id = int(trade_id_raw)
        expected_amount = await journal.remaining_trade_amount(cfg.db_url, trade_id=trade_id)
        filled_amount = resolution.filled_amount
        if filled_amount is None:
            filled_amount = expected_amount
        if filled_amount is not None and filled_amount > 0:
            requested_amount = expected_amount if expected_amount is not None else filled_amount
            tolerance = max(requested_amount * 0.001, 1e-12)
            if filled_amount > requested_amount + tolerance:
                await journal.revoke_pnl_readiness(rdb)
                log.critical(
                    "position_monitor.reconcile.stop_fill_exceeds_remaining",
                    base=base,
                    exchange=exchange,
                    trade_id=trade_id,
                    filled_amount=filled_amount,
                    expected_amount=expected_amount,
                )
                return
            terminal = filled_amount >= requested_amount - tolerance
            remaining_amount = 0.0 if terminal else requested_amount - filled_amount
            aggregate_price = await journal.record_close_fill(
                cfg.db_url,
                trade_id=trade_id,
                exchange=exchange,
                order_id=sl_order_id,
                fill_price=exit_price,
                filled_amount=filled_amount,
                requested_amount=requested_amount,
                remaining_amount=remaining_amount,
                terminal=terminal,
                fill_source=resolution.source,
                executed_at=executed_at,
                execution_time_source=execution_time_source,
            )
            if aggregate_price is None:
                await journal.revoke_pnl_readiness(rdb)
                log.error(
                    "position_monitor.reconcile.close_fill_persist_failed",
                    base=base,
                    exchange=exchange,
                    trade_id=trade_id,
                )
                return
            exit_price = aggregate_price
            if not terminal:
                await journal.revoke_pnl_readiness(rdb)
                log.critical(
                    "position_monitor.reconcile.stop_partial_after_position_vanished",
                    base=base,
                    exchange=exchange,
                    trade_id=trade_id,
                    filled_amount=filled_amount,
                    remaining_amount=remaining_amount,
                )
                return

    # The exchange-side event (SL fill) and, when applicable, its durable
    # close leg are confirmed.  It is now safe to drop operational tracking;
    # the trade-id pointer remains until the journal commit below succeeds.
    await rdb.delete(sl_key)
    await rdb.delete(f"position:opened_at:{exchange}:{base}")
    await rdb.delete(exit_module.best_price_key(exchange, base))
    await rdb.delete(exit_module.params_key(exchange, base))
    await rdb.delete(exit_module.entry_key(exchange, base))
    await rdb.delete(exit_module.side_key(exchange, base))
    await rdb.delete(exit_module.size_usd_key(exchange, base))

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
            executed_at=executed_at,
            execution_time_source=execution_time_source,
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
        pnl_usd = size_usd * pnl_pct / 100 if size_usd else None
        await notify.notify_close(
            *creds,
            base=base,
            exchange=exchange,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl_pct=pnl_pct,
            pnl_usd=pnl_usd,
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
    executed_at_raw = data.get("executed_at")
    executed_at = (
        datetime.fromisoformat(executed_at_raw)
        if isinstance(executed_at_raw, str) and executed_at_raw
        else None
    )
    committed = await journal.try_commit_close(
        cfg.db_url,  # type: ignore[arg-type]
        rdb,
        exchange=exchange,
        base=base,
        trade_id=data["trade_id"],
        exit_order_id=data["exit_order_id"],
        exit_price=data["exit_price"],
        reason=data["reason"],
        executed_at=executed_at,
        execution_time_source=data.get("execution_time_source"),
    )
    if committed:
        trade_id_key = _TRADE_ID_KEY.format(exchange=exchange, base=base)
        await journal.delete_trade_id_if_matches(rdb, trade_id_key, trade_id)
        log.info("position_monitor.pending_close_committed", base=base, exchange=exchange)
