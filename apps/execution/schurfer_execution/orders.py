import time
import uuid
from typing import Any

import structlog

from . import notify
from .account import fetch_margin_balance, fetch_positions
from .risk import (
    DAILY_PNL_KEY,
    TRADING_ENABLED_KEY,
    check_liquidation_distance,
    check_max_position_size,
    check_sufficient_margin,
    run_all_checks,
)

log = structlog.get_logger()

SL_ORDER_ID_KEY = "position:sl_order_id:{exchange}:{base}"

# Atomic compare-and-delete: only release the lock if we still own it.
# Prevents request A from deleting request B's lock after A's TTL expired.
_RELEASE_LOCK = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


async def place_order(
    *,
    base: str,
    exchange: str,
    side: str,
    size_usd: float,
    leverage: int,
    exchanges: dict[str, Any],
    rdb: Any,
    max_positions: int,
    max_position_usd: float,
    daily_loss_limit_usd: float,
    initial_sl_pct: float = 10.0,
    liquidation_buffer_pct: float = 20.0,
    cfg: Any = None,
) -> dict[str, Any]:
    lock_key = f"lock:order:{exchange}:{base.upper()}"
    lock_token = str(uuid.uuid4())
    locked = await rdb.set(lock_key, lock_token, nx=True, px=30_000)
    if not locked:
        return {"allowed": False, "reason": f"order in progress for {base} on {exchange}"}

    try:
        # Fail-closed: a missing key (fresh deploy, Redis eviction/flush) means
        # trading is NOT enabled. Must be explicitly turned on via POST /resume.
        trading_flag = (await rdb.get(TRADING_ENABLED_KEY) or b"0").decode()
        # trading:daily_pnl is maintained by a separate daily P&L tracker process.
        # Until that tracker is implemented this check reads 0 and won't trip.
        daily_pnl = float(await rdb.get(DAILY_PNL_KEY) or 0)
        positions, failed_exchanges = await fetch_positions(exchanges)
        balances = await fetch_margin_balance(exchanges, exchange)

        check = run_all_checks(
            base=base,
            exchange=exchange,
            size_usd=size_usd,
            trading_flag=trading_flag,
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
        symbol = f"{base.upper()}/USDT:USDT"
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

        if rounded_up:
            # Actual cost after rounding may exceed the limits checked against size_usd.
            actual_usd = round(amount * price * contract_size, 2)
            size_recheck = check_max_position_size(actual_usd, max_position_usd)
            if not size_recheck.allowed:
                return {"allowed": False, "reason": size_recheck.reason}
            margin_recheck = check_sufficient_margin(actual_usd, balances, exchange)
            if not margin_recheck.allowed:
                return {"allowed": False, "reason": margin_recheck.reason}

        order = await ex.create_market_order(symbol, ccxt_side, amount)
        await rdb.set(
            f"position:opened_at:{exchange}:{base.upper()}",
            str(int(time.time())),
            ex=86400,
        )
        log.info(
            "execution.order.placed",
            base=base,
            exchange=exchange,
            side=side,
            size_usd=size_usd,
            order_id=order.get("id"),
            rounded_up=rounded_up,
        )

        fill_price = float(order.get("average") or order.get("price") or price)
        stop_side = "buy" if ccxt_side == "sell" else "sell"
        trigger_price = (
            fill_price * (1 + initial_sl_pct / 100)
            if side == "short"
            else fill_price * (1 - initial_sl_pct / 100)
        )
        trigger_price = float(ex.price_to_precision(symbol, trigger_price))

        try:
            sl_order = await ex.create_stop_market_order(
                symbol, stop_side, amount, trigger_price, params={"reduceOnly": True}
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

        return {
            "allowed": True,
            "order_id": order.get("id"),
            "exchange": exchange,
            "base": base,
            "side": side,
            "size_usd": size_usd,
            "leverage": leverage,
            "price": price,
            "status": order.get("status"),
            "rounded_up": rounded_up,
        }
    finally:
        try:
            await rdb.eval(_RELEASE_LOCK, 1, lock_key, lock_token)
        except Exception as e:
            # Best-effort release. Lock expires via TTL. Do not override order result.
            log.error("execution.lock_release_failed", lock_key=lock_key, err=str(e))


async def close_position(
    *,
    exchanges: dict[str, Any],
    exchange: str,
    base: str,
    reason: str,
    rdb: Any,
) -> dict[str, Any]:
    lock_key = f"lock:order:{exchange}:{base.upper()}"
    lock_token = str(uuid.uuid4())
    locked = await rdb.set(lock_key, lock_token, nx=True, px=30_000)
    if not locked:
        return {
            "closed": False,
            "reason": f"close already in progress for {base} on {exchange}",
        }

    try:
        ex = exchanges.get(exchange)
        if not ex:
            return {"closed": False, "reason": f"exchange {exchange!r} not configured"}

        symbol = f"{base.upper()}/USDT:USDT"
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
        fill_price = float(order.get("average") or order.get("price") or 0)
        exit_price: float | None = fill_price or (mark_price if mark_price > 0 else None)
        await rdb.delete(f"position:opened_at:{exchange}:{base.upper()}")
        log.info(
            "execution.position.closed",
            base=base,
            exchange=exchange,
            side=position_side,
            reason=reason,
            order_id=order.get("id"),
            exit_price=exit_price,
        )
        return {
            "closed": True,
            "order_id": order.get("id"),
            "exchange": exchange,
            "base": base,
            "side": position_side,
            "reason": reason,
            "exit_price": exit_price,
        }
    finally:
        try:
            await rdb.eval(_RELEASE_LOCK, 1, lock_key, lock_token)
        except Exception as e:
            log.error("execution.close.lock_release_failed", lock_key=lock_key, err=str(e))
