"""Durable candidate -> armed -> claimed -> opened lifecycle for early_momentum_v3.

Postgres is the source of truth for this lifecycle. Redis (the WATCH cache and
the paper position) is a repairable cache of it, never the only place a live
episode can be found -- see `list_actionable`. A partial unique index enforces
at most one live (armed/claimed) episode per instrument, and `claim_episode`
is a single atomic UPDATE so a crashed worker's stale claim can be safely
reclaimed by a later tick without a second, race-prone "release" step.

`status`/`terminal_reason` are deliberately separate columns rather than one
enum with a value per rejection cause, so the state machine and its CHECK
constraints stay small while the reason vocabulary can grow freely. See
migration 0032 for the full schema and its reasoning.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import psycopg
import structlog
from psycopg.rows import dict_row
from psycopg.types.json import Json

if TYPE_CHECKING:
    from datetime import datetime

log = structlog.get_logger()

# Pre-trade rejection reasons (status='rejected').
REASON_IDENTITY_UNRESOLVED = "identity_unresolved"
REASON_ROUTE_INVALIDATED = "route_invalidated"
REASON_IDENTITY_CATALOG_STALE = "identity_catalog_stale"
REASON_QUOTE_TIMEOUT = "quote_timeout"
REASON_INVALID_ORDER_BOOK = "invalid_order_book"
REASON_INSUFFICIENT_DEPTH = "insufficient_depth"
REASON_SPREAD_TOO_WIDE = "spread_too_wide"
REASON_IMPACT_TOO_HIGH = "impact_too_high"
REASON_INFRASTRUCTURE_FAILURE = "infrastructure_failure"
REASON_ALREADY_OPEN = "already_open"
REASON_REARM_COOLDOWN = "rearm_cooldown"
# status='expired' / status='suppressed' reasons.
REASON_EXPIRED_WITHOUT_BREAKOUT = "expired_without_breakout"
REASON_EXPIRED_WHILE_CLAIMED = "expired_while_claimed"
REASON_POSITION_EXISTS = "position_exists"

STATUS_ARMED = "armed"
STATUS_CLAIMED = "claimed"
STATUS_OPENED = "opened"
STATUS_CLOSED = "closed"
STATUS_EXPIRED = "expired"
STATUS_REJECTED = "rejected"
STATUS_SUPPRESSED = "suppressed"


@dataclass(frozen=True)
class Episode:
    episode_id: str
    strategy_id: int
    contract_sha256: bytes
    source_exchange: str
    source_native_id: str
    exchange: str
    native_market_id: str
    execution_symbol: str | None
    execution_identity_key: str
    source_identity_key: str
    cluster_key: str
    ceiling: float
    features: dict[str, Any]
    armed_at: datetime
    expires_at: datetime
    status: str
    terminal_reason: str | None
    claim_token: str | None
    claimed_at: datetime | None
    claim_expires_at: datetime | None
    claim_attempts: int


@dataclass(frozen=True)
class ClaimOutcome:
    claimed: bool
    episode: Episode | None
    claim_token: str | None


@dataclass(frozen=True)
class ReapSummary:
    expired_armed: int
    expired_while_claimed: int
    infrastructure_failed_claims: int


def _row_to_episode(row: dict[str, Any]) -> Episode:
    return Episode(
        episode_id=str(row["episode_id"]),
        strategy_id=row["strategy_id"],
        contract_sha256=bytes(row["contract_sha256"]),
        source_exchange=row["source_exchange"],
        source_native_id=row["source_native_id"],
        exchange=row["exchange"],
        native_market_id=row["native_market_id"],
        execution_symbol=row["execution_symbol"],
        execution_identity_key=row["execution_identity_key"],
        source_identity_key=row["source_identity_key"],
        cluster_key=row["cluster_key"],
        ceiling=float(row["ceiling"]),
        features=row["features"],
        armed_at=row["armed_at"],
        expires_at=row["expires_at"],
        status=row["status"],
        terminal_reason=row["terminal_reason"],
        claim_token=str(row["claim_token"]) if row["claim_token"] is not None else None,
        claimed_at=row["claimed_at"],
        claim_expires_at=row["claim_expires_at"],
        claim_attempts=row["claim_attempts"],
    )


_INSERT_EPISODE = """
INSERT INTO app.early_momentum_episodes (
    episode_id, strategy_id, contract_sha256, source_exchange, source_native_id,
    exchange, native_market_id, execution_symbol, execution_identity_key,
    source_identity_key, cluster_key, ceiling, features, expires_at, status
) VALUES (
    %(episode_id)s, %(strategy_id)s, %(contract_sha256)s, %(source_exchange)s, %(source_native_id)s,
    %(exchange)s, %(native_market_id)s, %(execution_symbol)s, %(execution_identity_key)s,
    %(source_identity_key)s, %(cluster_key)s, %(ceiling)s, %(features)s,
    now() + make_interval(secs := %(ttl_seconds)s), 'armed'
)
ON CONFLICT (exchange, native_market_id) WHERE status IN ('armed', 'claimed')
DO NOTHING
RETURNING *
"""


async def create_episode(
    db_url: str,
    *,
    strategy_id: int,
    contract_sha256: bytes,
    source_exchange: str,
    source_native_id: str,
    exchange: str,
    native_market_id: str,
    execution_symbol: str | None,
    execution_identity_key: str,
    source_identity_key: str,
    cluster_key: str,
    ceiling: float,
    features: dict[str, Any],
    ttl_seconds: int,
) -> Episode | None:
    """Arm a new episode, or return None if this instrument is already watched.

    The `None` case is the immutable-WATCH rule enforced by Postgres: a
    repeat scanner tick on an instrument that already has a live (armed or
    claimed) episode changes nothing -- no new ceiling, no new expiry, no
    second episode. Route/identity resolution must already have succeeded by
    the time this is called; a caller whose resolution failed calls
    `create_rejected_episode` instead, so a row's initial status is always
    correct on first write (no separate 'pending' state).
    """
    episode_id = str(uuid.uuid4())
    try:
        async with (
            await psycopg.AsyncConnection.connect(db_url, row_factory=dict_row) as aconn,
            aconn.cursor() as cur,
        ):
            await cur.execute(
                _INSERT_EPISODE,
                {
                    "episode_id": episode_id,
                    "strategy_id": strategy_id,
                    "contract_sha256": contract_sha256,
                    "source_exchange": source_exchange,
                    "source_native_id": source_native_id,
                    "exchange": exchange,
                    "native_market_id": native_market_id,
                    "execution_symbol": execution_symbol,
                    "execution_identity_key": execution_identity_key,
                    "source_identity_key": source_identity_key,
                    "cluster_key": cluster_key,
                    "ceiling": ceiling,
                    "features": Json(features),
                    "ttl_seconds": ttl_seconds,
                },
            )
            row = await cur.fetchone()
    except Exception as exc:
        log.error(
            "episodes.create_failed",
            exchange=exchange,
            native_market_id=native_market_id,
            err=str(exc),
        )
        return None
    if row is None:
        return None
    return _row_to_episode(row)


# A still-disqualified candidate re-evaluates as a rejection on every 60s
# scanner tick -- without a dedup guard that's one new row per tick per
# instrument for as long as the disqualification lasts (hours, sometimes
# indefinitely for a symbol that never clusters), pure table bloat with no
# new information in it (colleague review). WHERE NOT EXISTS makes the skip
# atomic with the insert -- no separate check-then-insert race. Keyed by
# source_exchange/source_native_id (always known, even when route
# resolution never got far enough to produce a native_market_id) rather
# than exchange/native_market_id.
_REJECTED_EPISODE_DEDUP_WINDOW_SECONDS = 3600

_INSERT_REJECTED_EPISODE = """
INSERT INTO app.early_momentum_episodes (
    episode_id, strategy_id, contract_sha256, source_exchange, source_native_id,
    exchange, native_market_id, execution_symbol, execution_identity_key,
    source_identity_key, cluster_key, ceiling, features, expires_at, status, terminal_reason
)
SELECT
    %(episode_id)s, %(strategy_id)s, %(contract_sha256)s, %(source_exchange)s, %(source_native_id)s,
    %(exchange)s, %(native_market_id)s, %(execution_symbol)s, %(execution_identity_key)s,
    %(source_identity_key)s, %(cluster_key)s, %(ceiling)s, %(features)s,
    now(), 'rejected', %(reason)s
