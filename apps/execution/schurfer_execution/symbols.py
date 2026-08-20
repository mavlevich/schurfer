"""Canonical instrument identity for cross-exchange execution.

Replaces string-guessing heuristics with exact CCXT market metadata and DB identity routing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg


@dataclass(frozen=True)
class ExecutionInstrument:
    """Exact, resolved execution routing for a given trade."""

    exchange: str
    symbol: str  # CCXT unified, e.g., "DOGE/USDT:USDT"
    native_market_id: str  # e.g., "DOGEUSDT"
    base: str  # e.g., "DOGE"
    quote: str  # e.g., "USDT"
    settle: str  # e.g., "USDT"
    market_type: str  # e.g., "swap"


@dataclass(frozen=True)
class ResolvedRoute:
    """An explicit cross-venue identity route."""

    source_exchange: str
    source_native_id: str
    source_identity_key: str
    execution_exchange: str
    execution_native_id: str
    execution_identity_key: str
    cluster_key: str


async def resolve_route(
    db_url: str,
    source_exchange: str,
    source_native_market_id: str,
    execution_exchange: str,
) -> ResolvedRoute | None:
    """Return the unique confirmed native-market route between two venues.

    The identity table deliberately stores native ids and durable identity keys,
    not CCXT unified symbols.  The returned ``execution_native_id`` must therefore
    be resolved against the already-loaded target exchange metadata before use.
    Missing and ambiguous routes both fail closed by returning ``None``.
    """
    query = """
        SELECT
            a.identity_key,
            b.native_market_id,
            b.identity_key,
            a.cluster_key
        FROM app.momentum_universe_cluster_members a
        JOIN app.momentum_universe_cluster_members b ON a.cluster_key = b.cluster_key
        WHERE a.exchange = %s
          AND a.native_market_id = %s
          AND b.exchange = %s
          AND a.match_status = 'confirmed'
          AND b.match_status = 'confirmed'
    """
    async with (
        await psycopg.AsyncConnection.connect(db_url) as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(
            query,
            (source_exchange, source_native_market_id, execution_exchange),
        )
        rows = await cur.fetchall()

    if len(rows) != 1:
        return None

    source_identity_key, execution_native_id, execution_identity_key, cluster_key = rows[0]
    return ResolvedRoute(
        source_exchange=source_exchange,
        source_native_id=source_native_market_id,
        source_identity_key=source_identity_key,
        execution_exchange=execution_exchange,
        execution_native_id=execution_native_id,
        execution_identity_key=execution_identity_key,
        cluster_key=cluster_key,
    )


def resolve_execution_instrument(
    exchange_client: Any,
    source_symbol: str,
    required_quote: str = "USDT",
    required_settle: str = "USDT",
    required_type: str = "swap",
) -> ExecutionInstrument:
    """Resolve a raw symbol to a definitive execution instrument using CCXT market metadata.

    This is strictly for legacy payload fallback (e.g. recovering from a stale Redis position).
    For cross-venue signal triggering, use `resolve_route` instead.
    """
    markets = getattr(exchange_client, "markets", None)
    if not markets:
        raise RuntimeError(f"Markets not loaded for exchange {exchange_client.id}")

    matches: list[dict[str, Any]] = []

    for market in markets.values():
        if not market.get("active", True):
            continue

        identity_matches = (
            source_symbol == market.get("id")
            or source_symbol == market.get("symbol")
            or source_symbol.upper() == market.get("base", "")
        )
        contract_matches = (
            market.get("quote") == required_quote
            and market.get("settle") == required_settle
            and market.get("type") == required_type
        )
        if identity_matches and contract_matches:
            matches.append(market)

    if not matches:
        raise ValueError(
            f"Cannot resolve '{source_symbol}' on {exchange_client.id} "
            f"(require quote={required_quote}, settle={required_settle}, type={required_type})"
        )

    if len(matches) > 1:
        symbols_matched = ", ".join(m["symbol"] for m in matches)
        raise ValueError(
            f"Ambiguous symbol '{source_symbol}' on {exchange_client.id}: "
            f"matches {len(matches)} markets ({symbols_matched})"
        )

    m = matches[0]
    return ExecutionInstrument(
        exchange=exchange_client.id,
        symbol=m["symbol"],
        native_market_id=m["id"],
        base=m["base"],
        quote=m["quote"],
        settle=m["settle"],
        market_type=m["type"],
    )
