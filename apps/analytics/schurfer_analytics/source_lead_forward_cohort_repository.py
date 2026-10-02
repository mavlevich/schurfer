"""Postgres adapter for source_lead_forward_cohort_v1
(research/source-lead-forward-cohort-plumbing-v1).

`source_lead_forward_cohort.py`'s own `resolve_episode`/`formal_verdict` are
pure functions frozen well before any real qualified capture exists (see
that module's docstring). This is the DB-fetching half the module docstring
explicitly leaves for later: one qualified episode is exactly one
`app.source_lead_qualifications` row with `status='qualified'` under this
contract's `QUALIFICATION_VERSION`, joined to its parent
`app.source_lead_captures` row (for `base` and the cohort-start filter on
`source_first_observed_at`) and to the ONE `app.source_lead_target_observations`
row matching the qualification's own `selected_target_exchange` (for the
entry inputs `resolve_episode` needs: `observed_at`, `requested_notional_usd`,
and `liquidity["ask_vwap"]`, plus `instrument["unified_symbol"]` -- the exact,
already identity-verified CCXT symbol used to fetch that observation, never
reconstructed from a bare base ticker; see `EXCHANGE_FACTORIES`/
`fetch_symbol_candles` usage in the report module for where that symbol
feeds the exit-bar fetch).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from collections.abc import Sequence

# v2: an optional upper bound on the capture time (the administrative stop deadline).
# v3: an optional upper bound on the qualification stamp, so a first read's membership
# is the state at its single clock reading, as in the administrative snapshot.
QUALIFIED_EPISODE_QUERY_VERSION = "source_lead_forward_cohort_qualified_episode_v3"

_QUALIFIED_EPISODES_SQL = text("""
    SELECT
        c.id AS capture_id,
        c.base,
        q.canonical_asset_id,
        q.selected_target_exchange AS target_exchange,
        t.observed_at,
        t.requested_notional_usd,
        t.liquidity,
        t.instrument
    FROM app.source_lead_qualifications q
    JOIN app.source_lead_captures c ON c.id = q.capture_id
    JOIN app.source_lead_target_observations t
      ON t.capture_id = q.capture_id
     AND t.target_exchange = q.selected_target_exchange
    WHERE q.status = 'qualified'
      AND q.qualification_version = :qualification_version
      AND c.source_first_observed_at >= :since
      AND (CAST(:until AS timestamptz) IS NULL OR c.source_first_observed_at < :until)
      AND (CAST(:qualified_before AS timestamptz) IS NULL OR q.created_at < :qualified_before)
      AND t.status = 'sampled'
    ORDER BY c.source_first_observed_at, c.id
    LIMIT :limit
""")

ACCRUAL_SNAPSHOT_QUERY_VERSION = "source_lead_forward_cohort_accrual_snapshot_v1"

# Outcome-blind: identities and timestamps only. No book, liquidity, instrument or
# price column is selected. Qualification rows are insert-only (ON CONFLICT DO
# NOTHING) and stamp `created_at` with NOW(), the start of their transaction. So the
# set `q.created_at < :as_of` is final once no transaction that began before
# `as_of` is still open (`open_transactions_started_before`); from then on every
# snapshot of it is identical, however late it is taken.
_ACCRUAL_SNAPSHOT_SQL = text("""
    SELECT
        c.id AS capture_id,
        c.source_first_observed_at,
        q.canonical_asset_id,
        q.selected_target_exchange AS target_exchange,
        t.observed_at
    FROM app.source_lead_qualifications q
    JOIN app.source_lead_captures c ON c.id = q.capture_id
    JOIN app.source_lead_target_observations t
      ON t.capture_id = q.capture_id
     AND t.target_exchange = q.selected_target_exchange
    WHERE q.status = 'qualified'
      AND q.qualification_version = :qualification_version
      AND c.source_first_observed_at >= :since
      AND c.source_first_observed_at < :as_of
      AND q.created_at < :as_of
      AND t.status = 'sampled'
    ORDER BY c.source_first_observed_at, c.id
    LIMIT :limit
""")


# A transaction that began before `as_of` can still insert rows stamped before it. A
# session whose transaction start this role may not see (insufficient privilege)
# cannot be ruled out, so it is counted as unverifiable rather than ignored.
_OPEN_TRANSACTIONS_SQL = text("""
    SELECT
        count(*) FILTER (WHERE xact_start < :as_of) AS started_before,
        count(*) FILTER (WHERE state IS NULL) AS unverifiable
    FROM pg_stat_activity
    WHERE datname = current_database()
      AND backend_type = 'client backend'
      AND pid <> pg_backend_pid()
