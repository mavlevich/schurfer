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
- isolated-available USDT covers at least MIN_REQUIRED_USDT, the probe notional plus a fee
  reserve (wallet balance minus position IM, order IM, locked and bonus, as Bybit documents
  for isolated margin); the CLI can raise that floor, never lower it;
- the dedicated account is empty: no position in any category (linear USDT and USDC,
  inverse, option) and no open order of any kind in any category (linear, inverse, spot,
  option, with every order filter), each list read through every page;
- each target symbol reads back as one-way (`positionIdx` 0) with leverage at most 1x.

Fail closed: a field the verdict depends on that is missing or not a number, and any
pagination the walk cannot prove complete, give `blocked`, never a default value.
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
MIN_REQUIRED_USDT = PROBE_NOTIONAL_USD + FEE_RESERVE_USD
MAX_LEVERAGE = Decimal(1)
# Every position list and every open-order list the account can hold. The linear lists
# need a settle coin; each (category, filter) pair below was checked against the live API.
POSITION_SCOPES: tuple[dict[str, str], ...] = (
    {"category": "linear", "settleCoin": "USDT"},
    {"category": "linear", "settleCoin": "USDC"},
    {"category": "inverse"},
    {"category": "option"},
)
_DERIVATIVE_FILTERS = ("", "StopOrder", "tpslOrder")
ORDER_SCOPES: tuple[dict[str, str], ...] = (
    *(
        {"category": "linear", "settleCoin": coin, **({"orderFilter": f} if f else {})}
        for coin in ("USDT", "USDC")
        for f in _DERIVATIVE_FILTERS
    ),
    *({"category": "inverse", **({"orderFilter": f} if f else {})} for f in _DERIVATIVE_FILTERS),
    *(
        {"category": "spot", **({"orderFilter": f} if f else {})}
        for f in ("", "StopOrder", "tpslOrder", "OcoOrder")
    ),
    {"category": "option"},
)
BALANCE_FIELDS = ("walletBalance", "totalPositionIM", "totalOrderIM", "locked", "bonus")
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
        allowed_paths: frozenset[str] = ALLOWED_GET_PATHS,
    ) -> None:
        self._allowed = allowed_paths
        self._key = api_key
        self._secret = api_secret
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._clock = clock

    async def get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if path not in self._allowed:
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
        """Every item of a cursor-paginated list, deduplicated by `key` (Bybit repeats rows
        on a trailing page). The walk follows the cursor until Bybit returns none. A cursor
        seen before or more than _MAX_PAGES pages cannot be proven complete and raises,
        which the caller turns into `blocked`."""
        items: dict[object, dict[str, Any]] = {}
        cursor = ""
        seen: set[str] = set()
        for _ in range(_MAX_PAGES):
            page = await self.get(path, {**params, **({"cursor": cursor} if cursor else {})})
            for row in page.get("list") or []:
                items.setdefault(key(row), row)
            cursor = str(page.get("nextPageCursor") or "")
            if not cursor:
                return list(items.values())
            if cursor in seen:
                raise BybitApiError(path, "pagination", "cursor repeated")
            seen.add(cursor)
        raise BybitApiError(path, "pagination", f"more than {_MAX_PAGES} pages")


@dataclass(frozen=True)
class Snapshot:
    taken_at: datetime
    api_key: dict[str, Any]
    account: dict[str, Any]
    usdt: dict[str, Any] | None
    positions: list[dict[str, Any]]
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
    for scope in POSITION_SCOPES:
        rows = await client.get_all("/v5/position/list", {**scope, "limit": "200"}, _position_key)
        positions += [{**row, "_category": scope["category"]} for row in rows]
    orders: dict[object, dict[str, Any]] = {}
    for scope in ORDER_SCOPES:
        for order in await client.get_all(
            "/v5/order/realtime", {**scope, "limit": "50"}, _order_key
        ):
            orders[(scope["category"], _order_key(order))] = order
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
        positions=positions,
        open_orders=list(orders.values()),
        symbol_positions=symbol_positions,
    )


