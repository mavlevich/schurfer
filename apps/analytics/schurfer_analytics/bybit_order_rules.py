"""Bybit linear order rules: hourly capture and USD 50 order sizing (HYP-015 confirmation).

The confirmation design (docs/research/hyp015-confirmation-stage-v1.md) prices each
paper entry as an order the venue would accept: USD 50 at the entry price, rounded down
to the quantity step, refused below the minimum quantity or the minimum order value, or
above the market-order maximum. Paper rows do not carry these rules, so this captures
them beside the paper workers (which it never touches), from public
`/v5/market/instruments-info?category=linear`:

- one capture pass reads every cursor page; a repeated cursor, a non-zero `retCode` or
  a malformed row fails the pass, which is recorded as `failed` and maps no symbol;
- each instrument's raw row is keyed by its SHA-256 and stored once in
  `app.bybit_order_rule_defs`; a complete pass stores only `symbol -> key` in
  `app.bybit_order_rule_captures`;
- `rule_at` answers "the rules of this symbol at this instant" from the newest complete
  capture at or before it and no older than `MAX_RULE_AGE`, or not at all.

`size_order` refuses whatever it cannot prove: no rule, a stale capture, a symbol the
venue does not list as trading, or a missing step or minimum is a refused entry, never a
feasible one. It reads no paper row, no return and no price history.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

SOURCE_VERSION = "bybit_order_rules_v1"
CATEGORY = "linear"
PAGE_LIMIT = 1000
MAX_PAGES = 20
# The capture runs hourly; a decision is sized only from a pass at most this old.
MAX_RULE_AGE = timedelta(hours=2)
ORDER_NOTIONAL_USD = Decimal(50)
TRADING = "Trading"


class CaptureError(RuntimeError):
    """The venue's answer could not be read completely; the pass maps no symbol."""


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def rule_sha256(item: dict[str, Any]) -> str:
    """The key of one raw instruments-info row: any change of any field is a new rule."""
    body = json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


@dataclass(frozen=True)
class OrderRule:
    symbol: str
    status: str | None
    qty_step: Decimal | None
    min_order_qty: Decimal | None
    max_mkt_order_qty: Decimal | None
    min_notional_usd: Decimal | None
    tick_size: Decimal | None


def parse_rule(item: dict[str, Any]) -> OrderRule:
    raw_lot, raw_price = item.get("lotSizeFilter"), item.get("priceFilter")
    lot: dict[str, Any] = raw_lot if isinstance(raw_lot, dict) else {}
    price: dict[str, Any] = raw_price if isinstance(raw_price, dict) else {}
    status = item.get("status")
    return OrderRule(
        symbol=str(item["symbol"]),
        status=status if isinstance(status, str) else None,
        qty_step=_decimal(lot.get("qtyStep")),
        min_order_qty=_decimal(lot.get("minOrderQty")),
        max_mkt_order_qty=_decimal(lot.get("maxMktOrderQty")),
        min_notional_usd=_decimal(lot.get("minNotionalValue")),
        tick_size=_decimal(price.get("tickSize")),
    )


@dataclass(frozen=True)
class OrderSizing:
    feasible: bool
    reason: str | None
    qty: Decimal | None = None
    notional_usd: Decimal | None = None


def size_order(
    rule: OrderRule | None, *, price: Decimal, notional_usd: Decimal = ORDER_NOTIONAL_USD
) -> OrderSizing:
    """A market order for `notional_usd` at `price`, as the venue would take it, or the
    reason it would not. The quantity is rounded down to the step, so the order never
    exceeds the notional; the returned notional is the rounded one."""
    if rule is None:
        return OrderSizing(False, "rules_missing")
    if rule.status != TRADING:
        return OrderSizing(False, "not_trading")
    if rule.qty_step is None or rule.min_order_qty is None or rule.min_notional_usd is None:
        return OrderSizing(False, "rules_incomplete")
    if not price.is_finite() or price <= 0:
        return OrderSizing(False, "price_invalid")
    steps = (notional_usd / price / rule.qty_step).to_integral_value(rounding=ROUND_FLOOR)
    qty = steps * rule.qty_step
    if qty < rule.min_order_qty:
        return OrderSizing(False, "below_min_qty", qty, qty * price)
    if rule.max_mkt_order_qty is not None and qty > rule.max_mkt_order_qty:
        return OrderSizing(False, "above_max_market_qty", qty, qty * price)
    if qty * price < rule.min_notional_usd:
        return OrderSizing(False, "below_min_notional", qty, qty * price)
    return OrderSizing(True, None, qty, qty * price)


