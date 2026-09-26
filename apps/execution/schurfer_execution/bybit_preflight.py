"""Bybit account preflight: read-only checks before any LIVE_PROBE order (step 1).

This module can only read. Its HTTP client has a single signed GET method and refuses any
path outside `ALLOWED_GET_PATHS`; no order, position-setting or transfer endpoint is
reachable from here. LIVE_PROBE stays blocked in code regardless of this verdict.

A verdict is a snapshot, not a standing permission: `ready` holds only at `snapshot_at`,
and a future order path must re-run the checks immediately before each order.

Purposes (the key is checked for the purpose it is used for):

- `diagnostic`: today's read-only key. The key must be read-only and must not withdraw.
  `ready` here says the account is set up; it never authorises trading.
- `live_probe`: the future trading key. It must be able to trade contracts, must not be
  able to withdraw, and must be IP-whitelisted.

Account checks, for both purposes:

- margin mode ISOLATED_MARGIN (`GET /v5/account/info`);
- isolated-available USDT covers the probe notional plus a fee reserve (wallet balance
  minus position IM, order IM, locked and bonus, as Bybit documents for isolated margin);
- the dedicated account is empty: no open linear position and no active or conditional
  order in any settle coin, read through every page;
- each target symbol reads back as one-way (`positionIdx` 0) with leverage at most 1x;
  a symbol that cannot be confirmed by a read is `unverified`, never `ready`.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

MAINNET_URL = "https://api.bybit.com"
ALLOWED_GET_PATHS = frozenset(
    {
        "/v5/user/query-api",
        "/v5/account/info",
        "/v5/account/wallet-balance",
        "/v5/position/list",
        "/v5/order/realtime",
    }
)
PURPOSES = ("diagnostic", "live_probe")
PROBE_NOTIONAL_USD = Decimal(50)
FEE_RESERVE_USD = Decimal(5)
MAX_LEVERAGE = Decimal(1)
SETTLE_COINS = ("USDT", "USDC")
ORDER_FILTERS = ("Order", "StopOrder")
RECV_WINDOW_MS = 5000
_MAX_PAGES = 50


class ForbiddenPathError(ValueError):
    """The preflight client only reads, and only the allow-listed paths."""


class BybitApiError(RuntimeError):
    def __init__(self, path: str, code: object, message: object) -> None:
        super().__init__(f"{path} retCode={code} {message}")
        self.path = path
        self.code = code


def sign(secret: str, timestamp_ms: int, api_key: str, recv_window: int, query: str) -> str:
    """Bybit V5 HMAC-SHA256 signature of a GET request."""
    payload = f"{timestamp_ms}{api_key}{recv_window}{query}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


class BybitReadOnlyClient:
    """Signed GET-only client restricted to `ALLOWED_GET_PATHS`."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        http: httpx.AsyncClient,
        *,
        base_url: str = MAINNET_URL,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._key = api_key
        self._secret = api_secret
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._clock = clock

    async def get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if path not in ALLOWED_GET_PATHS:
            raise ForbiddenPathError(path)
        query = urlencode(params)
        timestamp = int(self._clock() * 1000)
        headers = {
            "X-BAPI-API-KEY": self._key,
            "X-BAPI-TIMESTAMP": str(timestamp),
            "X-BAPI-RECV-WINDOW": str(RECV_WINDOW_MS),
            "X-BAPI-SIGN": sign(self._secret, timestamp, self._key, RECV_WINDOW_MS, query),
        }
        url = f"{self._base_url}{path}" + (f"?{query}" if query else "")
        response = await self._http.get(url, headers=headers)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        if payload.get("retCode") != 0:
            raise BybitApiError(path, payload.get("retCode"), payload.get("retMsg"))
        result: dict[str, Any] = payload.get("result") or {}
        return result

    async def get_all(
        self, path: str, params: dict[str, str], key: Callable[[dict[str, Any]], object]
    ) -> list[dict[str, Any]]:
        """Every item of a cursor-paginated list, deduplicated by `key`. Bybit returns a
        cursor even after the last page and then repeats rows, so the walk ends at an
        empty cursor or at a page that adds nothing new; it never stops early otherwise."""
        items: dict[object, dict[str, Any]] = {}
        cursor = ""
        for _ in range(_MAX_PAGES):
            page = await self.get(path, {**params, **({"cursor": cursor} if cursor else {})})
            rows = page.get("list") or []
            new = [row for row in rows if key(row) not in items]
            items.update((key(row), row) for row in new)
            cursor = str(page.get("nextPageCursor") or "")
            if not cursor or not new:
                return list(items.values())
        raise BybitApiError(path, "pagination", f"more than {_MAX_PAGES} pages")


