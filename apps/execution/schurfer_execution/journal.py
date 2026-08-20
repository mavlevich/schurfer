from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

import psycopg
import structlog
from schurfer_performance import (
    LEGACY_ACCOUNTING_VERSION,
    PAPER_ACCOUNTING_VERSION,
    calculate_performance,
)

from .risk import PNL_READY_KEY

log = structlog.get_logger()

_STRATEGY_NAME = "pump_short"
_STRATEGY_VERSION = "1"
_STRATEGY_NAME_MAX_LEN = 64
_STRATEGY_VERSION_MAX_LEN = 16

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
    entry_price, entry_at, entry_slippage_bps, exit_slippage_bps,
    accounting_version, accounting_status, status, setup_context
) VALUES (
    %s, %s, %s, 'perp', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', %s
)
RETURNING id
"""

_CLOSE_TRADE = """
UPDATE app.trades
SET exit_order_id = %s,
    exit_price    = %s,
    exit_at       = %s,
    gross_pnl_usd = %s,
    gross_pnl_pct = %s,
    net_pnl_usd   = %s,
    net_pnl_pct   = %s,
    fees_usd      = %s,
    funding_usd   = %s,
    slippage_usd  = %s,
    pnl_usd       = %s,
    pnl_pct       = %s,
    outcome_label = %s,
    accounting_status = %s,
    accounting_error = %s,
    status        = 'closed',
    notes         = %s,
    updated_at    = now()
WHERE id = %s
  AND status = 'open'
