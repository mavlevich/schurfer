from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
import structlog
from psycopg.rows import dict_row
from schurfer_performance import (
    LEGACY_ACCOUNTING_VERSION,
    PAPER_ACCOUNTING_VERSION,
    calculate_performance,
)

from . import liquidity
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
    exit_slippage_bps = %s,
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
    bid_vwap,
    bid_impact_bps,
    ask_vwap,
    ask_impact_bps,
    contract_size,
    latency_ms,
    error
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
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
#
# exit_slippage_bps is deliberately NOT selected here. The value stored on
# the row at open_trade() time is an entry-time proxy (whatever the opposite
# book side looked like before the position existed) — using it for exit
# accounting silently substitutes a stale number for the real fill. Exit
# slippage must come from a fresh order-book capture taken at close time
# (see close_trade's fresh_exit_slippage_bps parameter); if that capture
# failed or wasn't provided, net accounting correctly falls back to
# "incomplete" rather than reusing the proxy.
_SELECT_TRADE_FOR_CLOSE = """
SELECT
    size_usd,
    entry_price,
    side,
    status,
    entry_at,
    entry_slippage_bps,
    accounting_version,
    gross_pnl_usd,
    gross_pnl_pct,
    net_pnl_usd,
    net_pnl_pct,
    fees_usd,
    funding_usd,
    slippage_usd,
    accounting_status
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
    *,
    side: str = "short",
) -> tuple[str, str, float | None, float | None]:
    """Derive accounting version/status and the two entry-time slippage legs.

    market_quality (when present) carries both bid_impact_bps and
    ask_impact_bps from a single pre-trade snapshot. Which one is the entry
    leg depends on side: a SHORT enters by selling (bid), a LONG enters by
    buying (ask) — see liquidity.book_side_for. The exit leg read here is
    only ever the same entry-time snapshot's opposite side, kept as a rough
    at-open estimate; close_trade() overrides it with a fresh at-close
    capture and never trusts this value for the final net PnL.

    entry_price_includes_impact=True (set by a caller that already priced
    its entry off an executed VWAP across the book — see early_momentum.py)
    means the entry-side impact is already baked into the stored entry
    price via that VWAP. Also charging market_quality's entry-side
    impact_bps here would subtract the same cost twice: once implicitly
    (gross PnL uses the VWAP-adjusted entry price) and once explicitly (the
    slippage_bps term in calculate_performance). entry_slippage_bps is then
    a known 0.0 -- not a missing reading, so accounting still reaches
    "complete".
    """
    quality = setup_context.get("market_quality")
    quality_data = quality if isinstance(quality, dict) else {}
    entry_side = liquidity.book_side_for(position_side=side, leg="entry")
    exit_side = liquidity.book_side_for(position_side=side, leg="exit")
    if setup_context.get("entry_price_includes_impact") is True:
        entry_slippage_bps: float | None = 0.0
    else:
        entry_slippage_bps = _optional_non_negative(quality_data.get(f"{entry_side}_impact_bps"))
    exit_slippage_bps = _optional_non_negative(quality_data.get(f"{exit_side}_impact_bps"))
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


async def ensure_strategy(db_url: str, *, name: str, version: str) -> int | None:
    """Idempotent upsert-by-natural-key into the strategy registry, exposed
    standalone for callers (e.g. early_momentum.py's episode-lifecycle path)
    that need a strategy_id before any trade row exists yet -- open_trade/
    open_trade_for_episode do this same upsert inline for their own callers.

    Writes on every call (touches updated_at even when nothing changed) --
    never call this from a read-only path (health checks, HTTP GETs). Use
    `find_strategy_id` there instead."""
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as aconn, aconn.cursor() as cur:
            await cur.execute(
                _UPSERT_STRATEGY, (name, version, f"Auto-registered strategy: {name}")
            )
            row = await cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        log.error("journal.ensure_strategy.failed", name=name, version=version, err=str(exc))
        return None


_SELECT_STRATEGY_ID = "SELECT id FROM app.strategies WHERE name = %s AND version = %s"


async def find_strategy_id(db_url: str, *, name: str, version: str) -> int | None:
    """Read-only lookup -- unlike `ensure_strategy`, never writes (no
    upsert, no `updated_at` touch). Returns None both when the strategy
    hasn't been registered yet and on a DB error; a read-only health path
    can't create the registry row itself, so "not found yet" and "DB
    unreachable" are handled identically by callers (colleague review:
    health checks must never perform a database write)."""
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as aconn, aconn.cursor() as cur:
            await cur.execute(_SELECT_STRATEGY_ID, (name, version))
            row = await cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        log.error("journal.find_strategy_id.failed", name=name, version=version, err=str(exc))
        return None


async def open_trade(
    db_url: str,
    *,
    symbol: str,
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
        ) = accounting_contract(setup_context, side=side)
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
                    symbol,
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
        log.error("journal.open_trade.failed", symbol=symbol, exchange=exchange, err=str(exc))
        return None


_INSERT_TRADE_FOR_EPISODE = """
INSERT INTO app.trades (
    strategy_id, symbol, exchange, market_type, side,
    entry_order_id, size_usd, leverage,
    entry_price, entry_at, entry_slippage_bps, exit_slippage_bps,
    accounting_version, accounting_status, status, setup_context,
    episode_id, entry_idempotency_key
) VALUES (
    %s, %s, %s, 'perp', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', %s, %s, %s
)
ON CONFLICT (entry_idempotency_key) WHERE entry_idempotency_key IS NOT NULL
DO NOTHING
RETURNING id
"""

_SELECT_TRADE_BY_IDEMPOTENCY_KEY = """
SELECT id, episode_id, strategy_id, symbol
FROM app.trades
WHERE entry_idempotency_key = %s
"""

# FOR UPDATE holds the row lock for the rest of this transaction, so nothing
# else can change this episode's claim between here and the mark-opened
# UPDATE below -- correct even under real concurrency, not just today's
# single-worker deployment.
#
# lease_fresh/window_fresh are computed by the DB's own now(), not the app
# server's clock (avoids clock skew), so a lease that expired *after* the
# caller's claim_episode() succeeded but *before* this commit -- e.g. a slow
# quote/liquidity check ate the whole lease window -- is still caught here,
# not just at claim time. Without this, a stale claim could still open a
# trade even though reap_overdue/list_actionable have already decided the
# episode is up for reclaim by someone else.
_SELECT_EPISODE_FOR_UPDATE = """
SELECT status, (claim_expires_at > now()) AS lease_fresh, (expires_at > now()) AS window_fresh
FROM app.early_momentum_episodes
WHERE episode_id = %s AND claim_token = %s
FOR UPDATE
"""

_MARK_EPISODE_OPENED = """
UPDATE app.early_momentum_episodes
SET status = 'opened'
WHERE episode_id = %s AND claim_token = %s
"""


@dataclass(frozen=True)
class OpenTradeOutcome:
    """Result of open_trade_for_episode.

    trade_id is None whenever nothing was durably opened -- either the claim
    was no longer valid (claim_valid=False, e.g. reclaimed/expired under the
    caller), or the idempotency-key row that already existed belongs to a
    different episode/strategy/instrument than expected (a key collision,
    never silently trusted -- see the mismatch check below).
    """

    trade_id: int | None
    created: bool
    recovered: bool
    claim_valid: bool


async def open_trade_for_episode(
    db_url: str,
    *,
    episode_id: str,
    claim_token: str,
    symbol: str,
    exchange: str,
    side: str,
    size_usd: float,
    leverage: int,
    entry_price: float,
    entry_idempotency_key: str,
    setup_context: dict[str, Any],
) -> OpenTradeOutcome:
    """Atomically: verify the claim is still valid, idempotently insert (or
    recover) the trade row, and mark the episode 'opened' -- all in one
    transaction, so a crash between any two of those steps is impossible
    (either the whole thing commits, or none of it does). A failed/expired
    claim aborts before any trade row is ever written.

    Does not touch or replace journal.open_trade — that function's
    `int | None` return type is relied on verbatim by every existing caller
    (pump-short, liquidation_cascade); changing it would silently break them.
    This is a separate, additive path used only by early_momentum_v3.
    """
    if side not in ("long", "short"):
        raise ValueError(f"invalid side: {side}")
    strategy_name, strategy_version = strategy_identity(setup_context)
    try:
        (
            accounting_version,
            accounting_status,
            entry_slippage_bps,
            exit_slippage_bps,
        ) = accounting_contract(setup_context, side=side)
        stored_context = {**setup_context, "accounting_version": accounting_version}

        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(_SELECT_EPISODE_FOR_UPDATE, (episode_id, claim_token))
            episode_row = await cur.fetchone()
            # 'opened' is accepted alongside 'claimed': a retry after this
            # exact claim's own attempt already committed (caller crashed or
            # lost the ack before finding out) must still recover via the
            # entry_idempotency_key path below, not be rejected as invalid --
            # and an already-opened episode's lease is irrelevant by then, so
            # it skips the freshness check entirely. A 'claimed' episode,
            # though, must still have a live lease *and* a live episode
            # window at this exact moment -- not just at claim time -- or a
            # slow quote/liquidity check could open a trade against an
            # episode someone else has already reclaimed or that reap_overdue
            # has already decided to terminate. Any other status with this
            # claim_token (expired/rejected/suppressed) means the lease
            # genuinely lapsed and was reaped away -- correctly invalid.
            status = episode_row[0] if episode_row is not None else None
            claim_still_live = episode_row is not None and (
                status == "opened" or (status == "claimed" and episode_row[1] and episode_row[2])
            )
            if not claim_still_live:
                log.warning(
                    "journal.open_trade_for_episode.claim_invalid",
                    episode_id=episode_id,
                    status=status,
                )
                return OpenTradeOutcome(
                    trade_id=None, created=False, recovered=False, claim_valid=False
                )

            await cur.execute(
                _UPSERT_STRATEGY,
                (strategy_name, strategy_version, f"Auto-registered strategy: {strategy_name}"),
            )
            strategy_row = await cur.fetchone()
            if strategy_row is None:
                return OpenTradeOutcome(
                    trade_id=None, created=False, recovered=False, claim_valid=True
                )
            strategy_id = strategy_row[0]

            await cur.execute(
                _INSERT_TRADE_FOR_EPISODE,
                (
                    strategy_id,
                    symbol,
                    exchange,
                    side,
                    None,
                    size_usd,
                    leverage,
                    entry_price,
                    datetime.now(tz=UTC),
                    entry_slippage_bps,
                    exit_slippage_bps,
                    accounting_version,
                    accounting_status,
                    json.dumps(stored_context),
                    episode_id,
                    entry_idempotency_key,
                ),
            )
            inserted = await cur.fetchone()
            if inserted is not None:
                trade_id = inserted[0]
                created = True
                recovered = False
            else:
                # Idempotent retry: a prior attempt already inserted this
                # exact entry_idempotency_key. Recover its id -- but only
                # after confirming it's genuinely the same episode/strategy/
                # instrument, never trusting a key match alone (a collision
                # must be a hard error, not a silently wrong trade_id).
                await cur.execute(_SELECT_TRADE_BY_IDEMPOTENCY_KEY, (entry_idempotency_key,))
                existing = await cur.fetchone()
                if existing is None:
                    log.error(
                        "journal.open_trade_for_episode.idempotency_key_vanished",
                        episode_id=episode_id,
                    )
                    return OpenTradeOutcome(
                        trade_id=None, created=False, recovered=False, claim_valid=True
                    )
                existing_id, existing_episode_id, existing_strategy_id, existing_symbol = existing
                if (
                    str(existing_episode_id) != str(episode_id)
                    or existing_strategy_id != strategy_id
                    or existing_symbol != symbol
                ):
                    log.error(
                        "journal.open_trade_for_episode.idempotency_key_collision",
                        episode_id=episode_id,
                        existing_episode_id=str(existing_episode_id),
                    )
                    return OpenTradeOutcome(
                        trade_id=None, created=False, recovered=False, claim_valid=True
                    )
                trade_id = existing_id
                created = False
                recovered = True

            await cur.execute(_MARK_EPISODE_OPENED, (episode_id, claim_token))
            return OpenTradeOutcome(
                trade_id=trade_id, created=created, recovered=recovered, claim_valid=True
            )
    except Exception as exc:
        log.error(
            "journal.open_trade_for_episode.failed",
            episode_id=episode_id,
            symbol=symbol,
            exchange=exchange,
            err=str(exc),
        )
        return OpenTradeOutcome(trade_id=None, created=False, recovered=False, claim_valid=False)


@dataclass(frozen=True)
class CloseOutcome:
    """Everything a caller needs to report a close, computed exactly once.

    `committed` mirrors the old bool contract: callers must not discard a
    Redis trade-id pointer (or retry state) unless this is True. The
    accounting fields are the SAME numbers written to the DB row — a caller
    reporting to Telegram (or anywhere else) must read them from here rather
    than recomputing independently, so the two can never diverge.

    `newly_closed` distinguishes a real first-time close (True) from a retry
    of an already-closed trade (False, accounting fields are the row's own
    previously-persisted values, read back verbatim -- not recomputed
    against exit_price, which a retry may only know as a later, different
    ticker read). This is NOT the same fact as "a notification was already
    delivered" -- a caller must not use it to suppress a retry's Telegram
    message, only to know whether to skip re-running side effects that
    assume a fresh close (e.g. episode-lifecycle transitions).
    """

    committed: bool
    newly_closed: bool = True
    gross_pnl_usd: float | None = None
    gross_pnl_pct: float | None = None
    net_pnl_usd: float | None = None
    net_pnl_pct: float | None = None
    fees_usd: float | None = None
    funding_usd: float | None = None
    slippage_usd: float | None = None
    accounting_status: str | None = None


def _exit_liquidity_params(trade_id: int, observation: dict[str, Any]) -> tuple[Any, ...]:
    return (
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
        observation.get("bid_vwap"),
        observation.get("bid_impact_bps"),
        observation.get("ask_vwap"),
        observation.get("ask_impact_bps"),
        observation.get("contract_size"),
        observation["latency_ms"],
        observation.get("error"),
    )


async def close_trade(
    db_url: str,
    *,
    trade_id: int,
    exit_order_id: str | None,
    exit_price: float,
    reason: str,
    fresh_exit_slippage_bps: float | None = None,
    exit_observation: dict[str, Any] | None = None,
) -> CloseOutcome:
    """Commit a close to the journal, returning the accounting actually written.

    entry_price/side/size_usd are loaded from the trade's own row (not
    passed in) so this can always recover a trade by trade_id alone, even
    if the Redis cache of its entry price/side has been evicted.

    Callers must not discard the Redis trade-id pointer (or any other local
    reference needed to retry) unless `.committed` is True — otherwise a DB
    outage at close time permanently loses the ability to record that trade's
    realized PnL, and it silently disappears from the daily loss total.

    fresh_exit_slippage_bps must come from an order-book capture taken at
    (or immediately before) close time — never from the trade row's entry-time
    snapshot. When None (capture failed, insufficient depth, or the caller has
    no fresh reading — e.g. a real/legacy-accounting close), net accounting
    correctly falls back to "incomplete" rather than reusing a stale proxy.

    exit_observation, when given, is written to
    app.trade_exit_liquidity_observations in the SAME transaction as the
    trades UPDATE — both commit or neither does, so the evidence row can
    never silently drift out of sync with the close it documents.
    """
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(_SELECT_TRADE_FOR_CLOSE, (trade_id,))
            row = await cur.fetchone()
            if row is None:
                log.error("journal.close_trade.trade_not_found", trade_id=trade_id)
                return CloseOutcome(committed=False)
            (
                size_usd_raw,
                entry_price_raw,
                side,
                status,
                entry_at,
                entry_slippage_raw,
                accounting_version,
                saved_gross_pnl_usd,
                saved_gross_pnl_pct,
                saved_net_pnl_usd,
                saved_net_pnl_pct,
                saved_fees_usd,
                saved_funding_usd,
                saved_slippage_usd,
                saved_accounting_status,
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
                # Return the row's own saved accounting verbatim (never
                # recomputed against this retry's exit_price, which may be a
                # later, different ticker read) so a caller can still report
                # the close accurately — newly_closed=False only means "not
                # freshly closed by this call," not "already notified."
                log.info("journal.close_trade.already_closed", trade_id=trade_id)
                return CloseOutcome(
                    committed=True,
                    newly_closed=False,
                    gross_pnl_usd=(
                        float(saved_gross_pnl_usd) if saved_gross_pnl_usd is not None else None
                    ),
                    gross_pnl_pct=(
                        float(saved_gross_pnl_pct) if saved_gross_pnl_pct is not None else None
                    ),
                    net_pnl_usd=float(saved_net_pnl_usd) if saved_net_pnl_usd is not None else None,
                    net_pnl_pct=float(saved_net_pnl_pct) if saved_net_pnl_pct is not None else None,
                    fees_usd=float(saved_fees_usd) if saved_fees_usd is not None else None,
                    funding_usd=float(saved_funding_usd) if saved_funding_usd is not None else None,
                    slippage_usd=(
                        float(saved_slippage_usd) if saved_slippage_usd is not None else None
                    ),
                    accounting_status=saved_accounting_status,
                )

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
                    exit_slippage_bps=fresh_exit_slippage_bps,
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
                    fresh_exit_slippage_bps,
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
            if updated is None:
                return CloseOutcome(committed=False)

            if exit_observation is not None:
                # Same connection, same not-yet-committed transaction as the
                # UPDATE above — both land together on `async with aconn`'s
                # clean exit, or neither does on an exception.
                await cur.execute(
                    _INSERT_EXIT_LIQUIDITY,
                    _exit_liquidity_params(trade_id, exit_observation),
                )

            return CloseOutcome(
                committed=True,
                gross_pnl_usd=gross_pnl_usd,
                gross_pnl_pct=gross_pnl_pct,
                net_pnl_usd=net_pnl_usd,
                net_pnl_pct=net_pnl_pct,
                fees_usd=fees_usd,
                funding_usd=funding_usd,
                slippage_usd=slippage_usd,
                accounting_status=accounting_status,
            )
    except Exception as exc:
        log.error("journal.close_trade.failed", trade_id=trade_id, err=str(exc))
        return CloseOutcome(committed=False)


async def record_exit_liquidity(
    db_url: str,
    *,
    trade_id: int,
    observation: dict[str, Any],
) -> bool:
    """Persist a close-time quote independently of a trade close.

    The unique trade_id constraint makes retries append-once. A later retry must
    not replace the point-in-time quote (or failure) seen at the actual close.

    close_trade()'s own `exit_observation` parameter is the atomic path used
    for a normal paper close (same transaction as the trades UPDATE); this
    standalone entry point exists for any caller that needs to persist an
    observation on its own connection instead.
    """
    try:
        aconn = await psycopg.AsyncConnection.connect(db_url)
        async with aconn, aconn.cursor() as cur:
            await cur.execute(
                _INSERT_EXIT_LIQUIDITY,
                _exit_liquidity_params(trade_id, observation),
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


async def find_open_trade_id(db_url: str, *, exchange: str, symbol: str) -> int | None:
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
            await cur.execute(_FIND_OPEN_TRADE, (exchange, symbol))
            row = await cur.fetchone()
            return int(row[0]) if row else None
    except Exception as exc:
        log.error(
            "journal.find_open_trade_id.failed", exchange=exchange, symbol=symbol, err=str(exc)
        )
        return None


_FIND_OPEN_EPISODE_TRADES = """
SELECT id, symbol, exchange, side, entry_price, size_usd, leverage, entry_at,
       entry_slippage_bps, exit_slippage_bps, accounting_version, setup_context, episode_id
