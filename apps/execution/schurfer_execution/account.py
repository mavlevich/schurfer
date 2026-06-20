import asyncio
from typing import Any

import ccxt.async_support as ccxt
import structlog

log = structlog.get_logger()


async def fetch_balance(exchanges: dict[str, Any]) -> list[dict[str, Any]]:
    async def _one(name: str, ex: ccxt.Exchange) -> dict[str, Any] | None:
        try:
            bal = await ex.fetch_balance()
            usdt = bal.get("USDT") or bal.get("total", {})
            if isinstance(usdt, dict):
                free = float(usdt.get("free") or 0)
                used = float(usdt.get("used") or 0)
                total = float(usdt.get("total") or 0)
            else:
                free = float(bal.get("free", {}).get("USDT") or 0)
                used = float(bal.get("used", {}).get("USDT") or 0)
                total = float(bal.get("total", {}).get("USDT") or 0)
            return {"exchange": name, "free": free, "used": used, "total": total}
        except Exception as e:
            log.warning("account.balance.error", exchange=name, err=str(e))
            return None

    results = await asyncio.gather(*[_one(name, ex) for name, ex in exchanges.items()])
    return [r for r in results if r is not None]


async def fetch_positions(
    exchanges: dict[str, Any],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Fetch open positions in parallel. Returns (positions, failed_exchange_names).

    Callers must treat a non-empty failed set as an error — do not skip risk checks
    when positions for the target exchange could not be retrieved.
    """
    failed: set[str] = set()

    async def _one(name: str, ex: ccxt.Exchange) -> list[dict[str, Any]]:
        try:
            positions = await ex.fetch_positions()
            open_positions: list[dict[str, Any]] = []
            for p in positions:
                contracts = float(p.get("contracts") or 0)
                if contracts == 0:
                    continue
                open_positions.append(
                    {
                        "exchange": name,
                        "symbol": p.get("symbol", ""),
                        "base": p.get("symbol", "").split("/")[0],
                        "side": p.get("side", ""),
                        "size_usd": float(p.get("notional") or 0),
                        "entry_price": float(p.get("entryPrice") or 0),
                        "unrealized_pnl": float(p.get("unrealizedPnl") or 0),
                        "leverage": float(p.get("leverage") or 1),
                        "liquidation_price": p.get("liquidationPrice"),
                    }
                )
            return open_positions
        except Exception as e:
            log.warning("account.positions.error", exchange=name, err=str(e))
            failed.add(name)  # safe: asyncio cooperative multitasking, no concurrent mutation
            return []

    results = await asyncio.gather(*[_one(name, ex) for name, ex in exchanges.items()])
    return [pos for group in results for pos in group], failed
