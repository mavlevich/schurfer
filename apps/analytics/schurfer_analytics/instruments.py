"""Exchange-instrument identity normalization for durable pump attribution."""

import math
from datetime import UTC, datetime
from typing import Any

_ONBOARD_FIELDS: dict[str, tuple[str, ...]] = {
    "binance": ("onboardDate",),
    "bybit": ("launchTime",),
    "okx": ("listTime",),
    "gate": ("launch_time", "create_time"),
    "bitget": ("launchTime",),
    "mexc": ("createTime",),
    "bingx": ("launchTime",),
    "phemex": ("listTime",),
    "htx": ("create_date",),
    "xt": ("onboardDate",),
    "toobit": ("launchTime",),
    "blofin": ("listTime",),
}

_DISPLAY_FIELDS = (
    "displayName",
    "displayNameEn",
    "displaySymbol",
    "symbolName",
    "enName",
    "name",
    "index_name",
)


def _timestamp_ms(value: Any) -> int | None:
    """Normalize known exchange timestamp representations to Unix milliseconds."""
    if value in (None, "", 0, "0"):
        return None
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        try:
            parsed_date = datetime.strptime(text, "%Y%m%d").replace(tzinfo=UTC)
        except ValueError:
            return None
        return int(parsed_date.timestamp() * 1000)
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        return None
    if not 1_000_000_000 <= parsed < 100_000_000_000_000:
        return None
    return int(parsed * 1000 if parsed < 1_000_000_000_000 else parsed)


def _market_type(market: dict[str, Any]) -> str:
    for market_type in ("swap", "future", "spot", "option"):
        if market.get(market_type) is True:
            return market_type
    value = market.get("type")
    return str(value) if value else "unknown"


def _display_name(info: dict[str, Any]) -> str | None:
    for field in _DISPLAY_FIELDS:
        value = info.get(field)
        if value not in (None, ""):
            return str(value).strip() or None
    return None


def _onboarded_at_ms(exchange: str, info: dict[str, Any]) -> int | None:
    for field in _ONBOARD_FIELDS.get(exchange, ()):
        timestamp = _timestamp_ms(info.get(field))
        if timestamp is not None:
            return timestamp
    return None


def instrument_metadata(
    exchange: str,
    unified_symbol: str,
    market: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return stable exchange-instrument fields without claiming canonical equivalence."""
    market = market or {}
    info = market.get("info")
    info = info if isinstance(info, dict) else {}
    market_id = str(market.get("id") or unified_symbol)
    market_type = _market_type(market)
    onboarded_at_ms = _onboarded_at_ms(exchange, info)
    identity_version = str(onboarded_at_ms) if onboarded_at_ms is not None else "unknown"
    return {
        "identity_key": f"{exchange}:{market_type}:{market_id}:{identity_version}",
        "market_id": market_id,
        "unified_symbol": str(market.get("symbol") or unified_symbol),
        "display_name": _display_name(info),
        "market_type": market_type,
        "base_asset": str(market.get("base") or unified_symbol.split("/", 1)[0]),
        "quote_asset": str(market.get("quote") or "") or None,
        "settle_asset": str(market.get("settle") or "") or None,
        "contract_size": _positive_float(market.get("contractSize")),
        "onboarded_at_ms": onboarded_at_ms,
    }


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 and math.isfinite(parsed) else None
