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

instruments_as_of/universe_seen_in_window (research/token-universe-
coverage-v1) read the SAME two tables across their full snapshot history
rather than only the latest row -- see token_universe_coverage.py's own
module doc comment for why this was added as a read against already-
persisted history rather than new capture infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

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
from .token_universe_coverage import AsOfCoverage, SeenInstrument, WindowCoverage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

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


def _require_aware(value: datetime, name: str) -> None:
    """Fail-fast guard for instruments_as_of/universe_seen_in_window's own
    caller-supplied timestamps -- colleague review, round 3: comparing a
    naive datetime against this table's own TIMESTAMPTZ columns would
    silently assume a timezone (psycopg/SQLAlchemy's own behavior) rather
    than fail loudly, and this method's whole contract (staleness relative
    to a specific UTC instant) depends on the caller meaning what they
    wrote."""
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware, got a naive datetime: {value!r}")


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
            snapshot = await self._latest_snapshot(connection, exchange, at_or_before=None)
            if snapshot is None:
                return ()
            universe_version, catalog_version, _captured_at = snapshot
            return await self._ready_instruments(
                connection, exchange, universe_version, catalog_version
            )

    async def instruments_as_of(self, exchange: str, as_of: datetime) -> AsOfCoverage:
        """research/token-universe-coverage-v1: what was ready on exchange
        at instant as_of, using the nearest snapshot captured at or before
        as_of -- momentum_universe_snapshots only gets a new row when a
        capture process restarts (irregular, not a fixed cadence), so this
        is necessarily an approximation, never a guarantee that as_of's own
        true listing state is reflected exactly. Returns
        snapshot_captured_at=None (and an empty instrument set) when no
        snapshot exists at or before as_of at all -- e.g. as_of predates
        this exchange's capture process ever starting -- so a caller can
        tell "no evidence either way" apart from "evidence says nothing was
        ready", which token_universe_coverage.AsOfCoverage.is_usable is the
        sanctioned way to check before trusting the result for a formal
        read.

        Raises ValueError if as_of is not timezone-aware -- comparing a
        naive datetime against this table's own TIMESTAMPTZ columns would
        silently assume a timezone rather than fail loudly."""
        _require_aware(as_of, "as_of")
        async with self._engine.connect() as connection:
            snapshot = await self._latest_snapshot(connection, exchange, at_or_before=as_of)
            if snapshot is None:
                return AsOfCoverage(
                    exchange=exchange,
                    as_of=as_of,
                    snapshot_captured_at=None,
                    native_market_ids=frozenset(),
                    identity_keys=frozenset(),
                )
            universe_version, catalog_version, captured_at = snapshot
            instruments = await self._ready_instruments(
                connection, exchange, universe_version, catalog_version
            )
            return AsOfCoverage(
                exchange=exchange,
                as_of=as_of,
                snapshot_captured_at=captured_at,
                native_market_ids=frozenset(i.native_market_id for i in instruments),
                identity_keys=frozenset(i.identity_key for i in instruments),
            )

    async def universe_seen_in_window(
        self,
        exchange: str,
        window_start: datetime,
        window_end: datetime,
        *,
        max_carry_in_staleness: timedelta,
    ) -> WindowCoverage:
        """research/token-universe-coverage-v1: every distinct instrument
        LIFE (keyed by identity_key -- see SeenInstrument's own doc comment
        on why a bare native_market_id would wrongly merge a delisted-then-
        relisted market id with its predecessor) that was
        identity_status='ready' at some point covering [window_start,
        window_end) -- the control-group universe research/serial-pump-
        regimes-v1 needs (every asset that COULD have been a pump candidate
        during the window, not only ones app.pump_events happened to
        record), independent of whether it is still listed today.

        Colleague review, round 2: snapshots captured_at INSIDE the window
        alone are not enough -- an exchange's own last snapshot before
        window_start still describes what was ready for the whole window
        up to the next restart (which could land anywhere inside or after
        the window, or not at all). Without that carry-in snapshot, a
        window containing zero capture-process restarts returns an empty
        universe even though hundreds of instruments were genuinely
        eligible throughout. This method includes the nearest snapshot at
        or before window_start alongside every snapshot actually captured
        inside the window -- but ONLY when that carry-in is itself
        admissibly fresh (within max_carry_in_staleness of window_start);
        a stale or missing carry-in is reported via
        WindowCoverage.carry_in_snapshot_captured_at/
        carry_in_within_tolerance=False for diagnosis, but excluded from
        `seen` rather than silently mixed into the universe as if it were
        still representative. A caller MUST check carry_in_within_tolerance
        (or the equivalent has_reliable_coverage property) before trusting
        `seen` as complete window-start coverage. There is no codebase-wide
        default for max_carry_in_staleness, matching AsOfCoverage.
        is_usable's own reasoning: how stale is still "the same listing
        state" is a research-contract decision for whichever report calls
        this.

        Every returned entry's own currently_ready is None (not yet
        classified) -- this method does not itself look at the
        current/latest snapshot (a window ending before "now" must not
        silently leak "now"'s own listing state into its answer); pass
        WindowCoverage.seen through token_universe_coverage.mark_currently_
        ready with this exchange's own instruments_as_of("now")'s own
        identity_keys to classify it and derive delisted() from it.

        Colleague review, round 3: raises ValueError, fail-fast, on any of
        window_start/window_end not timezone-aware, window_start >=
        window_end (an empty or reversed interval), or a negative
        max_carry_in_staleness -- none of these were checked before, so an
        empty/reversed window could still return a non-empty `seen` purely
        from the carry-in (window_end is never consulted when deciding the
        carry-in), silently producing a denominator for an interval that
        does not exist."""
        _require_aware(window_start, "window_start")
        _require_aware(window_end, "window_end")
        if window_start >= window_end:
            raise ValueError(
                f"window_start ({window_start!r}) must be strictly before "
                f"window_end ({window_end!r})"
            )
        if max_carry_in_staleness < timedelta(0):
            raise ValueError(
                f"max_carry_in_staleness must be non-negative, got {max_carry_in_staleness!r}"
            )
        async with self._engine.connect() as connection:
            carry_in = await self._latest_snapshot(connection, exchange, at_or_before=window_start)
            carry_in_captured_at = carry_in[2] if carry_in is not None else None
            carry_in_within_tolerance = (
                carry_in_captured_at is not None
                and (window_start - carry_in_captured_at) <= max_carry_in_staleness
            )

            # Only an ADMISSIBLY FRESH carry-in (within max_carry_in_staleness
            # of window_start) is actually used to populate `seen` -- a carry-in
            # far outside tolerance is reported via carry_in_snapshot_captured_at
            # / carry_in_within_tolerance=False for diagnostic purposes, but its
            # own (possibly long-stale, possibly no-longer-representative)
            # instrument set must not silently leak into the window's universe.
            snapshot_keys: list[tuple[str, str]] = []
            if carry_in is not None and carry_in_within_tolerance:
                snapshot_keys.append((carry_in[0], carry_in[1]))
            in_window_result = await connection.execute(
                select(_snapshots.c.universe_version, _snapshots.c.catalog_version)
                .where(
                    _snapshots.c.exchange == exchange,
                    _snapshots.c.captured_at >= window_start,
                    _snapshots.c.captured_at < window_end,
                )
                .distinct()
            )
            for row in in_window_result.all():
                key = (str(row.universe_version), str(row.catalog_version))
                if key not in snapshot_keys:
                    snapshot_keys.append(key)

            if not snapshot_keys:
                # No admissible snapshot at all (carry-in excluded for
                # staleness or missing entirely, and nothing captured inside
                # the window either) -- still report the actual carry-in
                # diagnosis computed above, not a hardcoded None/False that
                # would silently discard it even when a stale-but-real
                # carry-in candidate exists.
                return WindowCoverage(
                    exchange=exchange,
                    window_start=window_start,
                    window_end=window_end,
                    carry_in_snapshot_captured_at=carry_in_captured_at,
                    carry_in_within_tolerance=carry_in_within_tolerance,
                    seen=(),
                )

            snapshot_condition = (_snapshots.c.universe_version == snapshot_keys[0][0]) & (
                _snapshots.c.catalog_version == snapshot_keys[0][1]
            )
            for universe_version, catalog_version in snapshot_keys[1:]:
                snapshot_condition = snapshot_condition | (
                    (_snapshots.c.universe_version == universe_version)
                    & (_snapshots.c.catalog_version == catalog_version)
                )

            rows_result = await connection.execute(
                select(
                    _instruments.c.identity_key,
                    _instruments.c.native_market_id,
                    _instruments.c.base,
                    _instruments.c.canonical_market_type,
                    func.min(_snapshots.c.captured_at).label("first_seen_ready_at"),
                    func.max(_snapshots.c.captured_at).label("last_seen_ready_at"),
                )
                .select_from(
                    _instruments.join(
                        _snapshots,
                        (_snapshots.c.exchange == _instruments.c.exchange)
                        & (_snapshots.c.universe_version == _instruments.c.universe_version)
                        & (_snapshots.c.catalog_version == _instruments.c.catalog_version),
                    )
                )
                .where(
                    _instruments.c.exchange == exchange,
                    _instruments.c.identity_status == "ready",
                    snapshot_condition,
                )
                .group_by(
                    _instruments.c.identity_key,
                    _instruments.c.native_market_id,
                    _instruments.c.base,
                    _instruments.c.canonical_market_type,
                )
                .order_by(_instruments.c.native_market_id)
            )
            seen = tuple(
                SeenInstrument(
                    exchange=exchange,
                    identity_key=str(row.identity_key),
                    native_market_id=str(row.native_market_id),
                    base=str(row.base),
                    canonical_market_type=str(row.canonical_market_type),
                    first_seen_ready_at=_utc(row.first_seen_ready_at),
                    last_seen_ready_at=_utc(row.last_seen_ready_at),
                )
                for row in rows_result.all()
            )
            return WindowCoverage(
                exchange=exchange,
                window_start=window_start,
                window_end=window_end,
                carry_in_snapshot_captured_at=carry_in_captured_at,
                carry_in_within_tolerance=carry_in_within_tolerance,
                seen=seen,
            )

    async def _latest_snapshot(
        self, connection: AsyncConnection, exchange: str, *, at_or_before: datetime | None
    ) -> tuple[str, str, datetime] | None:
        """Shared by latest_ready_instruments (at_or_before=None: no upper
        bound) and instruments_as_of (at_or_before=as_of): the most recent
        snapshot's own (universe_version, catalog_version, captured_at),
        tie-broken the same captured_at-then-created_at way
        latest_ready_instruments's own doc comment explains. None if no
        snapshot matches at all."""
        conditions = [_snapshots.c.exchange == exchange]
        if at_or_before is not None:
            conditions.append(_snapshots.c.captured_at <= at_or_before)
        result = await connection.execute(
            select(
                _snapshots.c.universe_version,
                _snapshots.c.catalog_version,
                _snapshots.c.captured_at,
            )
            .where(*conditions)
            .order_by(desc(_snapshots.c.captured_at), desc(_snapshots.c.created_at))
            .limit(1)
        )
        row = result.first()
        if row is None:
            return None
        return str(row.universe_version), str(row.catalog_version), _utc(row.captured_at)

    async def _ready_instruments(
        self,
        connection: AsyncConnection,
        exchange: str,
        universe_version: str,
        catalog_version: str,
    ) -> tuple[CandidateInstrument, ...]:
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