WHERE NOT EXISTS (
    SELECT 1 FROM app.early_momentum_episodes
    WHERE source_exchange = %(dedup_source_exchange)s
      AND source_native_id = %(dedup_source_native_id)s
      AND terminal_reason = %(dedup_reason)s
      AND created_at > now() - make_interval(secs := %(dedup_window_seconds)s)
)
"""


async def create_rejected_episode(
    db_url: str,
    *,
    strategy_id: int,
    contract_sha256: bytes,
    source_exchange: str,
    source_native_id: str,
    exchange: str,
    native_market_id: str,
    ceiling: float,
    features: dict[str, Any],
    reason: str,
    execution_symbol: str | None = None,
    execution_identity_key: str = "",
    source_identity_key: str = "",
    cluster_key: str = "",
    dedup_window_seconds: int = _REJECTED_EPISODE_DEDUP_WINDOW_SECONDS,
) -> bool:
    """Durably record a candidate that never reached 'armed' (route/identity
    unresolved, or the catalog was too stale to trust) -- this is a rejection,
    not a silent no-op, so it's visible in the same episode-scoped queries as
    everything else. execution/identity fields are best-effort ("" when
    resolution never got far enough to produce them).

    A repeat rejection for the same (source_exchange, source_native_id,
    reason) within dedup_window_seconds of the last one is silently skipped
    -- returns True either way (skipping is a successful outcome, not a
    failure); only a real DB error returns False."""
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as aconn, aconn.cursor() as cur:
            await cur.execute(
                _INSERT_REJECTED_EPISODE,
                {
                    "episode_id": str(uuid.uuid4()),
                    "strategy_id": strategy_id,
                    "contract_sha256": contract_sha256,
                    "source_exchange": source_exchange,
                    "source_native_id": source_native_id,
                    "exchange": exchange,
                    "native_market_id": native_market_id or source_native_id,
                    "execution_symbol": execution_symbol,
                    "execution_identity_key": execution_identity_key,
                    "source_identity_key": source_identity_key,
                    "cluster_key": cluster_key,
                    "ceiling": ceiling,
                    "features": Json(features),
                    "reason": reason,
                    "dedup_source_exchange": source_exchange,
                    "dedup_source_native_id": source_native_id,
                    "dedup_reason": reason,
                    "dedup_window_seconds": dedup_window_seconds,
                },
            )
        return True
    except Exception as exc:
        log.error(
            "episodes.create_rejected_failed",
            exchange=exchange,
            source_native_id=source_native_id,
            reason=reason,
            err=str(exc),
        )
        return False


_CLAIM_EPISODE = """
UPDATE app.early_momentum_episodes
SET status = 'claimed',
    claim_token = %(token)s,
    claimed_at = now(),
    claim_expires_at = now() + make_interval(secs := %(lease_seconds)s),
    claim_attempts = claim_attempts + 1,
    updated_at = now()
