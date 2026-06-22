import asyncio
from typing import Any

import ccxt.async_support as ccxt
import structlog

log = structlog.get_logger()


_BALANCE_SKIP = frozenset({"free", "used", "total", "info", "timestamp", "datetime", "debt"})


def _usd_values_from_info(bal: dict[str, Any]) -> dict[str, float]:
    """Try to extract per-asset USD values from exchange-specific info (Bybit V5)."""
    usd: dict[str, float] = {}
    try:
        for account in bal.get("info", {}).get("result", {}).get("list", []):
            for coin in account.get("coin", []):
                sym = coin.get("coin", "")
                val = coin.get("usdValue")
                if sym and val is not None:
                    usd[sym] = float(val)
    except Exception as e:
        log.debug("account.usd_values.parse_skip", err=str(e))
    return usd


def _extract_all_assets(bal: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one entry per non-zero asset found in a ccxt balance response."""
    usd_values = _usd_values_from_info(bal)
    assets = []
    for symbol, v in bal.items():
        if symbol in _BALANCE_SKIP or not isinstance(v, dict):
            continue
        total = float(v.get("total") or 0)
        if total == 0:
            continue
        usd_value = usd_values.get(symbol, total if symbol == "USDT" else 0.0)
        assets.append(
            {
                "asset": symbol,
                "free": float(v.get("free") or 0),
                "used": float(v.get("used") or 0),
                "total": total,
                "usd_value": usd_value,
            }
        )
    return assets


async def _fetch_usd_price(ex: ccxt.Exchange, asset: str) -> float:
    # Try spot market explicitly — derivatives exchanges may not resolve {asset}/USDT otherwise
    for params in ({"type": "spot"}, {}):
        try:
            ticker = await ex.fetch_ticker(f"{asset}/USDT", params=params)
            price = float(ticker.get("last") or 0)
            if price > 0:
                return price
        except Exception as e:
            log.debug("account.usd_price.ticker_skip", asset=asset, params=params, err=str(e))
    log.debug("account.usd_price.unavailable", asset=asset)
    return 0.0


async def fetch_balance(exchanges: dict[str, Any]) -> list[dict[str, Any]]:
    async def _one(name: str, ex: ccxt.Exchange) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        default_type: str = getattr(ex, "options", {}).get("defaultType", "spot")

        def _add(bal: dict[str, Any], wallet: str, tradeable: bool) -> None:
            for asset_data in _extract_all_assets(bal):
                rows.append(
                    {"exchange": name, "wallet": wallet, "tradeable": tradeable, **asset_data}
                )

        try:
            params = {"type": default_type} if default_type != "spot" else {}
            bal = await ex.fetch_balance(params)
            _add(bal, default_type, True)
        except Exception as e:
            log.warning("account.balance.error", exchange=name, wallet=default_type, err=str(e))

        if default_type != "spot":
            try:
                bal = await ex.fetch_balance({"type": "spot"})
                _add(bal, "spot", False)
            except Exception as e:
                log.warning("account.balance.spot_error", exchange=name, err=str(e))

        if default_type != "fund":
            try:
                bal = await ex.fetch_balance({"type": "fund"})
                _add(bal, "fund", False)
            except Exception as e:
                log.debug("account.balance.fund_skip", exchange=name, err=str(e))

        # Resolve USD values for non-USDT assets not covered by exchange info
        needs_price = {r["asset"] for r in rows if r["asset"] != "USDT" and r["usd_value"] == 0.0}
        if needs_price:
            prices = await asyncio.gather(*[_fetch_usd_price(ex, sym) for sym in needs_price])
            price_map = dict(zip(needs_price, prices, strict=True))
            for r in rows:
                if r["usd_value"] == 0.0 and r["asset"] in price_map:
                    r["usd_value"] = round(r["total"] * price_map[r["asset"]], 2)

        # Always show configured exchanges even with zero balance.
        # error=True means all wallet fetches failed — balance unknown, not genuinely $0.
        if not rows:
            rows.append(
                {
                    "exchange": name,
                    "wallet": default_type,
                    "tradeable": True,
                    "asset": "USDT",
                    "free": 0.0,
                    "used": 0.0,
                    "total": 0.0,
                    "usd_value": 0.0,
                    "error": True,
                }
            )

        return rows

    results = await asyncio.gather(*[_one(name, ex) for name, ex in exchanges.items()])
    return [row for group in results for row in group]


async def fetch_margin_balance(exchanges: dict[str, Any], exchange: str) -> list[dict[str, Any]]:
    """Lightweight balance fetch for order pre-checks.

    Fetches only the primary tradeable wallet of the target exchange (no spot/fund/price lookups).
    Returns only USDT rows — the only asset used for margin checks.
    """
    ex = exchanges.get(exchange)
    if not ex:
        return []
    default_type: str = getattr(ex, "options", {}).get("defaultType", "spot")
    try:
        params = {"type": default_type} if default_type != "spot" else {}
        bal = await ex.fetch_balance(params)
        return [
            {"exchange": exchange, "wallet": default_type, "tradeable": True, **a}
            for a in _extract_all_assets(bal)
            if a["asset"] == "USDT"
        ]
    except Exception as e:
        log.warning("account.margin.error", exchange=exchange, err=str(e))
        return []


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