""")


@dataclass(frozen=True)
class OpenTransactions:
    started_before: int
    unverifiable: int

    @property
    def snapshot_final(self) -> bool:
        return self.started_before == 0 and self.unverifiable == 0


@dataclass(frozen=True)
class AccrualSnapshotRow:
    """One qualified v2 episode as it existed at a snapshot instant: identity and
    timestamps only, never a price."""

    capture_id: int
    source_first_observed_at: datetime
    canonical_asset_id: str
    target_exchange: str
    observed_at: datetime


@dataclass(frozen=True)
class RawQualifiedEpisode:
    """Unresolved -- resolve_episode still needs an exit_bar fetched
    separately (a live OHLCV lookup, not something this repository, whose
    job is only the already-persisted qualification data, provides).

    `canonical_asset_id` (not `base`, a bare ticker string) is what the
    report's own clustering/concentration/bootstrap must use -- colleague
    review, 2026-09-03: `base` alone can silently merge two different
    assets sharing a ticker, or split one multi-chain asset across two
    tickers, exactly the identity risk this codebase's own source-lead
    identity registry exists to close. Migration 0022's own CHECK
    constraint (`status = 'qualified' AND canonical_asset_id IS NOT NULL
    ...`) guarantees this is never null for a row this query's own
    `status = 'qualified'` filter selects."""

    capture_id: int
    base: str
    canonical_asset_id: str
    target_exchange: str
    observed_at: datetime
    requested_notional_usd: float
    liquidity: dict[str, Any]
    instrument: dict[str, Any]


def _as_json_dict(value: Any) -> dict[str, Any]:
    # asyncpg/psycopg return JSONB columns already decoded as Python dicts
    # under normal use, but a raw driver or an unusual pool configuration
    # can hand back the still-encoded string instead -- decode defensively
    # rather than trust the column type alone (colleague-review-established
    # pattern elsewhere in this codebase for JSONB reads).
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise TypeError(f"expected a JSON object, got {type(value).__name__}")


class SourceLeadForwardCohortRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, database_url: str) -> SourceLeadForwardCohortRepository:
        return cls(
            create_async_engine(
                async_database_url(database_url),
                pool_pre_ping=True,
                pool_size=2,
                max_overflow=0,
            )
        )

    async def database_now(self) -> datetime:
        async with self._engine.connect() as connection:
            value = (await connection.execute(text("SELECT now()"))).scalar_one()
        if not isinstance(value, datetime):
            raise TypeError("database now() did not return a datetime")
        return value

    async def fetch_qualified_episodes(
        self,
        *,
        qualification_version: str,
        since: datetime,
        limit: int,
        until: datetime | None = None,
        qualified_before: datetime | None = None,
    ) -> Sequence[RawQualifiedEpisode]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        async with self._engine.connect() as connection, connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            result = await connection.execute(
                _QUALIFIED_EPISODES_SQL,
                {
                    "qualification_version": qualification_version,
                    "since": since,
                    "until": until,
                    "qualified_before": qualified_before,
                    "limit": limit,
                },
            )
            rows = result.all()
        episodes = []
        for row in rows:
            canonical_asset_id = row.canonical_asset_id
            if not isinstance(canonical_asset_id, str) or not canonical_asset_id:
                # Migration 0022's own CHECK constraint should make this
                # unreachable for a status='qualified' row -- fail loud
                # rather than silently falling back to `base` (a bare
                # ticker) and reintroducing the identity risk this field
                # exists to close, in case that constraint is ever bypassed
                # (a raw migration, a manual UPDATE, a future schema change).
                raise ValueError(
                    f"source_lead_qualifications row for capture_id={row.capture_id} has no "
                    "canonical_asset_id despite status='qualified' -- migration 0022's own "
                    "CHECK constraint should make this unreachable; refusing to silently "
                    "cluster this episode by its bare ticker instead"
                )
            episodes.append(
                RawQualifiedEpisode(
                    capture_id=int(row.capture_id),
                    base=str(row.base),
                    canonical_asset_id=canonical_asset_id,
                    target_exchange=str(row.target_exchange),
                    observed_at=row.observed_at,
                    requested_notional_usd=float(row.requested_notional_usd),
                    liquidity=_as_json_dict(row.liquidity),
                    instrument=_as_json_dict(row.instrument),
                )
            )
        return tuple(episodes)

    async def open_transactions_started_before(self, as_of: datetime) -> OpenTransactions:
        """Other sessions of this database whose open transaction began before `as_of`,
        and sessions whose state this role cannot see."""
        async with self._engine.connect() as connection:
            row = (await connection.execute(_OPEN_TRANSACTIONS_SQL, {"as_of": as_of})).one()
        return OpenTransactions(int(row.started_before), int(row.unverifiable))

    async def fetch_accrual_snapshot(
        self,
        *,
        qualification_version: str,
        since: datetime,
        as_of: datetime,
        limit: int,
    ) -> Sequence[AccrualSnapshotRow]:
        """Qualified episodes captured in [since, as_of) whose qualification existed
        before `as_of`. Read-only and price-free."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        if as_of <= since:
            raise ValueError("as_of must be after since")
        async with self._engine.connect() as connection, connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            rows = (
                await connection.execute(
                    _ACCRUAL_SNAPSHOT_SQL,
                    {
                        "qualification_version": qualification_version,
                        "since": since,
                        "as_of": as_of,
                        "limit": limit,
                    },
                )
            ).all()
        snapshot = []
        for row in rows:
            if not isinstance(row.canonical_asset_id, str) or not row.canonical_asset_id:
                raise ValueError(f"qualified capture_id={row.capture_id} has no canonical_asset_id")
            snapshot.append(
                AccrualSnapshotRow(
                    capture_id=int(row.capture_id),
                    source_first_observed_at=row.source_first_observed_at,
                    canonical_asset_id=row.canonical_asset_id,
                    target_exchange=str(row.target_exchange),
                    observed_at=row.observed_at,
                )
            )
        return tuple(snapshot)

    async def close(self) -> None:
        await self._engine.dispose()
