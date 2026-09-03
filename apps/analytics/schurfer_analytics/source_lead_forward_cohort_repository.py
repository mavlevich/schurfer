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

QUALIFIED_EPISODE_QUERY_VERSION = "source_lead_forward_cohort_qualified_episode_v1"

_QUALIFIED_EPISODES_SQL = text("""
    SELECT
        c.id AS capture_id,
        c.base,
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
      AND t.status = 'sampled'
    ORDER BY c.source_first_observed_at, c.id
    LIMIT :limit
""")


@dataclass(frozen=True)
class RawQualifiedEpisode:
    """Unresolved -- resolve_episode still needs an exit_bar fetched
    separately (a live OHLCV lookup, not something this repository, whose
    job is only the already-persisted qualification data, provides)."""

    capture_id: int
    base: str
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
                    "limit": limit,
                },
            )
            rows = result.all()
        return tuple(
            RawQualifiedEpisode(
                capture_id=int(row.capture_id),
                base=str(row.base),
                target_exchange=str(row.target_exchange),
                observed_at=row.observed_at,
                requested_notional_usd=float(row.requested_notional_usd),
                liquidity=_as_json_dict(row.liquidity),
                instrument=_as_json_dict(row.instrument),
            )
            for row in rows
        )