WHERE episode_id = %(episode_id)s
  AND expires_at > now()
  AND (
      status = 'armed'
      OR (status = 'claimed' AND claim_expires_at < now())
  )
RETURNING *
"""


async def claim_episode(db_url: str, *, episode_id: str, lease_seconds: int = 30) -> ClaimOutcome:
    """Atomically claim an armed episode, or reclaim one whose prior claim's
    lease already expired. `expires_at > now()` gates BOTH branches: a
    genuinely expired episode's stale claim must terminate (via reap_overdue),
    never get reclaimed here -- see migration 0032's docstring."""
    token = str(uuid.uuid4())
    try:
        async with (
            await psycopg.AsyncConnection.connect(db_url, row_factory=dict_row) as aconn,
            aconn.cursor() as cur,
        ):
            await cur.execute(
                _CLAIM_EPISODE,
                {"token": token, "lease_seconds": lease_seconds, "episode_id": episode_id},
            )
            row = await cur.fetchone()
    except Exception as exc:
        log.error("episodes.claim_failed", episode_id=episode_id, err=str(exc))
        return ClaimOutcome(claimed=False, episode=None, claim_token=None)
    if row is None:
        return ClaimOutcome(claimed=False, episode=None, claim_token=None)
    return ClaimOutcome(claimed=True, episode=_row_to_episode(row), claim_token=token)


