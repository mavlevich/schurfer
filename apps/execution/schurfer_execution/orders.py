import uuid
from typing import Any

import structlog

from .account import fetch_margin_balance, fetch_positions
from .risk import DAILY_PNL_KEY, TRADING_ENABLED_KEY, run_all_checks

log = structlog.get_logger()

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
) -> dict[str, Any]:
    lock_key = f"lock:order:{exchange}:{base.upper()}"
    lock_token = str(uuid.uuid4())
    locked = await rdb.set(lock_key, lock_token, nx=True, px=30_000)
    if not locked:
        return {"allowed": False, "reason": f"order in progress for {base} on {exchange}"}

    try:
        trading_flag = (await rdb.get(TRADING_ENABLED_KEY) or b"1").decode()
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

        ex = exchanges[exchange]
        symbol = f"{base.upper()}/USDT:USDT"
        ccxt_side = "sell" if side == "short" else "buy"

        if not ex.markets:
            await ex.load_markets()

        market = ex.markets.get(symbol)
        if market is None:
            return {"allowed": False, "reason": f"symbol {symbol} not found on {exchange}"}

        contract_size = float(market.get("contractSize") or 1.0)
        await ex.set_leverage(leverage, symbol)
        ticker = await ex.fetch_ticker(symbol)
        price = float(ticker["last"])
        raw_amount = size_usd / price / contract_size
        amount = float(ex.amount_to_precision(symbol, raw_amount))

        order = await ex.create_market_order(symbol, ccxt_side, amount)
        log.info(
            "execution.order.placed",
            base=base,
            exchange=exchange,
            side=side,
            size_usd=size_usd,
            order_id=order.get("id"),
        )
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
        }
    finally:
        try:
            await rdb.eval(_RELEASE_LOCK, 1, lock_key, lock_token)
        except Exception as e:
            # Best-effort release. Lock expires via TTL. Do not override order result.
            log.error("execution.lock_release_failed", lock_key=lock_key, err=str(e))