@dataclass(frozen=True)
class Snapshot:
    taken_at: datetime
    api_key: dict[str, Any]
    account: dict[str, Any]
    usdt: dict[str, Any] | None
    open_positions: list[dict[str, Any]]
    open_orders: list[dict[str, Any]]
    symbol_positions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


async def take_snapshot(
    client: BybitReadOnlyClient, symbols: Sequence[str], now: Callable[[], datetime]
) -> Snapshot:
    api_key = await client.get("/v5/user/query-api", {})
    account = await client.get("/v5/account/info", {})
    wallet = await client.get(
        "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"}
    )
    coins = [c for a in wallet.get("list") or [] for c in a.get("coin") or []]
    usdt = next((c for c in coins if c.get("coin") == "USDT"), None)
    positions: list[dict[str, Any]] = []
    orders: dict[str, dict[str, Any]] = {}
    for settle in SETTLE_COINS:
        positions += await client.get_all(
            "/v5/position/list",
            {"category": "linear", "settleCoin": settle, "limit": "200"},
            _position_key,
        )
        for order_filter in ORDER_FILTERS:
            for order in await client.get_all(
                "/v5/order/realtime",
                {
                    "category": "linear",
                    "settleCoin": settle,
                    "orderFilter": order_filter,
                    "limit": "50",
                },
                _order_key,
            ):
                orders[str(_order_key(order))] = order
    symbol_positions = {
        symbol: await client.get_all(
            "/v5/position/list", {"category": "linear", "symbol": symbol}, _position_key
        )
        for symbol in symbols
    }
    return Snapshot(
        taken_at=now(),
        api_key=api_key,
        account=account,
        usdt=usdt,
        open_positions=[p for p in positions if _dec(p.get("size")) != 0],
        open_orders=list(orders.values()),
        symbol_positions=symbol_positions,
    )


def _position_key(row: dict[str, Any]) -> object:
    return (row.get("symbol"), str(row.get("positionIdx")))


def _order_key(row: dict[str, Any]) -> object:
    return row.get("orderId")


def _dec(value: object) -> Decimal:
    """Bybit sends numbers as strings, and "" for an absent amount."""
    if value in (None, ""):
        return Decimal(0)
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal("NaN")


def isolated_available_usdt(coin: dict[str, Any]) -> Decimal:
    """Bybit's isolated-margin available balance for one coin: wallet balance minus
    position IM, order IM, locked and bonus."""
    return (
        _dec(coin.get("walletBalance"))
        - _dec(coin.get("totalPositionIM"))
        - _dec(coin.get("totalOrderIM"))
        - _dec(coin.get("locked"))
        - _dec(coin.get("bonus"))
    )


def _key_reasons(api_key: dict[str, Any], purpose: str) -> tuple[list[str], dict[str, Any]]:
    permissions: dict[str, list[str]] = api_key.get("permissions") or {}
    granted = {p for values in permissions.values() for p in values or []}
    read_only = str(api_key.get("readOnly")) == "1"
    ips = [ip for ip in api_key.get("ips") or [] if ip]
    whitelisted = bool(ips) and "*" not in ips
    contract_trade = set(permissions.get("ContractTrade") or [])
    reasons = []
    if "Withdraw" in granted:
        reasons.append("key_can_withdraw")
    if purpose == "diagnostic" and not read_only:
        reasons.append("key_not_read_only")
    if purpose == "live_probe":
        if read_only:
            reasons.append("key_read_only")
        if not {"Order", "Position"} <= contract_trade:
            reasons.append("key_missing_contract_trade")
        if not whitelisted:
            reasons.append("key_no_ip_whitelist")
    facts = {
        "read_only": read_only,
        "can_withdraw": "Withdraw" in granted,
        "ip_whitelisted": whitelisted,
        "contract_trade": sorted(contract_trade),
        "expires_at": api_key.get("expiredAt"),
    }
    return reasons, facts