_TERMINATE_CLAIMED = """
UPDATE app.early_momentum_episodes
SET status = %(status)s, terminal_reason = %(reason)s, updated_at = now()
WHERE episode_id = %(episode_id)s AND claim_token = %(claim_token)s
"""

_TERMINATE_ARMED = """
UPDATE app.early_momentum_episodes
SET status = %(status)s, terminal_reason = %(reason)s, updated_at = now()
WHERE episode_id = %(episode_id)s AND status = 'armed'
"""


async def terminate_episode(
    db_url: str,
    *,
    episode_id: str,
    reason: str,
    claim_token: str | None = None,
    status: str = STATUS_REJECTED,
) -> bool:
    """Move an episode to a terminal status immediately (never left to lease
    expiry). Pass `claim_token` for a post-claim failure (guards against
    acting on a since-reclaimed episode); omit it for a pre-claim rejection
    (route/identity/staleness), which is only allowed while still 'armed'."""
    sql, params = (
        (_TERMINATE_CLAIMED, {"claim_token": claim_token})
        if claim_token is not None
        else (_TERMINATE_ARMED, {})
    )
    params = {"episode_id": episode_id, "status": status, "reason": reason, **params}
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as aconn, aconn.cursor() as cur:
            await cur.execute(sql, params)
            return cur.rowcount > 0
    except Exception as exc:
        log.error("episodes.terminate_failed", episode_id=episode_id, reason=reason, err=str(exc))
        return False


_MARK_CLOSED = """
UPDATE app.early_momentum_episodes
SET status = 'closed', updated_at = now()
WHERE episode_id = %(episode_id)s AND status = 'opened'
"""


async def mark_closed(db_url: str, *, episode_id: str) -> bool:
    """Best-effort denormalization once the episode's trade closes -- nothing
    depends on this being perfectly in sync; app.trades.status is what's
    actually authoritative for whether a position is open."""
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as aconn, aconn.cursor() as cur:
            await cur.execute(_MARK_CLOSED, {"episode_id": episode_id})
            return cur.rowcount > 0
    except Exception as exc:
        log.warning("episodes.mark_closed_failed", episode_id=episode_id, err=str(exc))
        return False


_SET_EXECUTION_SYMBOL = """
UPDATE app.early_momentum_episodes
SET execution_symbol = %(execution_symbol)s, updated_at = now()
WHERE episode_id = %(episode_id)s
"""


async def set_execution_symbol(db_url: str, *, episode_id: str, execution_symbol: str) -> bool:
    """Backfill the CCXT-unified symbol once a live exchange client resolves
    it at claim time (the scanner has no client to derive it from at ARM
    time). Best-effort denormalization -- entry_idempotency_key/claim_token
    are what actually gate the open, not this field."""
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as aconn, aconn.cursor() as cur:
            await cur.execute(
                _SET_EXECUTION_SYMBOL,
                {"episode_id": episode_id, "execution_symbol": execution_symbol},
            )
            return cur.rowcount > 0
    except Exception as exc:
        log.warning("episodes.set_execution_symbol_failed", episode_id=episode_id, err=str(exc))
        return False


_REAP_EXPIRED_ARMED = """
UPDATE app.early_momentum_episodes
SET status = 'expired', terminal_reason = %(reason)s, updated_at = now()
WHERE episode_id IN (
    SELECT episode_id FROM app.early_momentum_episodes
    WHERE status = 'armed' AND expires_at < now()
    LIMIT %(batch_size)s
)
"""

_REAP_INFRASTRUCTURE_FAILED_CLAIMS = """
UPDATE app.early_momentum_episodes
SET status = 'rejected', terminal_reason = %(reason)s, updated_at = now()
WHERE episode_id IN (
    SELECT episode_id FROM app.early_momentum_episodes
    WHERE status = 'claimed'
      AND expires_at > now()
      AND claim_expires_at < now()
      AND claim_attempts >= %(max_attempts)s
    LIMIT %(batch_size)s
)
"""

