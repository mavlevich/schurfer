from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import psycopg
import structlog

log = structlog.get_logger()

_STRATEGY_NAME = "pump_short"
_STRATEGY_VERSION = "1"

_UPSERT_STRATEGY = """
INSERT INTO app.strategies (name, version, description)
VALUES (%s, %s, %s)
ON CONFLICT (name, version) DO UPDATE SET updated_at = now()
RETURNING id
"""

_INSERT_TRADE = """
INSERT INTO app.trades (
    strategy_id, symbol, exchange, market_type, side,
    entry_order_id, size_usd, leverage,
    entry_price, entry_at, status, setup_context
) VALUES (%s, %s, %s, 'perp', 'short', %s, %s, %s, %s, %s, 'open', %s)
RETURNING id
"""

_CLOSE_TRADE = """
UPDATE app.trades
SET exit_order_id = %s,
    exit_price    = %s,
    exit_at       = %s,
    pnl_usd       = %s,
    pnl_pct       = %s,
    outcome_label = %s,
    status        = 'closed',
    notes         = %s,
    updated_at    = now()
WHERE id = %s
"""


async def open_trade(
    db_url: str,
    *,
    base: str,
    exchange: str,
    order_id: str | None,
    size_usd: float,
    leverage: int,
    entry_price: float,
    setup_context: dict[str, Any],
) -> int | None:
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(
                _UPSERT_STRATEGY,
                (_STRATEGY_NAME, _STRATEGY_VERSION, "Auto-short on pump score"),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            strategy_id = row[0]

            await cur.execute(
                _INSERT_TRADE,
                (
                    strategy_id,
                    f"{base.upper()}/USDT:USDT",
                    exchange,
                    order_id,
                    size_usd,
                    leverage,
                    entry_price,
                    datetime.now(tz=UTC),
                    json.dumps(setup_context),
                ),
            )
            row = await cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        log.error("journal.open_trade.failed", base=base, exchange=exchange, err=str(exc))
        return None


async def close_trade(
    db_url: str,
    *,
    trade_id: int,
    exit_order_id: str | None,
    exit_price: float,
    entry_price: float,
    side: str,
    reason: str,
) -> None:
    pnl_pct = (
        (entry_price - exit_price) / entry_price * 100
        if side == "short"
        else (exit_price - entry_price) / entry_price * 100
    )
    outcome = "win" if pnl_pct > 0 else ("loss" if pnl_pct < 0 else "breakeven")

    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(
                _CLOSE_TRADE,
                (
                    exit_order_id,
                    exit_price,
                    datetime.now(tz=UTC),
                    None,  # pnl_usd filled later by tracker
                    round(pnl_pct, 4),
                    outcome,
                    reason,
                    trade_id,
                ),
            )
    except Exception as exc:
        log.error("journal.close_trade.failed", trade_id=trade_id, err=str(exc))