FROM app.trades
WHERE status = 'open'
  AND episode_id IS NOT NULL
  AND setup_context ->> 'paper' = 'true'
"""


@dataclass(frozen=True)
class OpenEpisodeTrade:
    """An early_momentum_v3 paper trade that is durably 'open' in Postgres --
    used to detect and repair a Redis position key that a crash between
    open_trade_for_episode's commit and paper.py's rdb.set(...) left
    missing (colleague review: without this, that trade is never picked up
    by list_actionable either, since it's already 'opened' not 'armed'/
    'claimed' -- it would silently never be monitored for TP/SL/max-hold)."""

    trade_id: int
    symbol: str
    exchange: str
    side: str
    entry_price: float
    size_usd: float
    leverage: int
    entry_at: datetime
    entry_slippage_bps: float | None
    exit_slippage_bps: float | None
    accounting_version: str
    setup_context: dict[str, Any]
    episode_id: str


async def find_open_episode_trades(db_url: str) -> list[OpenEpisodeTrade]:
    try:
        async with (
            await psycopg.AsyncConnection.connect(db_url, row_factory=dict_row) as aconn,
            aconn.cursor() as cur,
        ):
            await cur.execute(_FIND_OPEN_EPISODE_TRADES)
            rows = await cur.fetchall()
    except Exception as exc:
        log.error("journal.find_open_episode_trades.failed", err=str(exc))
        return []
    return [
        OpenEpisodeTrade(
            trade_id=row["id"],
            symbol=row["symbol"],
            exchange=row["exchange"],
            side=row["side"],
            entry_price=float(row["entry_price"]),
            size_usd=float(row["size_usd"]),
            leverage=int(row["leverage"]),
            entry_at=row["entry_at"],
            entry_slippage_bps=(
                float(row["entry_slippage_bps"]) if row["entry_slippage_bps"] is not None else None
            ),
            exit_slippage_bps=(
                float(row["exit_slippage_bps"]) if row["exit_slippage_bps"] is not None else None
            ),
            accounting_version=row["accounting_version"],
            setup_context=row["setup_context"],
            episode_id=str(row["episode_id"]),
        )
        for row in rows
    ]


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
    outcome = await close_trade(
        db_url,
        trade_id=trade_id,
        exit_order_id=exit_order_id,
        exit_price=exit_price,
        reason=reason,
    )
    committed = outcome.committed
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