# A claimed row whose own episode window (expires_at) has already passed is
# reaped unconditionally, regardless of claim_attempts -- without this, such
# a row hangs forever: list_actionable's old claimed-branch (before this fix)
# had no expires_at check either, so it kept re-caching the WATCH key every
# tick; claim_episode's own WHERE clause requires expires_at > now(), so
# every reclaim attempt matches zero rows and never increments
# claim_attempts, so the infrastructure-failure branch above never fires
# either -- a permanent stuck loop (colleague review).
_REAP_EXPIRED_WHILE_CLAIMED = """
UPDATE app.early_momentum_episodes
SET status = 'expired', terminal_reason = %(reason)s, updated_at = now()
WHERE episode_id IN (
    SELECT episode_id FROM app.early_momentum_episodes
    WHERE status = 'claimed' AND expires_at < now()
    LIMIT %(batch_size)s
)
"""


async def reap_overdue(
    db_url: str, *, batch_size: int = 200, max_claim_attempts: int = 5
) -> ReapSummary:
    """Terminate the truly-dead cases -- an armed row past its own expiry, a
    claimed row whose own episode window ran out (regardless of
    claim_attempts), or a claimed row whose lease expired at least
    max_claim_attempts times over while the episode window itself is still
    open. A claimed-and-lease-expired row UNDER that attempts cap, with the
    episode window still open, is deliberately left alone: it's exactly what
    claim_episode's own reclaim branch (and list_actionable's discovery of
    it) exists to pick back up, not something to give up on just because the
    worker that held it may be gone."""
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as aconn, aconn.cursor() as cur:
            await cur.execute(
                _REAP_EXPIRED_ARMED,
                {"reason": REASON_EXPIRED_WITHOUT_BREAKOUT, "batch_size": batch_size},
            )
            expired_armed = cur.rowcount
            await cur.execute(
                _REAP_EXPIRED_WHILE_CLAIMED,
                {"reason": REASON_EXPIRED_WHILE_CLAIMED, "batch_size": batch_size},
            )
            expired_while_claimed = cur.rowcount
            await cur.execute(
                _REAP_INFRASTRUCTURE_FAILED_CLAIMS,
                {
                    "reason": REASON_INFRASTRUCTURE_FAILURE,
                    "max_attempts": max_claim_attempts,
                    "batch_size": batch_size,
                },
            )
            infra_failed = cur.rowcount
        return ReapSummary(
            expired_armed=expired_armed,
            expired_while_claimed=expired_while_claimed,
            infrastructure_failed_claims=infra_failed,
        )
    except Exception as exc:
        log.error("episodes.reap_overdue_failed", err=str(exc))
        return ReapSummary(expired_armed=0, expired_while_claimed=0, infrastructure_failed_claims=0)


_LIST_ACTIONABLE = """
SELECT * FROM app.early_momentum_episodes
WHERE (status = 'armed' AND expires_at > now())
   OR (
       status = 'claimed'
       AND expires_at > now()
       AND claim_expires_at < now()
       AND claim_attempts < %(max_attempts)s
   )
ORDER BY armed_at
LIMIT %(batch_size)s
"""


async def list_actionable(
    db_url: str, *, batch_size: int = 200, max_claim_attempts: int = 5
) -> list[Episode]:
    """The DB-driven discovery path: every episode a tick should be watching
    or may reclaim, read straight from Postgres -- independent of whatever
    Redis currently has cached. Callers use this to repair a missing/evicted
    WATCH key, not just to react to what Redis already shows them."""
    try:
        async with (
            await psycopg.AsyncConnection.connect(db_url, row_factory=dict_row) as aconn,
            aconn.cursor() as cur,
        ):
            await cur.execute(
                _LIST_ACTIONABLE,
                {"batch_size": batch_size, "max_attempts": max_claim_attempts},
            )
            rows = await cur.fetchall()
        return [_row_to_episode(row) for row in rows]
    except Exception as exc:
        log.error("episodes.list_actionable_failed", err=str(exc))
        return []


_RESOLVE_ROUTES_BATCH = """
SELECT
    a.native_market_id AS source_native_id,
    a.identity_key AS source_identity_key,
    b.native_market_id AS execution_native_id,
    b.identity_key AS execution_identity_key,
    a.cluster_key AS cluster_key
FROM app.momentum_universe_cluster_members a
JOIN app.momentum_universe_cluster_members b ON a.cluster_key = b.cluster_key
WHERE a.exchange = %(source_exchange)s
  AND a.native_market_id = ANY(%(source_native_ids)s)
  AND b.exchange = %(execution_exchange)s
  AND a.match_status = 'confirmed'
  AND b.match_status = 'confirmed'
"""