def _position_key(row: dict[str, Any]) -> object:
    return (row.get("symbol"), str(row.get("positionIdx")))


def _order_key(row: dict[str, Any]) -> object:
    return row.get("orderId")


def _dec(value: object) -> Decimal | None:
    """A finite number sent as a string, or None when missing, empty or not a number:
    a field the verdict depends on is never defaulted."""
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def isolated_available_usdt(coin: dict[str, Any]) -> Decimal | None:
    """Bybit's isolated-margin available balance for one coin: wallet balance minus
    position IM, order IM, locked and bonus. None when any component is unreadable."""
    parts = [_dec(coin.get(name)) for name in BALANCE_FIELDS]
    if any(part is None for part in parts):
        return None
    wallet, position_im, order_im, locked, bonus = (p for p in parts if p is not None)
    return wallet - position_im - order_im - locked - bonus


def _key_reasons(api_key: dict[str, Any], purpose: str) -> tuple[list[str], dict[str, Any]]:
    permissions = api_key.get("permissions")
    read_only_raw = str(api_key.get("readOnly"))
    reasons = []
    if not isinstance(permissions, dict):
        reasons.append("key_permissions_unverified")
        permissions = {}
    if read_only_raw not in ("0", "1"):
        reasons.append("key_read_only_unverified")
    granted = {p for values in permissions.values() for p in values or []}
    read_only = read_only_raw == "1"
    ips = [ip for ip in api_key.get("ips") or [] if ip]
    whitelisted = bool(ips) and "*" not in ips
    contract_trade = set(permissions.get("ContractTrade") or [])
    if "Withdraw" in granted:
        reasons.append("key_can_withdraw")
    if purpose == "diagnostic" and read_only_raw == "0":
        reasons.append("key_not_read_only")
    if purpose == "live_probe":
        if read_only_raw == "1":
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
    if required_usdt < MIN_REQUIRED_USDT:
        raise ValueError(f"required_usdt may not go below {MIN_REQUIRED_USDT}")
    reasons, key_facts = _key_reasons(snapshot.api_key, purpose)

    margin_mode = snapshot.account.get("marginMode")
    if margin_mode != "ISOLATED_MARGIN":
        reasons.append(f"margin_mode_not_isolated:{margin_mode}")

    available = None if snapshot.usdt is None else isolated_available_usdt(snapshot.usdt)
    if available is None:
        reasons.append("usdt_balance_unverified")
    elif available < required_usdt:
        reasons.append("insufficient_isolated_usdt")

    sizes = [_dec(p.get("size")) for p in snapshot.positions]
    open_positions = sum(1 for size in sizes if size is not None and size != 0)
    if any(size is None for size in sizes):
        reasons.append("position_size_unverified")
    if open_positions:
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
        raw_indexes = [_dec(p.get("positionIdx")) for p in rows]
        leverages = [_dec(p.get("leverage")) for p in rows]
        symbol_facts[symbol] = {
            "position_idx": [None if x is None else str(x) for x in raw_indexes],
            "leverage": [None if x is None else str(x) for x in leverages],
        }
        if any(x is None for x in raw_indexes) or any(x is None or x <= 0 for x in leverages):
            reasons.append(f"symbol_unverified:{symbol}")
            continue
        if {x for x in raw_indexes if x is not None} != {Decimal(0)}:
            reasons.append(f"symbol_not_one_way:{symbol}")
        if any(x is not None and x > MAX_LEVERAGE for x in leverages):
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
            "open_positions": open_positions,
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
        "--required-usdt",
        type=Decimal,
        default=MIN_REQUIRED_USDT,
        help=f"raise the isolated USDT floor; it never goes below {MIN_REQUIRED_USDT}",
    )
    args = parser.parse_args()
    if args.required_usdt < MIN_REQUIRED_USDT:
        parser.error(f"--required-usdt may not go below {MIN_REQUIRED_USDT}")
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