RETURNING id
"""

_INSERT_EXIT_LIQUIDITY = """
INSERT INTO app.trade_exit_liquidity_observations (
    trade_id,
    observed_at,
    exchange,
    symbol,
    market_id,
    status,
    requested_notional_usd,
    filled_notional_usd,
    best_bid,
    best_ask,
    mid,
    spread_bps,
    ask_vwap,
    ask_impact_bps,
    contract_size,
    latency_ms,
    error
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT (trade_id) DO NOTHING
"""

# entry_price/side/size_usd are read back from the trade's own row rather
# than passed in by the caller — Redis (position:entry/position:side) is
# just a cache for the monitor loop, not the source of truth for accounting.
# If that cache is evicted, the close must still be recordable from the DB.
# status is read too so a retry after an ambiguous commit (transaction landed
# server-side but the connection dropped before we got the ack) can detect
# "already closed" and skip re-running the UPDATE — otherwise a retry would
# overwrite exit_at with its own later timestamp, which can shift which UTC
# day the PnL counts toward if the retry lands after midnight.
_SELECT_TRADE_FOR_CLOSE = """
SELECT
    size_usd,
    entry_price,
    side,
    status,
    entry_at,
    entry_slippage_bps,
    exit_slippage_bps,
    accounting_version
FROM app.trades
WHERE id = %s
"""

# Excludes paper (dry-run) trades: setup_context->paper is only set to true
# for paper trades, absent for real ones. Real daily PnL must not be polluted
# by paper losses/gains.
_FIND_OPEN_TRADE = """
SELECT id FROM app.trades
WHERE exchange = %s AND symbol = %s AND status = 'open'
ORDER BY entry_at DESC
LIMIT 1
"""

_REALIZED_PNL_TODAY = """
SELECT COALESCE(SUM(pnl_usd), 0)
FROM app.trades
WHERE status = 'closed'
  AND exit_at >= %s
  AND COALESCE(setup_context->>'paper', 'false') != 'true'
"""


def _optional_non_negative(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def accounting_contract(
    setup_context: dict[str, Any],
) -> tuple[str, str, float | None, float | None]:
    quality = setup_context.get("market_quality")
    quality_data = quality if isinstance(quality, dict) else {}
    entry_slippage_bps = _optional_non_negative(quality_data.get("bid_impact_bps"))
    exit_slippage_bps = _optional_non_negative(quality_data.get("ask_impact_bps"))
    if setup_context.get("paper") is True:
        return (
            PAPER_ACCOUNTING_VERSION,
            "pending",
            entry_slippage_bps,
            exit_slippage_bps,
        )
    return (
        LEGACY_ACCOUNTING_VERSION,
        "legacy",
        entry_slippage_bps,
        exit_slippage_bps,
    )


def strategy_identity(setup_context: dict[str, Any]) -> tuple[str, str]:
    """Return the normalized strategy registry identity without performing I/O."""
    explicit_name = setup_context.get("strategy_name")
    version_value = setup_context.get("strategy_version", _STRATEGY_VERSION)
    strategy_value = setup_context.get("strategy")

    if explicit_name is not None and not isinstance(explicit_name, str):
        raise ValueError("strategy_name must be a string")
    if not isinstance(version_value, str):
        raise ValueError("strategy_version must be a string")
    if strategy_value is not None and not isinstance(strategy_value, str):
        raise ValueError("strategy must be a string")

    strategy_name = explicit_name if explicit_name is not None else _STRATEGY_NAME
    strategy_version = version_value

    # New strategies pass a canonical "name_vN" value in strategy. The pump
    # caller predates that contract and passes the same identifier in
    # strategy_version, so parse it only when no explicit name was supplied.
    strategy_identifier = strategy_value
    if strategy_identifier is None and explicit_name is None and "_v" in strategy_version:
        strategy_identifier = strategy_version
    if strategy_identifier is not None:
        if "_v" not in strategy_identifier:
            raise ValueError("strategy must use the name_vN format")
        strategy_name, strategy_version = strategy_identifier.rsplit("_v", 1)

    strategy_name = strategy_name.strip()
    strategy_version = strategy_version.strip()
    if not strategy_name or not strategy_version:
        raise ValueError("strategy name and version must not be empty")
    if len(strategy_name) > _STRATEGY_NAME_MAX_LEN:
        raise ValueError(f"strategy_name exceeds {_STRATEGY_NAME_MAX_LEN} chars: {strategy_name}")
    if len(strategy_version) > _STRATEGY_VERSION_MAX_LEN:
        raise ValueError(
            f"strategy_version exceeds {_STRATEGY_VERSION_MAX_LEN} chars: {strategy_version}"
        )
    return strategy_name, strategy_version


async def open_trade(
    db_url: str,
    *,
    base: str,
    exchange: str,
    side: str,
    order_id: str | None,
    size_usd: float,
    leverage: int,
    entry_price: float,
    setup_context: dict[str, Any],
) -> int | None:
    if side not in ("long", "short"):
        raise ValueError(f"invalid side: {side}")
    strategy_name, strategy_version = strategy_identity(setup_context)
    try:
        (
            accounting_version,
            accounting_status,
            entry_slippage_bps,
            exit_slippage_bps,
        ) = accounting_contract(setup_context)
        stored_context = {
            **setup_context,
            "accounting_version": accounting_version,
        }
        aconn = await psycopg.AsyncConnection.connect(db_url)

        async with aconn, aconn.cursor() as cur:
            await cur.execute(
                _UPSERT_STRATEGY,
                (strategy_name, strategy_version, f"Auto-registered strategy: {strategy_name}"),
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
                    side,
                    order_id,
                    size_usd,
                    leverage,
                    entry_price,
                    datetime.now(tz=UTC),
                    entry_slippage_bps,
                    exit_slippage_bps,
                    accounting_version,
                    accounting_status,
                    json.dumps(stored_context),
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
    reason: str,
) -> bool:
    """Returns True only if the close was durably committed to the journal.

    entry_price/side/size_usd are loaded from the trade's own row (not
    passed in) so this can always recover a trade by trade_id alone, even
    if the Redis cache of its entry price/side has been evicted.

    Callers must not discard the Redis trade-id pointer (or any other local
    reference needed to retry) unless this returns True — otherwise a DB
    outage at close time permanently loses the ability to record that trade's
    realized PnL, and it silently disappears from the daily loss total.
    """
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(_SELECT_TRADE_FOR_CLOSE, (trade_id,))
            row = await cur.fetchone()
            if row is None:
                log.error("journal.close_trade.trade_not_found", trade_id=trade_id)
                return False
            (
                size_usd_raw,
                entry_price_raw,
                side,
                status,
                entry_at,
                entry_slippage_raw,
                exit_slippage_raw,
                accounting_version,
            ) = row
            size_usd = float(size_usd_raw)
            entry_price = float(entry_price_raw)
            if status == "closed":
                # Idempotent retry: an earlier attempt's transaction actually
                # committed server-side even though we (or a prior caller)
                # never got the ack. Treat as success without touching the
                # row again — re-running the UPDATE would overwrite exit_at
                # with this retry's timestamp and could shift the trade into
                # a different UTC day for realized_pnl_today() purposes.
                log.info("journal.close_trade.already_closed", trade_id=trade_id)
                return True

            closed_at = datetime.now(tz=UTC)
            if accounting_version == PAPER_ACCOUNTING_VERSION:
                duration_minutes = max(0.0, (closed_at - entry_at).total_seconds() / 60)
                accounting = calculate_performance(
                    position_usd=size_usd,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    side=side,
                    duration_minutes=duration_minutes,
                    entry_slippage_bps=(
                        float(entry_slippage_raw) if entry_slippage_raw is not None else None
                    ),
                    exit_slippage_bps=(
                        float(exit_slippage_raw) if exit_slippage_raw is not None else None
                    ),
                )
                gross_pnl_usd = round(accounting.gross_pnl_usd, 4)
                gross_pnl_pct = round(accounting.gross_return_pct, 4)
                net_pnl_usd = (
                    round(accounting.net_pnl_usd, 4) if accounting.net_pnl_usd is not None else None
                )
                net_pnl_pct = (
                    round(accounting.net_return_pct, 4)
                    if accounting.net_return_pct is not None
                    else None
                )
                fees_usd = round(accounting.fees_usd, 4)
                funding_usd = round(accounting.funding_usd, 4)
                slippage_usd = (
                    round(accounting.slippage_usd, 4)
                    if accounting.slippage_usd is not None
                    else None
                )
                pnl_usd = net_pnl_usd
                pnl_pct = net_pnl_pct
                accounting_status = accounting.status
                accounting_error = accounting.error
            else:
                gross_pnl_pct = (
                    (entry_price - exit_price) / entry_price * 100
                    if side == "short"
                    else (exit_price - entry_price) / entry_price * 100
                )
                gross_pnl_usd = round(size_usd * gross_pnl_pct / 100, 4)
                gross_pnl_pct = round(gross_pnl_pct, 4)
                net_pnl_usd = None
                net_pnl_pct = None
                fees_usd = 0.0
                funding_usd = 0.0
                slippage_usd = None
                pnl_usd = gross_pnl_usd
                pnl_pct = gross_pnl_pct
                accounting_status = "legacy"
                accounting_error = None

            outcome_value = net_pnl_pct if net_pnl_pct is not None else gross_pnl_pct
            outcome = "win" if outcome_value > 0 else ("loss" if outcome_value < 0 else "breakeven")

            await cur.execute(
                _CLOSE_TRADE,
                (
                    exit_order_id,
                    exit_price,
                    closed_at,
                    gross_pnl_usd,
                    gross_pnl_pct,
                    net_pnl_usd,
                    net_pnl_pct,
                    fees_usd,
                    funding_usd,
                    slippage_usd,
                    pnl_usd,
                    pnl_pct,
                    outcome,
                    accounting_status,
                    accounting_error,
                    reason,
                    trade_id,
                ),
            )
            updated = await cur.fetchone()
            return updated is not None
    except Exception as exc:
        log.error("journal.close_trade.failed", trade_id=trade_id, err=str(exc))
        return False


async def record_exit_liquidity(
    db_url: str,
    *,
    trade_id: int,
    observation: dict[str, Any],
) -> bool:
    """Persist the first close-time quote without changing the trade close.

    The unique trade_id constraint makes retries append-once. A later retry must
    not replace the point-in-time quote (or failure) seen at the actual close.
    """
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(
                _INSERT_EXIT_LIQUIDITY,
                (
                    trade_id,
                    observation["observed_at"],
                    observation["exchange"],
                    observation["symbol"],
                    observation.get("market_id"),
                    observation["status"],
                    observation["requested_notional_usd"],
                    observation.get("filled_notional_usd"),
                    observation.get("best_bid"),
                    observation.get("best_ask"),
                    observation.get("mid"),
                    observation.get("spread_bps"),
                    observation.get("ask_vwap"),
                    observation.get("ask_impact_bps"),
                    observation.get("contract_size"),
                    observation["latency_ms"],
                    observation.get("error"),
                ),
            )
        return True
    except Exception as exc:
        log.error(
            "journal.exit_liquidity.failed",
            trade_id=trade_id,
            status=observation.get("status"),
            err=str(exc),
        )
        return False


async def find_open_trade_id(db_url: str, *, exchange: str, base: str) -> int | None:
    """Fallback lookup for a close incident whose trade_id wasn't captured at
    creation time (it was created while the matching open was still an
    unresolved-fill incident, before journal.open_trade ran). Looks up the most
    recent open trade for this exchange/base directly from the journal, which
    is authoritative once open_trade has actually run — unlike the Redis
    trade:id cache, this can't have simply never been written yet.
    """
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(_FIND_OPEN_TRADE, (exchange, f"{base.upper()}/USDT:USDT"))
            row = await cur.fetchone()
            return int(row[0]) if row else None
    except Exception as exc:
        log.error("journal.find_open_trade_id.failed", exchange=exchange, base=base, err=str(exc))
        return None


async def realized_pnl_today(db_url: str) -> float | None:
    """Sum of pnl_usd for real (non-paper) trades closed since UTC midnight today.

    Returns None on any DB error. This must NOT be treated as "$0 realized" —
    callers need to distinguish "no loss today" from "couldn't check" and skip
    updating any cached daily-loss figure in the latter case, or a transient
    DB outage would silently reset the daily loss circuit breaker to zero.
    """
    start_of_day = datetime.now(tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(_REALIZED_PNL_TODAY, (start_of_day,))
            row = await cur.fetchone()
            return float(row[0]) if row else 0.0
    except Exception as exc:
        log.error("journal.realized_pnl_today.failed", err=str(exc))
        return None


_PENDING_CLOSE_KEY = "journal:pending_close:{exchange}:{base}:{trade_id}"
_PENDING_CLOSE_TTL = 86400 * 3  # retry window before this needs human attention


def _pending_close_key(exchange: str, base: str, trade_id: int) -> str:
    return _PENDING_CLOSE_KEY.format(exchange=exchange, base=base, trade_id=trade_id)


async def write_pending_close(
    rdb: Any,
    *,
    exchange: str,
    base: str,
    trade_id: int,
    exit_order_id: str | None,
    exit_price: float,
    reason: str,
) -> None:
    """Durable marker for a close that's confirmed on the exchange but not
    yet committed to the journal. Carries everything needed to retry the
    commit on its own — independent of any other Redis state — so callers
    are free to clean up position-monitoring keys immediately once the
    exchange-side close is confirmed, regardless of journal write outcome.

    Also revokes the readiness lease here (not just at the top of
    try_commit_close): the pnl tracker runs as a concurrent asyncio task, and
    a slow close_trade() DB call can overlap with a tracker tick that reads
    "no pending closes" before this marker exists, then republishes the
    lease. Revoking again the instant the marker actually lands narrows that
    window to the tracker's own re-check gap instead of this call's full
    DB round-trip duration.
    """
    payload = json.dumps(
        {
            "trade_id": trade_id,
            "exit_order_id": exit_order_id,
            "exit_price": exit_price,
            "reason": reason,
        }
    )
    await rdb.set(_pending_close_key(exchange, base, trade_id), payload, ex=_PENDING_CLOSE_TTL)
    await revoke_pnl_readiness(rdb)


async def revoke_pnl_readiness(rdb: Any) -> None:
    """Immediately invalidate the daily-PnL-is-fresh lease.

    Called the moment a real position close is confirmed (or even just
    detected but not yet priced) — the cached daily_pnl figure is stale from
    that instant, not just once the tracker's next tick notices. Without
    this, an existing lease (up to PNL_READY_TTL seconds old) would keep
    allowing new trades in the window before the tracker recomputes.
    """
    await rdb.delete(PNL_READY_KEY)


async def try_commit_close(
    db_url: str,
    rdb: Any,
    *,
    exchange: str,
    base: str,
    trade_id: int,
    exit_order_id: str | None,
    exit_price: float,
    reason: str,
) -> bool:
    """Attempt to commit a close to the journal; on failure, durably records
    it as pending instead of losing it. Returns True only if committed now —
    callers must not discard their own trade-id pointer unless this is True.

    Always revokes the PnL-readiness lease first — the cached daily_pnl is
    stale the instant a real close happens, whether or not the DB write
    itself succeeds.
    """
    await revoke_pnl_readiness(rdb)
    committed = await close_trade(
        db_url,
        trade_id=trade_id,
        exit_order_id=exit_order_id,
        exit_price=exit_price,
        reason=reason,
    )
    if committed:
        await rdb.delete(_pending_close_key(exchange, base, trade_id))
    else:
        await write_pending_close(
            rdb,
            exchange=exchange,
            base=base,
            trade_id=trade_id,
            exit_order_id=exit_order_id,
            exit_price=exit_price,
            reason=reason,
        )
    return committed


def pending_close_key_pattern() -> str:
    return _PENDING_CLOSE_KEY.format(exchange="*", base="*", trade_id="*")


def parse_pending_close_key(key: str) -> tuple[str, str, int] | None:
    # journal:pending_close:{exchange}:{base}:{trade_id}
    parts = key.split(":")
    if len(parts) != 5:
        return None
    try:
        trade_id = int(parts[4])
    except ValueError:
        return None
    return parts[2], parts[3], trade_id


# Atomic compare-and-delete: only remove the trade-id pointer if it still
# points at the trade we just closed. Without this, a slow retry of an old
# pending close could delete a newer trade's pointer if a new position
# opened on the same exchange:base symbol in the meantime.
_CAS_DELETE = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


async def delete_trade_id_if_matches(rdb: Any, key: str, expected_trade_id: int) -> bool:
    result = await rdb.eval(_CAS_DELETE, 1, key, str(expected_trade_id))
    return bool(result)


async def any_pending_closes(rdb: Any) -> bool:
    """True if at least one close is still waiting to be committed to the
    journal. The tracker must not declare daily PnL fresh/ready while this
    is true — a pending close's loss isn't reflected in realized_pnl_today()
    yet, since its trades row is still 'open' in the DB."""
    async for _ in rdb.scan_iter(match=pending_close_key_pattern()):
        return True
    return False