@dataclass(frozen=True)
class BatchRoute:
    source_native_id: str
    source_identity_key: str
    execution_native_id: str
    execution_identity_key: str
    cluster_key: str


async def resolve_routes_batch(
    db_url: str,
    *,
    source_exchange: str,
    source_native_ids: list[str],
    execution_exchange: str,
) -> dict[str, BatchRoute | None]:
    """Resolve routes for many candidates in one query/connection instead of
    symbols.resolve_route's one-fresh-connection-per-symbol pattern repeated
    N times per tick. Missing or ambiguous (>1 confirmed match) routes both
    fail closed to None for that source_native_id -- same contract as
    resolve_route, just batched."""
    if not source_native_ids:
        return {}
    result: dict[str, list[BatchRoute]] = {sid: [] for sid in source_native_ids}
    try:
        async with (
            await psycopg.AsyncConnection.connect(db_url, row_factory=dict_row) as aconn,
            aconn.cursor() as cur,
        ):
            await cur.execute(
                _RESOLVE_ROUTES_BATCH,
                {
                    "source_exchange": source_exchange,
                    "source_native_ids": source_native_ids,
                    "execution_exchange": execution_exchange,
                },
            )
            rows = await cur.fetchall()
    except Exception as exc:
        log.error(
            "episodes.resolve_routes_batch_failed",
            source_exchange=source_exchange,
            execution_exchange=execution_exchange,
            err=str(exc),
        )
        return dict.fromkeys(source_native_ids)
    for row in rows:
        result.setdefault(row["source_native_id"], []).append(
            BatchRoute(
                source_native_id=row["source_native_id"],
                source_identity_key=row["source_identity_key"],
                execution_native_id=row["execution_native_id"],
                execution_identity_key=row["execution_identity_key"],
                cluster_key=row["cluster_key"],
            )
        )
    return {sid: (matches[0] if len(matches) == 1 else None) for sid, matches in result.items()}


_ROUTE_STILL_CONFIRMED = """
SELECT 1 FROM app.momentum_universe_cluster_members
WHERE cluster_key = %(cluster_key)s
  AND exchange = %(exchange)s
  AND native_market_id = %(native_market_id)s
  AND match_status = 'confirmed'
"""


async def route_still_confirmed(
    db_url: str, *, cluster_key: str, exchange: str, native_market_id: str
) -> bool:
    """Re-check the frozen route immediately before spending the entry quote.
    A route that went stale between ARM and claim must terminate as
    route_invalidated -- never be silently re-resolved to a different one."""
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as aconn, aconn.cursor() as cur:
            await cur.execute(
                _ROUTE_STILL_CONFIRMED,
                {
                    "cluster_key": cluster_key,
                    "exchange": exchange,
                    "native_market_id": native_market_id,
                },
            )
            return await cur.fetchone() is not None
    except Exception as exc:
        log.error("episodes.route_still_confirmed_failed", cluster_key=cluster_key, err=str(exc))
        return False


_SNAPSHOT_AGE = """
SELECT EXTRACT(EPOCH FROM (now() - MAX(captured_at)))
FROM app.momentum_universe_snapshots
WHERE exchange = %(exchange)s
"""


async def identity_snapshot_age_seconds(db_url: str, *, exchange: str) -> float | None:
    """None means no snapshot has ever been captured for this exchange --
    treated the same as "too stale" by the caller, never as "fresh"."""
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as aconn, aconn.cursor() as cur:
            await cur.execute(_SNAPSHOT_AGE, {"exchange": exchange})
            row = await cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None
    except Exception as exc:
        log.error("episodes.identity_snapshot_age_failed", exchange=exchange, err=str(exc))
        return None


_REARM_RECENT_EPISODE = """
SELECT 1 FROM app.early_momentum_episodes
WHERE exchange = %(exchange)s
  AND native_market_id = %(native_market_id)s
  AND armed_at > now() - make_interval(secs := %(cooldown_seconds)s)
LIMIT 1
"""


