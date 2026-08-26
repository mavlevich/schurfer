import time
import uuid
from typing import Any

import ccxt
import structlog

from . import exit as exit_module
from . import incidents, journal, notify, order_attempts
from .account import fetch_margin_balance, fetch_positions
from .fill_price import FILL_UNRESOLVED, resolve_fill_price
from .journal import revoke_pnl_readiness
from .order_lock import OrderLockLease
from .risk import (
    DAILY_PNL_KEY,
    PNL_READY_KEY,
    TRADING_ENABLED_KEY,
    check_liquidation_distance,
    check_max_position_size,
    check_sufficient_margin,
    run_all_checks,
)
from .supervisor import SUBMISSION_UNKNOWN_BLOCKER, WorkerReadinessGate

log = structlog.get_logger()

SL_ORDER_ID_KEY = "position:sl_order_id:{exchange}:{base}"


async def _handle_unresolved_open(
    *,
    db_url: str | None,
    rdb: Any,
    cfg: Any,
    exchange: str,
    base: str,
    order_id: str,
    side: str,
    size_usd: float,
    leverage: int,
    setup_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """The order is confirmed placed on the exchange, but its fill price is not.

    Never fabricate an entry price to keep the normal open flow running. Instead
    create a durable incident and defer journal.open_trade/exit tracking to the
    incident worker, which completes the open once resolve_fill_price confirms a
    real price for this same order id.
    """
    await revoke_pnl_readiness(rdb)
    incident_id = None
    if db_url:
        incident_id = await incidents.create_incident(
            db_url,
            exchange=exchange,
            base=base,
            operation="open",
            order_id=order_id,
            trade_id=None,
            context={
                "side": side,
                "size_usd": size_usd,
                "leverage": leverage,
                "setup_context": setup_context,
            },
        )
        if incident_id is not None and await incidents.claim_creation_notification(
            db_url, incident_id
        ):
            creds = notify.credentials(cfg) if cfg is not None else None
            if creds:
                await notify.notify_alert(
                    *creds,
                    text=(
                        f"Fill price unresolved for OPEN {base} on {exchange} "
                        f"(order {order_id}). Position exists on the exchange; "
                        "journal entry and exit tracking are deferred until the "
                        "price is confirmed. See incident "
                        f"{incident_id}."
                    ),
                )
    log.error(
        "execution.order.fill_unresolved",
        base=base,
        exchange=exchange,
        order_id=order_id,
        incident_id=incident_id,
    )
    return {
        "allowed": True,
        "fill_status": FILL_UNRESOLVED,
        "incident_id": incident_id,
        "order_id": order_id,
        "exchange": exchange,
        "base": base,
        "side": side,
        "size_usd": size_usd,
        "leverage": leverage,
    }


async def _handle_unresolved_close(
    *,
    db_url: str | None,
    rdb: Any,
    cfg: Any,
    exchange: str,
    base: str,
    order_id: str,
    side: str,
    reason: str,
    mark_price: float,
) -> dict[str, Any]:
    """The close is confirmed on the exchange, but its fill price is not.

    The position is already gone from the exchange's perspective, so the caller
    must still stop monitoring it — only the journal close (and therefore this
    trade's realized PnL) is deferred, exactly like an unresolved journal write
    already is via journal.write_pending_close, just one step earlier.
    """
    await revoke_pnl_readiness(rdb)
    incident_id = None
    if db_url:
        trade_id_raw = await rdb.get(f"trade:id:{exchange}:{base.upper()}")
        trade_id = int(trade_id_raw) if trade_id_raw else None
        incident_id = await incidents.create_incident(
            db_url,
            exchange=exchange,
            base=base,
            operation="close",
            order_id=order_id,
            trade_id=trade_id,
            context={"reason": reason, "mark_price": mark_price},
        )
        if incident_id is not None and await incidents.claim_creation_notification(
            db_url, incident_id
        ):
            creds = notify.credentials(cfg) if cfg is not None else None
            if creds:
                await notify.notify_alert(
                    *creds,
                    text=(
                        f"Fill price unresolved for CLOSE {base} on {exchange} "
                        f"(order {order_id}). Position is closed on the exchange; "
                        f"PnL is unknown until reconciled. See incident {incident_id}."
                    ),
                )
    log.error(
        "execution.close.fill_unresolved",
        base=base,
        exchange=exchange,
        order_id=order_id,
        incident_id=incident_id,
    )
    return {
        "closed": True,
        "fill_status": FILL_UNRESOLVED,
        "incident_id": incident_id,
        "order_id": order_id,
        "exchange": exchange,
        "base": base,
        "side": side,
        "reason": reason,
        "exit_price": None,
    }


async def place_order(
    *,
    base: str,
    symbol: str,
    exchange: str,
    side: str,
    size_usd: float,
    leverage: int,
    exchanges: dict[str, Any],
    rdb: Any,
    max_positions: int,
    max_position_usd: float,
    daily_loss_limit_usd: float,
    liquidity_checked_usd: float | None = None,
    # Full dict (not just initial_sl_pct alone) so the SAME values used for
    # the SL trigger price below are also what complete_open (further down)
    # writes to Redis for position monitoring -- a caller passing these two
    # independently is exactly what let trader.py and incident_worker.py
    # drift into their own hand-copied exit_params before this PR. None
    # falls back to exit_module.exit_params(None) -- the same default
    # bracket the old initial_sl_pct=10.0 default resolved to, so the manual
    # /order HTTP endpoint (routers/orders.py, no signal/pump_pct to derive
    # this from) needs no change.
    exit_params: dict[str, float] | None = None,
    liquidation_buffer_pct: float = 20.0,
    cfg: Any = None,
    setup_context: dict[str, Any] | None = None,
    worker_gate: WorkerReadinessGate,
) -> dict[str, Any]:
    exit_params = exit_params or exit_module.exit_params(None)
    initial_sl_pct = exit_params["initial_sl_pct"]

    # Admission is checked before even taking the Redis order lock. The
    # generation token is checked again immediately before the exchange call,
    # closing the race where a critical worker fails during pre-flight checks.
    is_open, gate_token = worker_gate.is_open()
    if not is_open:
        return {
            "allowed": False,
            "reason": f"worker readiness gate closed: {worker_gate.get_reasons()}",
        }

    lock_key = f"lock:order:{exchange}:{base.upper()}"
    lease = await OrderLockLease.acquire(rdb=rdb, key=lock_key, operation="open")
    if lease is None:
        return {"allowed": False, "reason": f"order in progress for {base} on {exchange}"}

    async with lease:
        # Fail-closed: a missing key (fresh deploy, Redis eviction/flush) means
        # trading is NOT enabled. Must be explicitly turned on via POST /resume.
        trading_flag = (await rdb.get(TRADING_ENABLED_KEY) or b"0").decode()
        # trading:daily_pnl is maintained by the pnl tracker (tracker.py), which
        # also refreshes PNL_READY_KEY below only after a fully successful tick.
        daily_pnl = float(await rdb.get(DAILY_PNL_KEY) or 0)
        pnl_ready_raw = await rdb.get(PNL_READY_KEY)
        pnl_ready_flag = (
            pnl_ready_raw.decode() if isinstance(pnl_ready_raw, bytes) else pnl_ready_raw
        )
        positions, failed_exchanges = await fetch_positions(exchanges)
        balances = await fetch_margin_balance(exchanges, exchange)

        check = run_all_checks(
            base=base,
            exchange=exchange,
            size_usd=size_usd,
            trading_flag=trading_flag,
            pnl_ready_flag=pnl_ready_flag,
            open_positions=positions,
            balances=balances,
            daily_pnl=daily_pnl,
            max_positions=max_positions,
            max_position_usd=max_position_usd,
            daily_loss_limit_usd=daily_loss_limit_usd,
            failed_exchanges=failed_exchanges,
        )
        if not check.allowed:
            return {"allowed": False, "reason": check.reason}

        liq_check = check_liquidation_distance(initial_sl_pct, leverage, liquidation_buffer_pct)
        if not liq_check.allowed:
            return {"allowed": False, "reason": liq_check.reason}

        ex = exchanges[exchange]
        # symbol passed explicitly
        ccxt_side = "sell" if side == "short" else "buy"

        if not ex.markets:
            await ex.load_markets()

        market = ex.markets.get(symbol)
        if market is None:
            return {"allowed": False, "reason": f"symbol {symbol} not found on {exchange}"}

        contract_size = float(market.get("contractSize") or 1.0)
        limits = market.get("limits") or {}
        min_amount = float((limits.get("amount") or {}).get("min") or 0)
        min_cost = float((limits.get("cost") or {}).get("min") or 0)

        await ex.set_leverage(leverage, symbol)
        ticker = await ex.fetch_ticker(symbol)
        price = float(ticker["last"])

        requested_amount = size_usd / price / contract_size
        raw_amount = requested_amount
        if min_amount > 0 and raw_amount < min_amount:
            log.info(
                "execution.order.amount_rounded_up",
                base=base,
                requested=round(raw_amount, 6),
                minimum=min_amount,
            )
            raw_amount = min_amount
        rounded_up = raw_amount > requested_amount

        amount = float(ex.amount_to_precision(symbol, raw_amount))
        if amount == 0:
            hint = f" — min {min_amount} contracts (${min_cost:.2f})" if min_amount else ""
            return {
                "allowed": False,
                "reason": f"amount rounds to 0 for {symbol}{hint}",
            }

        actual_usd = round(amount * price * contract_size, 2)
        if liquidity_checked_usd is not None and actual_usd > liquidity_checked_usd:
            return {
                "allowed": False,
                "reason": (
                    f"actual position ${actual_usd:.2f} exceeds liquidity-checked "
                    f"notional ${liquidity_checked_usd:.2f}"
                ),
            }

        if rounded_up:
            # Actual cost after rounding may exceed the limits checked against size_usd.
            size_recheck = check_max_position_size(actual_usd, max_position_usd)
            if not size_recheck.allowed:
                return {"allowed": False, "reason": size_recheck.reason}
            margin_recheck = check_sufficient_margin(actual_usd, balances, exchange)
            if not margin_recheck.allowed:
                return {"allowed": False, "reason": margin_recheck.reason}

        # clientOrderId (bybit: mapped to orderLinkId by ccxt) is generated
        # locally, before the call -- it round-trips through the exchange
        # and back on every subsequent fetch of this order, so it survives
        # even a total local crash that loses order_id itself (never
        # captured, or captured but never persisted).
        entry_client_order_id = str(uuid.uuid4())
        db_url = getattr(cfg, "db_url", None) if cfg is not None else None

        # Durable record written BEFORE the exchange is ever called (see
        # order_attempts.py's own docstring) -- if this fails while a
        # database IS configured, refuse to place the order at all rather
        # than risk a real position with zero durable trace anywhere,
        # including during a total DB outage the later journal write (and
        # its own incident-creation fallback) would equally be unable to
        # survive (colleague review, P0). cfg=None entirely (no database
        # configured for this call at all -- some test harnesses, and any
        # future caller that genuinely never wants tracking) skips this
        # gate, matching journal.complete_open's own "db_url may be None"
        # convention one step earlier.
        attempt_id: int | None = None
        if db_url:
            attempt_id = await order_attempts.create_attempt(
                db_url,
                client_order_id=entry_client_order_id,
                exchange=exchange,
                base=base,
                symbol=symbol,
                native_market_id=market.get("id"),
                market_type=market.get("type"),
                side=side,
                size_usd=actual_usd,
                requested_amount=amount,
                leverage=leverage,
                contract_size=contract_size,
                exit_params=exit_params,
                setup_context=setup_context or {},
            )
            if attempt_id is None:
                return {
                    "allowed": False,
                    "reason": "cannot durably record order intent (db unavailable) -- "
                    "refusing to place a live order that could not be tracked",
                }

        is_open, current_token = worker_gate.is_open()
        if not is_open or current_token != gate_token:
            reason = "worker readiness gate closed or generation changed during checks"
            if db_url and attempt_id is not None:
                await order_attempts.mark_failed(db_url, attempt_id, error=reason)
            return {"allowed": False, "reason": reason}

        try:
            order = await ex.create_market_order(
                symbol, ccxt_side, amount, params={"clientOrderId": entry_client_order_id}
            )
        except ccxt.NetworkError as exc:
            if db_url and attempt_id is not None:
                await order_attempts.mark_submission_unknown(db_url, attempt_id, error=str(exc))
            worker_gate.set_safety_blocker(SUBMISSION_UNKNOWN_BLOCKER)
            return {
                "allowed": False,
                "reason": f"submission_unknown: network timeout or error {exc}",
            }
        except ccxt.ExchangeError as exc:
            # Explicit exchange rejection (e.g. InsufficientFunds, InvalidOrder)
            if db_url and attempt_id is not None:
                await order_attempts.mark_failed(db_url, attempt_id, error=str(exc))
            return {"allowed": False, "reason": f"exchange_rejection: {exc}"}
        except Exception as exc:
            if db_url and attempt_id is not None:
                await order_attempts.mark_submission_unknown(db_url, attempt_id, error=str(exc))
            worker_gate.set_safety_blocker(SUBMISSION_UNKNOWN_BLOCKER)
            return {"allowed": False, "reason": f"submission_unknown: unexpected error {exc}"}

        if db_url and attempt_id is not None:
            await order_attempts.mark_accepted(db_url, attempt_id, order_id=str(order.get("id")))

        await rdb.set(
            f"position:opened_at:{exchange}:{base.upper()}",
            str(int(time.time())),
            ex=86400,
        )
        order_id = order.get("id")
        log.info(
            "execution.order.placed",
            base=base,
            exchange=exchange,
            side=side,
            size_usd=size_usd,
            order_id=order_id,
            rounded_up=rounded_up,
        )

        resolution = await resolve_fill_price(
            ex, symbol=symbol, order=order, requested_amount=amount
        )
        # The protective stop must exist even if the fill price cannot be confirmed
        # yet — an unprotected position is worse than one sized off the best
        # available reference. This reference price is never recorded as the fill:
        # accounting/journal stays blocked on resolution.price, not on this.
        sl_reference_price = resolution.price if resolution.price is not None else price
        stop_side = "buy" if ccxt_side == "sell" else "sell"
        trigger_price = (
            sl_reference_price * (1 + initial_sl_pct / 100)
            if side == "short"
            else sl_reference_price * (1 - initial_sl_pct / 100)
        )
        trigger_price = float(ex.price_to_precision(symbol, trigger_price))

        try:
            sl_client_order_id = str(uuid.uuid4())
            sl_order = await ex.create_stop_market_order(
                symbol,
                stop_side,
                amount,
                trigger_price,
                params={"reduceOnly": True, "clientOrderId": sl_client_order_id},
            )
            await rdb.set(
                SL_ORDER_ID_KEY.format(exchange=exchange, base=base.upper()),
                str(sl_order.get("id")),
                ex=86400,
            )
            log.info(
                "execution.stop_loss.placed",
                base=base,
                exchange=exchange,
                trigger_price=trigger_price,
                order_id=sl_order.get("id"),
            )
        except Exception as sl_exc:
            # Fail-safe: a position without a stop-loss must not be left open.
            # close_position() is not used here — it re-acquires this same lock
            # and would deadlock while we still hold it.
            log.error(
                "execution.stop_loss.failed",
                base=base,
                exchange=exchange,
                err=str(sl_exc),
            )
            alert_text = (
                f"Stop-loss placement FAILED for {base} on {exchange}: {sl_exc}\n"
                f"Force-closing position immediately."
            )
            force_closed = False
            try:
                close_order = await ex.create_market_order(
                    symbol, stop_side, amount, params={"reduceOnly": True}
                )
                await rdb.delete(f"position:opened_at:{exchange}:{base.upper()}")
                log.warning(
                    "execution.stop_loss.emergency_close",
                    base=base,
                    exchange=exchange,
                    order_id=close_order.get("id"),
                )
                alert_text += "\nPosition force-closed successfully."
                force_closed = True
            except Exception as close_exc:
                log.critical(
                    "execution.stop_loss.emergency_close_failed",
                    base=base,
                    exchange=exchange,
                    err=str(close_exc),
                )
                alert_text += (
                    f"\nCRITICAL: emergency close ALSO failed ({close_exc}) — "
                    f"position is UNPROTECTED. Manual intervention required."
                )
            creds = notify.credentials(cfg) if cfg is not None else None
            if creds:
                await notify.notify_alert(*creds, text=alert_text)
            reason = (
                f"stop-loss placement failed, position force-closed: {sl_exc}"
                if force_closed
                else (
                    f"stop-loss placement failed AND emergency close failed — "
                    f"position UNPROTECTED on {exchange}:{base}: {sl_exc}"
                )
            )
            return {"allowed": False, "reason": reason, "force_closed": force_closed}

        if resolution.status == FILL_UNRESOLVED:
            # Exchanges are expected to always return an id for a placed order;
            # fall back to a synthetic one only so an id-less response can never
            # collide with another incident under the (exchange, order_id) key.
            incident_order_id = str(order_id) if order_id is not None else f"unknown:{uuid.uuid4()}"
            result = await _handle_unresolved_open(
                db_url=db_url,
                rdb=rdb,
                cfg=cfg,
                exchange=exchange,
                base=base,
                order_id=incident_order_id,
                side=side,
                size_usd=size_usd,
                leverage=leverage,
                setup_context=setup_context,
            )
            if db_url and attempt_id is not None:
                await order_attempts.mark_completed(db_url, attempt_id, trade_id=None)
            return result

        # A price is confirmed at this point (the FILL_UNRESOLVED branch
        # above already returned otherwise) -- complete the journal write
        # and the Redis exit-tracking keys right here, inside the same lock
        # and the same function call as the exchange order itself, instead
        # of leaving that to the caller a few awaits (write_decision,
        # notify) later. That gap used to mean a crash between this
        # function returning and the caller's own bookkeeping left a real,
        # SL-protected position with no app.trades row, ever -- operational
        # safety (the SL, monitor.py's exit checks reading straight off the
        # exchange position) was never at risk, only the ledger was
        # (colleague review candidate on an earlier draft).
        assert resolution.price is not None
        trade_id = await journal.complete_open(
            db_url,
            rdb,
            symbol=symbol,
            exchange=exchange,
            base=base,
            side=side,
            order_id=str(order_id) if order_id is not None else None,
            size_usd=actual_usd,
            leverage=leverage,
            entry_price=resolution.price,
            exit_params=exit_params,
            setup_context=setup_context or {},
        )
        if db_url and attempt_id is not None:
            await order_attempts.mark_completed(
                db_url, attempt_id, trade_id=trade_id, filled_amount=resolution.filled_amount
            )
        if db_url and trade_id is None:
            # The exchange fill is confirmed and protected, but the journal
            # write itself failed (DB outage at exactly this moment) --
            # durably record it the same way an unresolved fill price
            # already is, so incident_worker can retry journal.complete_open
            # via its own _complete_open once the DB is reachable again,
            # instead of this position silently never getting a ledger
            # entry. Context includes exit_params/contract_size/
            # client_order_id/filled_amount too, not just the bare fields
            # the original FILL_UNRESOLVED incident context carried -- a
            # human reconciling this by hand (or a future recovery path)
            # needs the exact SL sizing and fill this attempt actually saw,
            # not a value re-derived from setup_context.get("pump_pct")
            # that may not match (colleague review).
            # incident_worker re-resolves the fill price itself rather than
            # trusting a cached one here -- one redundant exchange call on
            # this rare path (DB down at exactly this moment) is a small
            # price for reusing the exact same, already-tested recovery
            # machinery instead of a second, parallel one.
            incident_order_id = str(order_id) if order_id is not None else f"unknown:{uuid.uuid4()}"
            incident_id = await incidents.create_incident(
                db_url,
                exchange=exchange,
                base=base,
                operation="open",
                order_id=incident_order_id,
                trade_id=None,
                context={
                    "side": side,
                    "size_usd": actual_usd,
                    "leverage": leverage,
                    "setup_context": setup_context,
                    "exit_params": exit_params,
                    "contract_size": contract_size,
                    "client_order_id": entry_client_order_id,
                    "filled_amount": resolution.filled_amount,
                },
            )
            await revoke_pnl_readiness(rdb)
            log.critical(
                "execution.order.journal_write_failed",
                base=base,
                exchange=exchange,
                order_id=order_id,
                incident_id=incident_id,
            )
            creds = notify.credentials(cfg) if cfg is not None else None
            if creds:
                # Best-effort and independent of Postgres -- fires even if
                # incident_id is None because create_incident itself also
                # hit the same outage that took down the journal write, the
                # one scenario where nothing else durable exists for this
                # position at all yet (colleague review, P0).
                incident_note = (
                    f"incident {incident_id}"
                    if incident_id is not None
                    else "incident ALSO FAILED TO CREATE -- reconcile via "
                    "app.live_order_attempts.client_order_id"
                )
                await notify.notify_alert(
                    *creds,
                    text=(
                        f"Journal write FAILED for a live OPEN on {base}/{exchange} "
                        f"(order {order_id}, client_order_id {entry_client_order_id}). "
                        f"Position is real and SL-protected on the exchange. {incident_note}."
                    ),
                )

        return {
            "allowed": True,
            "fill_status": resolution.status,
            "fill_source": resolution.source,
            "order_id": order_id,
            "exchange": exchange,
            "base": base,
            "side": side,
            "size_usd": size_usd,
            "leverage": leverage,
            "price": resolution.price,
            "status": order.get("status"),
            "rounded_up": rounded_up,
            "trade_id": trade_id,
        }
    raise RuntimeError("open order lease exited without an operation result")


async def close_position(
    *,
    exchanges: dict[str, Any],
    exchange: str,
    base: str,
    symbol: str,
    reason: str,
    rdb: Any,
    cfg: Any = None,
) -> dict[str, Any]:
    lock_key = f"lock:order:{exchange}:{base.upper()}"
    lease = await OrderLockLease.acquire(rdb=rdb, key=lock_key, operation="close")
    if lease is None:
        return {
            "closed": False,
            "reason": f"close already in progress for {base} on {exchange}",
        }

    async with lease:
        ex = exchanges.get(exchange)
        if not ex:
            return {"closed": False, "reason": f"exchange {exchange!r} not configured"}

        # symbol passed explicitly
        all_positions = await ex.fetch_positions()
        position = next(
            (
                p
                for p in all_positions
                if p.get("symbol") == symbol and float(p.get("contracts") or 0) > 0
            ),
            None,
        )
        if position is None:
            return {"closed": False, "reason": f"no open position for {symbol}"}

        contracts = float(position["contracts"])
        position_side = position.get("side", "")
        if not position_side:
            return {"closed": False, "reason": f"position side unknown for {symbol}"}
        close_side = "buy" if position_side == "short" else "sell"

        if not ex.markets:
            await ex.load_markets()

        # Cancel the resting exchange stop-loss first, so it doesn't linger as a
        # dangling reduce-only order once this close fills the position to zero.
        sl_key = SL_ORDER_ID_KEY.format(exchange=exchange, base=base.upper())
        sl_order_id = await rdb.get(sl_key)
        if sl_order_id:
            sl_order_id = sl_order_id.decode() if isinstance(sl_order_id, bytes) else sl_order_id
            try:
                await ex.cancel_order(sl_order_id, symbol)
            except Exception as cancel_exc:
                # Order may have already filled or expired — not fatal, proceed with close.
                log.warning(
                    "execution.stop_loss.cancel_failed",
                    base=base,
                    exchange=exchange,
                    order_id=sl_order_id,
                    err=str(cancel_exc),
                )
            await rdb.delete(sl_key)

        mark_price = float(position.get("markPrice") or position.get("mark_price") or 0)
        amount = float(ex.amount_to_precision(symbol, contracts))
        order = await ex.create_market_order(
            symbol, close_side, amount, params={"reduceOnly": True}
        )
        order_id = order.get("id")
        await rdb.delete(f"position:opened_at:{exchange}:{base.upper()}")

        resolution = await resolve_fill_price(
            ex, symbol=symbol, order=order, requested_amount=amount
        )
        if resolution.status == FILL_UNRESOLVED:
            close_order_id = str(order_id) if order_id is not None else f"unknown:{uuid.uuid4()}"
            return await _handle_unresolved_close(
                db_url=getattr(cfg, "db_url", None) if cfg is not None else None,
                rdb=rdb,
                cfg=cfg,
                exchange=exchange,
                base=base,
                order_id=close_order_id,
                side=position_side,
                reason=reason,
                mark_price=mark_price,
            )

        log.info(
            "execution.position.closed",
            base=base,
            exchange=exchange,
            side=position_side,
            reason=reason,
            order_id=order_id,
            exit_price=resolution.price,
        )
        return {
            "closed": True,
            "fill_status": resolution.status,
            "fill_source": resolution.source,
            "order_id": order_id,
            "exchange": exchange,
            "base": base,
            "side": position_side,
            "reason": reason,
            "exit_price": resolution.price,
        }
    raise RuntimeError("close order lease exited without an operation result")