async def fetch_linear(client: Any) -> tuple[list[dict[str, Any]], int]:
    """Every linear instruments-info row, by complete, non-repeating cursor traversal."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor = ""
    for requests in range(1, MAX_PAGES + 1):
        params: dict[str, Any] = {"category": CATEGORY, "limit": PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        page = await client.public_get_v5_market_instruments_info(params)
        if not isinstance(page, dict) or str(page.get("retCode")) != "0":
            message = page.get("retMsg") if isinstance(page, dict) else None
            raise CaptureError(f"instruments-info error: {message!r}")
        result = page.get("result")
        if not isinstance(result, dict):
            raise CaptureError("instruments-info returned a malformed page")
        items = result.get("list")
        if not isinstance(items, list) or not all(
            isinstance(item, dict) and isinstance(item.get("symbol"), str) and item["symbol"]
            for item in items
        ):
            raise CaptureError("instruments-info returned a malformed page")
        rows.extend(items)
        next_cursor = result.get("nextPageCursor") or ""
        if not next_cursor:
            symbols = [item["symbol"] for item in rows]
            if len(set(symbols)) != len(symbols):
                raise CaptureError("instruments-info listed a symbol twice")
            return rows, requests
        if next_cursor in seen:
            raise CaptureError("instruments-info cursor repeated")
        seen.add(next_cursor)
        cursor = next_cursor
    raise CaptureError(f"instruments-info did not end within {MAX_PAGES} pages")


async def capture(db_url: str, client: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """One pass: fetch, store new rule definitions, then record the pass. A failed fetch
    is recorded as `failed` and maps no symbol."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from .outcome_repository import async_database_url

    try:
        rows, requests = await fetch_linear(client)
        error = None
    except CaptureError as exc:
        rows, requests, error = [], 0, str(exc)[:1000]
    # Stamped when the answer is in hand, so no decision is sized from rules fetched later.
    captured = (now or datetime.now(UTC)).astimezone(UTC)
    members = {str(item["symbol"]): rule_sha256(item) for item in rows}
    engine = create_async_engine(async_database_url(db_url), pool_pre_ping=True, pool_size=1)
    new_rules = 0
    try:
        async with engine.begin() as conn:
            for item in rows:
                rule = parse_rule(item)
                result = await conn.execute(
                    text(
                        """
                        INSERT INTO app.bybit_order_rule_defs
                            (rule_sha256, symbol, status, qty_step, min_order_qty,
                             max_mkt_order_qty, min_notional_usd, tick_size, raw)
                        VALUES (:sha, :symbol, :status, :step, :min_qty, :max_mkt,
                                :min_notional, :tick, CAST(:raw AS jsonb))
                        ON CONFLICT (rule_sha256) DO NOTHING
                        """
                    ),
                    {
                        "sha": members[rule.symbol],
                        "symbol": rule.symbol,
                        "status": rule.status,
                        "step": rule.qty_step,
                        "min_qty": rule.min_order_qty,
                        "max_mkt": rule.max_mkt_order_qty,
                        "min_notional": rule.min_notional_usd,
                        "tick": rule.tick_size,
                        "raw": json.dumps(item, sort_keys=True, ensure_ascii=False),
                    },
                )
                new_rules += result.rowcount or 0
            await conn.execute(
                text(
                    """
                    INSERT INTO app.bybit_order_rule_captures
                        (captured_at, status, request_count, instrument_count, members,
                         error, source_version)
                    VALUES (:at, :status, :requests, :count, CAST(:members AS jsonb),
                            :error, :version)
                    """
                ),
                {
                    "at": captured,
                    "status": "failed" if error else "complete",
                    "requests": requests,
                    "count": len(members),
                    "members": json.dumps(members, sort_keys=True),
                    "error": error,
                    "version": SOURCE_VERSION,
                },
            )
    finally:
        await engine.dispose()
    return {
        "status": "failed" if error else "complete",
        "captured_at": captured.isoformat(),
        "instruments": len(members),
        "new_rules": new_rules,
        "requests": requests,
        "error": error,
    }


async def rule_at(
    conn: Any, symbol: str, at: datetime, *, max_age: timedelta = MAX_RULE_AGE
) -> OrderRule | None:
    """The rules of `symbol` at `at`: from the newest complete capture at or before `at`,
    if it is no older than `max_age` and lists the symbol. Otherwise None."""
    from sqlalchemy import text

    row = (
        await conn.execute(
            text(
                """
                WITH latest AS (
                    SELECT members FROM app.bybit_order_rule_captures
                    WHERE source_version = :version AND status = 'complete'
                      AND captured_at <= :at AND captured_at >= :oldest
                    ORDER BY captured_at DESC LIMIT 1
                )
                SELECT d.raw FROM latest
                JOIN app.bybit_order_rule_defs d ON d.rule_sha256 = latest.members ->> :symbol
                """
            ),
            {
                "version": SOURCE_VERSION,
                "at": at,
                "oldest": at - max_age,
                "symbol": symbol,
            },
        )
    ).first()
    if row is None:
        return None
    raw = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    return parse_rule(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.parse_args(argv)
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.stderr.write("DATABASE_URL is required for the order-rule capture\n")
        return 1

    async def run() -> dict[str, Any]:
        from .exchange_registry import EXCHANGE_FACTORIES

        client = EXCHANGE_FACTORIES["bybit"]()
        try:
            return await capture(db_url, client)
        finally:
            await client.close()

    summary = asyncio.run(run())
    sys.stdout.write(json.dumps(summary, sort_keys=True) + "\n")
    return 0 if summary["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
