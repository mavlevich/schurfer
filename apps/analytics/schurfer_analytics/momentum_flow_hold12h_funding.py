"""Captured actual-funding for the HYP-015 hold12h verdict.

The verdict's ``ActualFundingSource`` protocol (see momentum_flow_hold12h_verdict_report)
is SYNC -- ``resolve_pair`` calls ``coverage`` inline -- so the settlements are pre-loaded
into an in-memory ``StoredFundingSource`` before the pure pipeline runs. The capture side
(fetch + persist) lives in momentum_flow_hold12h_funding_resolver; this module owns the
pure coverage rule, the in-memory source, and the DB read.

Proven coverage is a RECORDED fact, never assumed: an interval is covered only when a
``complete`` coverage run of the frozen ``ACTUAL_FUNDING_VERSION`` spans it. Otherwise the
source returns ``None`` and the probe is accounting_incomplete. Settlements carry their
``source_version`` so a duplicate ``(instrument, settlement_at, source_version)`` is a hard
integrity error downstream (funding_usd_over_interval raises), never a silent double-charge.
"""

from __future__ import annotations

# ruff: noqa: S608 -- app_schema is a caller constant; every value is bound.
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .momentum_flow_hold12h_verdict_report import (
    FundingCoverage,
    InstrumentRoute,
    SettlementEvent,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

# The frozen funding construction this source produces. Bumped only with the verdict
# contract's actual_funding_version when the capture semantics change.
ACTUAL_FUNDING_VERSION = "hold12h_actual_funding_v1"


@dataclass(frozen=True)
class CoverageRun:
    """A recorded fetch of one instrument window and its terminal status."""

    requested_since: datetime
    requested_until: datetime
    status: str
    source_version: str


def _route_key(route: InstrumentRoute) -> tuple[str, str]:
    """The exact-route identity a settlement is keyed on: venue + native market id."""
    return (route.exchange, route.market_id)


def coverage_for_interval(
    settlements: Sequence[SettlementEvent],
    runs: Sequence[CoverageRun],
    *,
    entry_at: datetime,
    exit_at: datetime,
    source_version: str = ACTUAL_FUNDING_VERSION,
) -> FundingCoverage | None:
    """Pure coverage decision for one instrument over ``(entry_at, exit_at]``.

    ``None`` (=> accounting_incomplete) unless a ``complete`` run of ``source_version``
    SPANS the interval (``requested_since <= entry_at`` and ``requested_until >= exit_at``).
    When proven, the events are the settlements of that version -- the interval filter and
    the long sign are applied later by ``funding_usd_over_interval``; here we return the
    version's events with ``proven_full_coverage=True``. An empty event tuple then means a
    proven-zero-funding interval, never an assumed one.

    An ``integrity_conflict`` run (a re-fetch that returned a different rate for a stored
    settlement) that OVERLAPS the interval invalidates any ``complete`` run: the interval is
    ``accounting_incomplete`` until a human resolves the conflict, so a compromised rate
    never keeps feeding the verdict.
    """
    blocked = any(
        run.status == "integrity_conflict"
        and run.source_version == source_version
        and run.requested_since <= exit_at
        and run.requested_until >= entry_at
        for run in runs
    )
    if blocked:
        return None
    proven = any(
        run.status == "complete"
        and run.source_version == source_version
        and run.requested_since <= entry_at
        and run.requested_until >= exit_at
        for run in runs
    )
    if not proven:
        return None
    events = tuple(s for s in settlements if s.source_version == source_version)
    return FundingCoverage(events=events, proven_full_coverage=True)


class StoredFundingSource:
    """In-memory ``ActualFundingSource`` over pre-loaded settlements + coverage runs,
    keyed by exact route. Pure lookups only -- no I/O in ``coverage`` (the protocol is
    sync); build it with ``load_stored_funding`` before running the verdict."""

    def __init__(
        self,
        settlements: dict[tuple[str, str], tuple[SettlementEvent, ...]],
        runs: dict[tuple[str, str], tuple[CoverageRun, ...]],
        *,
        source_version: str = ACTUAL_FUNDING_VERSION,
    ) -> None:
        self._settlements = settlements
        self._runs = runs
        self._source_version = source_version

    def coverage(
        self, route: InstrumentRoute, entry_at: datetime, exit_at: datetime
    ) -> FundingCoverage | None:
        key = _route_key(route)
        return coverage_for_interval(
            self._settlements.get(key, ()),
            self._runs.get(key, ()),
            entry_at=entry_at,
            exit_at=exit_at,
            source_version=self._source_version,
        )


async def load_stored_funding(
    db_url: str,
    routes: Sequence[InstrumentRoute],
    *,
    source_version: str = ACTUAL_FUNDING_VERSION,
    app_schema: str = "app",
) -> StoredFundingSource:
    """Load every captured settlement + coverage run for the given routes into memory."""
    from sqlalchemy import bindparam, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from .outcome_repository import async_database_url

    keys = sorted({_route_key(r) for r in routes})
    if not keys:
        return StoredFundingSource({}, {}, source_version=source_version)
    exchanges = sorted({k[0] for k in keys})
    market_ids = sorted({k[1] for k in keys})

    settlements: dict[tuple[str, str], list[SettlementEvent]] = {}
    runs: dict[tuple[str, str], list[CoverageRun]] = {}
    engine = create_async_engine(async_database_url(db_url), pool_pre_ping=True, pool_size=1)
    try:
        async with engine.connect() as conn:
            settlement_rows = (
                (
                    await conn.execute(
                        text(
                            f"""
                            SELECT exchange, native_market_id, settlement_at, funding_rate,
                                   source_version
                            FROM {app_schema}.hold12h_funding_settlements
                            WHERE exchange IN :ex AND native_market_id IN :mid
                              AND source_version = :sv
                            """
                        ).bindparams(
                            bindparam("ex", expanding=True), bindparam("mid", expanding=True)
                        ),
                        {"ex": exchanges, "mid": market_ids, "sv": source_version},
                    )
                )
                .mappings()
                .all()
            )
            run_rows = (
                (
                    await conn.execute(
                        text(
                            f"""
                            SELECT exchange, native_market_id, requested_since, requested_until,
                                   status, source_version
                            FROM {app_schema}.hold12h_funding_coverage_runs
                            WHERE exchange IN :ex AND native_market_id IN :mid
                              AND source_version = :sv
                            """
                        ).bindparams(
                            bindparam("ex", expanding=True), bindparam("mid", expanding=True)
                        ),
                        {"ex": exchanges, "mid": market_ids, "sv": source_version},
                    )
                )
                .mappings()
                .all()
            )
    finally:
        await engine.dispose()

    for row in settlement_rows:
        key = (str(row["exchange"]), str(row["native_market_id"]))
        settlements.setdefault(key, []).append(
            SettlementEvent(
                settlement_at=row["settlement_at"],
                rate=float(row["funding_rate"]),
                source_version=str(row["source_version"]),
            )
        )
    for row in run_rows:
        key = (str(row["exchange"]), str(row["native_market_id"]))
        runs.setdefault(key, []).append(
            CoverageRun(
                requested_since=row["requested_since"],
                requested_until=row["requested_until"],
                status=str(row["status"]),
                source_version=str(row["source_version"]),
            )
        )
    return StoredFundingSource(
        {k: tuple(v) for k, v in settlements.items()},
        {k: tuple(v) for k, v in runs.items()},
        source_version=source_version,
    )