def evaluate(
    snapshot: Snapshot, *, purpose: str, symbols: Sequence[str], required_usdt: Decimal
) -> dict[str, Any]:
    if purpose not in PURPOSES:
        raise ValueError(f"unknown purpose {purpose!r}")
    reasons, key_facts = _key_reasons(snapshot.api_key, purpose)

    margin_mode = snapshot.account.get("marginMode")
    if margin_mode != "ISOLATED_MARGIN":
        reasons.append(f"margin_mode_not_isolated:{margin_mode}")

    available: Decimal | None = None
    if snapshot.usdt is None:
        reasons.append("usdt_balance_unverified")
    else:
        available = isolated_available_usdt(snapshot.usdt)
        if not available.is_finite():
            reasons.append("usdt_balance_unverified")
        elif available < required_usdt:
            reasons.append("insufficient_isolated_usdt")

    if snapshot.open_positions:
        reasons.append("account_not_empty:positions")
    if snapshot.open_orders:
        reasons.append("account_not_empty:orders")

    if not symbols:
        reasons.append("no_target_symbols")
    symbol_facts: dict[str, Any] = {}
    for symbol in symbols:
        rows = [p for p in snapshot.symbol_positions.get(symbol, []) if p.get("symbol") == symbol]
        if not rows:
            reasons.append(f"symbol_unverified:{symbol}")
            symbol_facts[symbol] = None
            continue
        indexes = sorted({int(_dec(p.get("positionIdx"))) for p in rows})
        leverages = [_dec(p.get("leverage")) for p in rows]
        symbol_facts[symbol] = {"position_idx": indexes, "leverage": [str(x) for x in leverages]}
        if indexes != [0]:
            reasons.append(f"symbol_not_one_way:{symbol}")
        if any(not x.is_finite() or x <= 0 for x in leverages):
            reasons.append(f"symbol_unverified:{symbol}")
        elif any(x > MAX_LEVERAGE for x in leverages):
            reasons.append(f"symbol_leverage_above_1x:{symbol}")

    return {
        "verdict": "blocked" if reasons else "ready",
        "purpose": purpose,
        "snapshot_at": snapshot.taken_at.isoformat(),
        "note": "a snapshot, not a standing permission; re-check before every order",
        "reasons": reasons,
        "facts": {
            "key": key_facts,
            "margin_mode": margin_mode,
            "isolated_available_usdt": None if available is None else str(available),
            "required_usdt": str(required_usdt),
            "open_positions": len(snapshot.open_positions),
            "open_orders": len(snapshot.open_orders),
            "symbols": symbol_facts,
        },
    }


async def run(
    *,
    api_key: str,
    api_secret: str,
    purpose: str,
    symbols: Sequence[str],
    required_usdt: Decimal,
    base_url: str = MAINNET_URL,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as http:
        client = BybitReadOnlyClient(api_key, api_secret, http, base_url=base_url)
        try:
            snapshot = await take_snapshot(client, symbols, lambda: datetime.now(UTC))
        except (BybitApiError, httpx.HTTPError) as exc:
            path = exc.path if isinstance(exc, BybitApiError) else type(exc).__name__
            code = exc.code if isinstance(exc, BybitApiError) else "http"
            return {
                "verdict": "blocked",
                "purpose": purpose,
                "snapshot_at": datetime.now(UTC).isoformat(),
                "reasons": [f"unverified:{path}:{code}"],
                "facts": {},
            }
    return evaluate(snapshot, purpose=purpose, symbols=symbols, required_usdt=required_usdt)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--purpose", choices=PURPOSES, default="diagnostic")
    parser.add_argument("--symbols", default="", help="comma-separated Bybit linear symbols")
    parser.add_argument(
        "--required-usdt", type=Decimal, default=PROBE_NOTIONAL_USD + FEE_RESERVE_USD
    )
    args = parser.parse_args()
    api_key = os.environ.get("BYBIT_API_KEY", "").strip()
    api_secret = os.environ.get("BYBIT_API_SECRET", "").strip()
    if not (api_key and api_secret):
        raise SystemExit("BYBIT_API_KEY and BYBIT_API_SECRET are required")
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    verdict = asyncio.run(
        run(
            api_key=api_key,
            api_secret=api_secret,
            purpose=args.purpose,
            symbols=symbols,
            required_usdt=args.required_usdt,
        )
    )
    sys.stdout.write(json.dumps(verdict, indent=2, sort_keys=True) + "\n")
    raise SystemExit(0 if verdict["verdict"] == "ready" else 2)


if __name__ == "__main__":
    main()