async def within_rearm_cooldown(
    db_url: str, *, exchange: str, native_market_id: str, cooldown_seconds: int
) -> bool:
    """Honest stand-in for true qualified->not-qualified->qualified edge
    detection (which the scanner can't prove today -- it only ever emits
    positive candidate rows, never a disqualification signal). A fixed
    cooldown since this instrument's last armed_at, regardless of that
    episode's outcome, is what's actually implementable without changing the
    scanner itself."""
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as aconn, aconn.cursor() as cur:
            await cur.execute(
                _REARM_RECENT_EPISODE,
                {
                    "exchange": exchange,
                    "native_market_id": native_market_id,
                    "cooldown_seconds": cooldown_seconds,
                },
            )
            return await cur.fetchone() is not None
    except Exception as exc:
        log.error("episodes.within_rearm_cooldown_failed", exchange=exchange, err=str(exc))
        # Fail closed: an unreadable cooldown check must not let a re-arm
        # through it can't actually verify.
        return True


_HEALTH_METRICS = """
SELECT
    (SELECT count(*) FROM app.early_momentum_episodes
      WHERE status = 'armed' AND expires_at < now()) AS overdue_armed,
    (SELECT count(*) FROM app.early_momentum_episodes
      WHERE status = 'claimed' AND claim_expires_at < now()) AS expired_claims,
    (SELECT count(*) FROM app.early_momentum_episodes
      WHERE status = 'rejected' AND terminal_reason = 'identity_catalog_stale'
        AND created_at > now() - interval '1 hour') AS identity_stale_rejections_last_hour,
    (SELECT EXTRACT(EPOCH FROM (now() - MIN(expires_at))) FROM app.early_momentum_episodes
      WHERE status = 'armed' AND expires_at < now()) AS oldest_overdue_armed_age_seconds,
    (SELECT EXTRACT(EPOCH FROM (now() - MIN(claim_expires_at))) FROM app.early_momentum_episodes
      WHERE status = 'claimed' AND claim_expires_at < now()) AS oldest_expired_claim_age_seconds,
    (SELECT EXTRACT(EPOCH FROM (now() - MIN(t))) FROM (
        SELECT expires_at AS t FROM app.early_momentum_episodes
          WHERE status = 'armed' AND expires_at < now()
        UNION ALL
        SELECT claim_expires_at AS t FROM app.early_momentum_episodes
          WHERE status = 'claimed' AND claim_expires_at < now()
    ) overdue) AS oldest_overdue_age_seconds
"""


async def health_metrics(db_url: str) -> dict[str, Any]:
    """overdue_armed/expired_claims stay the raw counts they always were --
    always shown, never hidden by the reaper grace period (that judgment
    belongs to early_momentum_health.compute_status, not here). The two
    per-status ages are None whenever their own count is zero (nothing to
    measure) -- compute_status only ever consults an age when its count is
    positive, so that's never ambiguous with "couldn't measure"; a query
    failure fails every field closed to None, same as before."""
    try:
        async with (
            await psycopg.AsyncConnection.connect(db_url, row_factory=dict_row) as aconn,
            aconn.cursor() as cur,
        ):
            await cur.execute(_HEALTH_METRICS)
            row = await cur.fetchone()
        return {
            "overdue_armed": row["overdue_armed"] if row else None,
            "expired_claims": row["expired_claims"] if row else None,
            "identity_stale_rejections_last_hour": (
                row["identity_stale_rejections_last_hour"] if row else None
            ),
            "oldest_overdue_armed_age_seconds": (
                float(row["oldest_overdue_armed_age_seconds"])
                if row and row["oldest_overdue_armed_age_seconds"] is not None
                else None
            ),
            "oldest_expired_claim_age_seconds": (
                float(row["oldest_expired_claim_age_seconds"])
                if row and row["oldest_expired_claim_age_seconds"] is not None
                else None
            ),
            # Kept for backward compatibility -- combined across both
            # armed and claimed, defaults to 0.0 (not None) when nothing is
            # overdue, exactly as before this PR.
            "oldest_overdue_age_seconds": (
                float(row["oldest_overdue_age_seconds"])
                if row and row["oldest_overdue_age_seconds"] is not None
                else 0.0
            ),
        }
    except Exception as exc:
        log.error("episodes.health_metrics_failed", err=str(exc))
        return {
            "overdue_armed": None,
            "expired_claims": None,
            "identity_stale_rejections_last_hour": None,
            "oldest_overdue_armed_age_seconds": None,
            "oldest_expired_claim_age_seconds": None,
            "oldest_overdue_age_seconds": None,
        }


