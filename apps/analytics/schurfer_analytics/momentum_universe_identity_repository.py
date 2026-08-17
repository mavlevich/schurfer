"""Postgres adapter for cross-venue instrument identity matching.

Reads the latest ready-instrument snapshot per exchange from
app.momentum_universe_snapshots/momentum_universe_instruments (written by
apps/collector's own momentumcapture.PersistCaptureStartupSnapshot -- see
migration 0028's own docstring), and persists momentum_universe_identity_
classifier.classify()'s own output into app.momentum_universe_asset_
clusters/momentum_universe_cluster_members (migration 0029).

Full-resync writer, not an append-only ledger: each matching run reflects
the CURRENT latest snapshots, so persist_clusters replaces the whole
cluster/member table content atomically (one transaction) rather than
diffing against the previous run. A base that stops cross-matching (e.g.
one venue delists it) simply has no cluster row after the next run, not a
stale leftover a caller has to know to ignore.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    delete,
    desc,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .momentum_universe_identity_classifier import AssetCluster, CandidateInstrument
from .outcome_repository import async_database_url

_metadata = MetaData()

_snapshots = Table(
    "momentum_universe_snapshots",
    _metadata,
    Column("exchange", String),
    Column("universe_version", String),
    Column("catalog_version", String),
    Column("captured_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True)),
    schema="app",
)

_instruments = Table(
    "momentum_universe_instruments",
    _metadata,
    Column("exchange", String),
    Column("universe_version", String),
    Column("catalog_version", String),
    Column("native_market_id", String),
    Column("base", String),
    Column("canonical_market_type", String),
    Column("onboarded_at", DateTime(timezone=True)),
    Column("identity_status", String),
    Column("identity_key", Text),
    schema="app",
)

_clusters = Table(
    "momentum_universe_asset_clusters",
    _metadata,
    Column("cluster_key", Text),
    Column("base", String),
    Column("canonical_market_type", String),
    Column("match_ruleset_version", String),
    Column("member_count", Integer),
    Column("resolved_at", DateTime(timezone=True)),
    schema="app",
)

_members = Table(
    "momentum_universe_cluster_members",
    _metadata,
    Column("cluster_key", Text),
    Column("exchange", String),
    Column("native_market_id", String),
    Column("identity_key", Text),
    Column("onboarded_at", DateTime(timezone=True)),
    Column("match_status", String),
    Column("match_reason", Text),
    schema="app",
)


# hashtext() of a fixed string -- same convention as momentum_flow_watch_
# repository.acquire_worker_lock's own pg_try_advisory_lock(hashtext(...))
# key. A distinct string from every other lock key already in use in this
# codebase (watch_version, paper_version, ...), since this lock guards a
# concern none of those touch (the cluster/member tables).
_ADVISORY_LOCK_KEY = "momentum_universe_identity_match"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class MatchRunSummary:
    exchanges_read: tuple[str, ...]
    instruments_read: int
    clusters_written: int
    members_written: int


class MomentumUniverseIdentityRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, database_url: str) -> MomentumUniverseIdentityRepository:
        # pool_size=4, not 2: the matcher's own run() fetches every
        # exchange's latest_ready_instruments concurrently (asyncio.
        # gather), so a pool too small to hand out one connection per
        # exchange would just serialize the "concurrent" reads at the
        # pool layer instead of failing -- 4 gives headroom for the
        # currently-captured 2 venues plus near-term growth without
        # needing a matching bump the moment a third venue is onboarded.
        return cls(
            create_async_engine(
                async_database_url(database_url),
                pool_pre_ping=True,
                pool_size=4,
                max_overflow=0,
            )
        )

    async def latest_ready_instruments(self, exchange: str) -> tuple[CandidateInstrument, ...]:
        """Reads every identity_status='ready' instrument from the most
        recently captured_at snapshot for this exchange. Returns an empty
        tuple if this exchange has never persisted a snapshot -- a real,
        valid state (a venue whose capture binary has never started, or
        one not onboarded to momentum_flow yet), not an error.

        Ordered by captured_at DESC, created_at DESC: captured_at alone is
        application-computed (see PersistCaptureStartupSnapshot) and can
        tie across two snapshots (e.g. a crash-looping capture process
        restarting fast enough to land in the same instant, or plain clock
        resolution) -- created_at is the DB's own now() at INSERT time,
        which does not tie the same way, so the pair together makes "most
        recent" deterministic instead of leaving Postgres's LIMIT 1 to
        break the tie arbitrarily."""
        async with self._engine.connect() as connection:
            latest_result = await connection.execute(
                select(_snapshots.c.universe_version, _snapshots.c.catalog_version)
                .where(_snapshots.c.exchange == exchange)
                .order_by(desc(_snapshots.c.captured_at), desc(_snapshots.c.created_at))
                .limit(1)
            )
            latest = latest_result.first()
            if latest is None:
                return ()
            universe_version, catalog_version = latest
            rows_result = await connection.execute(
                select(
                    _instruments.c.native_market_id,
                    _instruments.c.base,
                    _instruments.c.canonical_market_type,
                    _instruments.c.identity_key,
                    _instruments.c.onboarded_at,
                )
                .where(
                    _instruments.c.exchange == exchange,
                    _instruments.c.universe_version == universe_version,
                    _instruments.c.catalog_version == catalog_version,
                    _instruments.c.identity_status == "ready",
                )
                .order_by(_instruments.c.native_market_id)
            )
            return tuple(
                CandidateInstrument(
                    exchange=exchange,
                    native_market_id=str(row.native_market_id),
                    base=str(row.base),
                    canonical_market_type=str(row.canonical_market_type),
                    identity_key=str(row.identity_key),
                    onboarded_at=_utc(row.onboarded_at),
                )
                for row in rows_result.all()
            )

    async def persist_clusters(
        self,
        clusters: tuple[AssetCluster, ...],
        *,
        match_ruleset_version: str,
        resolved_at: datetime,
    ) -> int:
        """Replaces the entire cluster/member table content with `clusters`
        in one transaction. Returns the number of member rows written.
        Safe to call with an empty tuple (clears every existing row) --
        a caller that means "no clusters this run" (e.g. only one exchange
        had any ready instruments at all) gets exactly that reflected.

        Takes a transaction-scoped Postgres advisory lock first (released
        automatically on commit/rollback, no separate unlock call needed):
        this is a full DELETE-then-INSERT resync, not an upsert, so two
        overlapping calls (a slow ad-hoc run plus an impatient operator
        starting a second one, or a future periodic timer overlapping a
        manual run -- see the matcher's own doc comment on why there is no
        timer yet) would otherwise interleave and either raise a
        cluster_key PK violation or silently let the loser's stale
        classification clobber the winner's. The lock serializes them
        instead: the second caller blocks until the first's transaction
        finishes, then reads/writes a table the first call has already
        fully settled."""
        cluster_rows = [
            {
                "cluster_key": cluster.cluster_key,
                "base": cluster.base,
                "canonical_market_type": cluster.canonical_market_type,
                "match_ruleset_version": match_ruleset_version,
                "member_count": len(cluster.members),
                "resolved_at": _utc(resolved_at),
            }
            for cluster in clusters
        ]
        member_rows = [
            {
                "cluster_key": cluster.cluster_key,
                "exchange": member.exchange,
                "native_market_id": member.native_market_id,
                "identity_key": member.identity_key,
                "onboarded_at": _utc(member.onboarded_at),
                "match_status": member.match_status,
                "match_reason": member.match_reason,
            }
            for cluster in clusters
            for member in cluster.members
        ]
        async with self._engine.begin() as connection:
            await connection.execute(
                select(func.pg_advisory_xact_lock(func.hashtext(_ADVISORY_LOCK_KEY)))
            )
            await connection.execute(delete(_members))
            await connection.execute(delete(_clusters))
            if cluster_rows:
                await connection.execute(insert(_clusters).values(cluster_rows))
            if member_rows:
                await connection.execute(insert(_members).values(member_rows))
        return len(member_rows)

    async def close(self) -> None:
        await self._engine.dispose()