async def identity_health(
    db_url: str, *, exchanges: list[str], max_age_hours: float
) -> dict[str, dict[str, Any]]:
    """Per-exchange identity-snapshot freshness, exactly what the ARM-time
    staleness gate itself checks -- so a P0 like "the gate silently blocks
    every candidate because the catalog is older than the threshold" is
    visible in health instead of only showing up as "zero trades, no
    obvious reason" (colleague review)."""
    result: dict[str, dict[str, Any]] = {}
    for exchange in exchanges:
        age = await identity_snapshot_age_seconds(db_url, exchange=exchange)
        result[exchange] = {
            "age_seconds": age,
            "stale": age is None or age > max_age_hours * 3600,
        }
    return result


_SOURCE_FRESHNESS = """
SELECT max(bucket_start) AS latest_bucket,
       EXTRACT(EPOCH FROM (now() - max(bucket_start))) AS lag_seconds
FROM timeseries.bybit_momentum_bars_1m
WHERE exchange = %(exchange)s
  AND market_type = %(market_type)s
  AND capture_version = ANY(%(capture_versions)s)
  AND bucket_start >= now() - interval '1 day'
"""


async def source_freshness(
    db_url: str,
    *,
    exchanges: list[str],
    market_type: str,
    capture_versions: frozenset[str],
) -> dict[str, dict[str, Any]]:
    """Per-exchange latest-bucket age straight from the source timeseries
    table -- a fully stalled collector for one exchange must be visible
    here even while the other exchange keeps ticking fine (same reasoning
    as identity_health's per-exchange split).

    Scoped to the exact `market_type`/`capture_version`s the caller's
    quality policy actually trusts -- an unfiltered scan would let a live
    inverse-market or superseded-capture-version stream mask a genuine
    stall in the linear/v1 data v4 actually reads (colleague review)."""
    result: dict[str, dict[str, Any]] = {}
    for exchange in exchanges:
        try:
            async with (
                await psycopg.AsyncConnection.connect(db_url, row_factory=dict_row) as aconn,
                aconn.cursor() as cur,
            ):
                await cur.execute(
                    _SOURCE_FRESHNESS,
                    {
                        "exchange": exchange,
                        "market_type": market_type,
                        "capture_versions": list(capture_versions),
                    },
                )
                row = await cur.fetchone()
            result[exchange] = {
                "latest_bucket": row["latest_bucket"] if row else None,
                "lag_seconds": (
                    float(row["lag_seconds"]) if row and row["lag_seconds"] is not None else None
                ),
            }
        except Exception as exc:
            log.error("episodes.source_freshness_failed", exchange=exchange, err=str(exc))
            result[exchange] = {"latest_bucket": None, "lag_seconds": None}
    return result


_LAST_SUCCESSFUL_OPEN_AT = """
SELECT max(t.entry_at) AS last_successful_open_at
FROM app.trades t
JOIN app.early_momentum_episodes e ON e.episode_id = t.episode_id
WHERE e.strategy_id = %(strategy_id)s
"""


async def last_successful_open_at(db_url: str, *, strategy_id: int) -> datetime | None:
    """Scoped by strategy_id (never a bare `episode_id IS NOT NULL`) so an
    older cohort's trade can never mask a complete absence of activity for
    the strategy version actually being asked about (colleague review)."""
    try:
        async with (
            await psycopg.AsyncConnection.connect(db_url, row_factory=dict_row) as aconn,
            aconn.cursor() as cur,
        ):
            await cur.execute(_LAST_SUCCESSFUL_OPEN_AT, {"strategy_id": strategy_id})
            row = await cur.fetchone()
        return row["last_successful_open_at"] if row else None
    except Exception as exc:
        log.error("episodes.last_successful_open_at_failed", strategy_id=strategy_id, err=str(exc))
        return None
