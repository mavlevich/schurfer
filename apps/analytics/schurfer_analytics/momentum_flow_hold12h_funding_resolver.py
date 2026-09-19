"""Prospective funding-settlement capture for the HYP-015 hold12h verdict.

For each exact HYP-015 instrument window this fetches the venue's actual funding-rate
history (via the shared bounded fetcher), parses each settlement, and persists it
idempotently along with a coverage run recording the requested bounds and terminal status.
It captures the COST side only -- it never reads a probe return.

Idempotent by construction: settlements are written ON CONFLICT DO NOTHING against the
``(exchange, native_market_id, settlement_at, source_version)`` unique key, so a retry
re-writes nothing. Idempotency only covers identical rows -- a re-fetch that returns a
different rate for a stored settlement raises ``FundingRateConflictError`` (a hard
integrity failure) rather than keeping the stale value.

A window is recorded ``complete`` ONLY when the fetch succeeded, was not truncated by
pagination, EVERY fetched row parsed, and the returned settlements bracket the target
interval (proving the endpoint reached across both bounds, not merely the edge of
available history). The request window is padded on each side so that bracketing is
achievable. Anything doubtful is a non-complete status that leaves the interval
accounting_incomplete downstream, never a silently-assumed full coverage. It captures the
COST side only -- it never reads a probe return.
"""

from __future__ import annotations

# ruff: noqa: S608 -- the app schema is a constant; every value is bound.
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from .derivatives_history import (
    METHOD_BY_NAME,
    DerivativesHistoryFetch,
    fetch_derivatives_history,
    source_timestamp_ms,
)
from .momentum_flow_hold12h_funding import ACTUAL_FUNDING_VERSION

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .momentum_flow_hold12h_verdict_report import InstrumentRoute

_FUNDING_METHOD = METHOD_BY_NAME["funding_rate_history"]


@dataclass(frozen=True)
class ParsedSettlement:
    settlement_at: datetime
    funding_rate: float
    native_payload: dict[str, Any]


def parse_settlement(row: Any) -> ParsedSettlement | None:
    """Parse one CCXT funding-history row, or ``None`` if it is not a usable settlement.

    Accepts any cadence (the timestamp is taken verbatim, not assumed 8h). Rejects a
    missing/invalid timestamp or a non-finite ``fundingRate`` so corrupt rows never become
    a silent zero."""
    if not isinstance(row, dict):
        return None
    ts_ms = source_timestamp_ms(row, "object")
    if ts_ms is None:
        return None
    rate = row.get("fundingRate")
    if isinstance(rate, bool) or not isinstance(rate, int | float):
        return None
    if not math.isfinite(float(rate)):
        return None
    return ParsedSettlement(
        settlement_at=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
        funding_rate=float(rate),
        native_payload=dict(row),
    )


def coverage_status(fetch: DerivativesHistoryFetch) -> str:
    """Map a fetch result to a terminal coverage status. Only a clean, fully-paged fetch is
    ``complete`` -- a fetch error or a pagination truncation leaves the window not proven."""
    if fetch.error_status == "fetch_failed":
        return "fetch_failed"
    if fetch.error_status == "invalid_response":
        return "invalid_response"
    if fetch.pagination_exhausted:
        return "pagination_exhausted"
    return "complete"


@dataclass(frozen=True)
class WindowCapture:
    status: str
    settlements_written: int
    request_count: int
    error: str | None
    integrity_conflict: bool = False


class FundingRateConflictError(RuntimeError):
    """A re-fetch returned a different funding rate for a settlement already stored under the
    same ``(exchange, native_market_id, settlement_at, source_version)``. Idempotency only
    holds for identical rows, so a changed rate is a hard integrity failure: the window is
    left non-complete instead of silently keeping the stale first-writer value."""


def window_status(
    fetch: DerivativesHistoryFetch,
    settlements: Sequence[ParsedSettlement],
    parse_failures: int,
    *,
    entry: datetime,
    exit_at: datetime,
) -> str:
    """Terminal coverage status for one funding window, conservative by construction.

    ``complete`` requires ALL of: a clean, fully-paged fetch; every fetched row parsed
    (no silent drop); and the returned settlements demonstrably BRACKET the target
    interval -- at least one at or before ``entry`` (proves the endpoint's history reaches
    back across the start, so nothing just after entry was truncated) AND at least one at
    or after ``exit_at`` (proves it reaches past the end, not that it merely hit the
    boundary of available history). Anything doubtful -- a dropped row, or a response that
    cannot prove both bounds -- is ``incomplete``, never a silently-assumed full coverage.
    A proven-zero-funding window is only possible when both brackets are seen but none fall
    inside ``(entry, exit]``."""
    base = coverage_status(fetch)
    if base != "complete":
        return base
    if parse_failures > 0:
        return "incomplete"
    covers_start = any(s.settlement_at <= entry for s in settlements)
    covers_end = any(s.settlement_at >= exit_at for s in settlements)
    return "complete" if covers_start and covers_end else "incomplete"


class FundingWriter(Protocol):
    """The persistence resolve_window needs -- satisfied by Hold12hFundingRepository (and
    by an in-memory fake in tests), so the orchestration is decoupled from the DB."""

    async def write_settlements(
        self,
        route: InstrumentRoute,
        settlements: Sequence[ParsedSettlement],
        *,
        now: datetime,
        source_version: str,
    ) -> int: ...

    async def write_coverage_run(
        self,
        route: InstrumentRoute,
        *,
        requested_since: datetime,
        requested_until: datetime,
        status: str,
        request_count: int,
        settlements_written: int,
        error: str | None,
        source_version: str,
    ) -> None: ...


async def resolve_window(
    exchange: Any,
    repo: FundingWriter,
    route: InstrumentRoute,
    *,
    entry: datetime,
    exit_at: datetime,
    now: datetime,
    boundary_pad_hours: float = 12.0,
    source_version: str = ACTUAL_FUNDING_VERSION,
    limit: int = 200,
    max_pages: int = 10,
    timeout_seconds: float = 30.0,
) -> WindowCapture:
    """Fetch, parse and persist the funding settlements for one instrument window, then
    record the coverage run. The requested window is padded by ``boundary_pad_hours`` on
    each side (>= one funding cadence) so a ``complete`` run can PROVE it bracketed the
    target interval rather than stopping at the boundary of available history. The run is
    recorded last so a crash mid-write leaves it non-complete (fail-closed)."""
    from datetime import timedelta

    pad = timedelta(hours=boundary_pad_hours)
    since = entry - pad
    until = exit_at + pad
    fetch = await fetch_derivatives_history(
        exchange,
        _FUNDING_METHOD,
        route.unified_symbol,
        timeframe=None,
        since_ms=int(since.timestamp() * 1000),
        until_ms=int(until.timestamp() * 1000),
        limit=limit,
        max_pages=max_pages,
        timeout_seconds=timeout_seconds,
    )
    parsed = [p for row in fetch.rows if (p := parse_settlement(row)) is not None]
    parse_failures = len(fetch.rows) - len(parsed)
    status = window_status(fetch, parsed, parse_failures, entry=entry, exit_at=exit_at)
    error = fetch.error
    integrity_conflict = False
    try:
        written = await repo.write_settlements(
            route, parsed, now=now, source_version=source_version
        )
    except FundingRateConflictError as exc:
        # A changed venue rate on re-fetch is an integrity failure: never mark the window
        # complete, and surface the conflict on the run instead of overwriting silently.
        status, written, error, integrity_conflict = "incomplete", 0, str(exc), True
    await repo.write_coverage_run(
        route,
        requested_since=since,
        requested_until=until,
        status=status,
        request_count=fetch.request_count,
        settlements_written=written,
        error=error,
        source_version=source_version,
    )
    return WindowCapture(status, written, fetch.request_count, error, integrity_conflict)


class Hold12hFundingRepository:
    """Persists captured settlements (idempotent) and coverage runs."""

    def __init__(self, engine: Any, *, app_schema: str = "app") -> None:
        self._engine = engine
        self._app = app_schema

    @classmethod
    def from_url(cls, db_url: str, *, app_schema: str = "app") -> Hold12hFundingRepository:
        from sqlalchemy.ext.asyncio import create_async_engine

        from .outcome_repository import async_database_url

        engine = create_async_engine(async_database_url(db_url), pool_pre_ping=True, pool_size=1)
        return cls(engine, app_schema=app_schema)

    async def dispose(self) -> None:
        await self._engine.dispose()

    async def write_settlements(
        self,
        route: InstrumentRoute,
        settlements: Sequence[ParsedSettlement],
        *,
        now: datetime,
        source_version: str,
    ) -> int:
        """Idempotent insert; returns how many NEW rows were written.

        Idempotency only holds for identical rows: after the insert we re-read the stored
        rate for every incoming settlement and raise ``FundingRateConflictError`` if the
        venue returned a different rate for one already recorded, so a changed rate is a
        hard integrity failure rather than a silently-kept stale value."""
        if not settlements:
            return 0
        from sqlalchemy import bindparam, text

        rows = [
            {
                "exchange": route.exchange,
                "native_market_id": route.market_id,
                "unified_symbol": route.unified_symbol,
                "market_type": route.market_type,
                "settlement_at": s.settlement_at,
                "funding_rate": s.funding_rate,
                "source_at": s.settlement_at,
                "observed_at": now,
                "fetched_at": now,
                "native_payload": _json(s.native_payload),
                "source_version": source_version,
            }
            for s in settlements
        ]
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(
                    f"""
                    INSERT INTO {self._app}.hold12h_funding_settlements
                        (exchange, native_market_id, unified_symbol, market_type,
                         settlement_at, funding_rate, source_at, observed_at, fetched_at,
                         native_payload, source_version)
                    VALUES (:exchange, :native_market_id, :unified_symbol, :market_type,
                            :settlement_at, :funding_rate, :source_at, :observed_at,
                            :fetched_at, CAST(:native_payload AS JSONB), :source_version)
                    ON CONFLICT ON CONSTRAINT uq_hold12h_funding_settlement DO NOTHING
                    """
                ),
                rows,
            )
            written = int(result.rowcount or 0)
            incoming = {s.settlement_at: s.funding_rate for s in settlements}
            stored = (
                (
                    await conn.execute(
                        text(
                            f"""
                            SELECT settlement_at, funding_rate
                            FROM {self._app}.hold12h_funding_settlements
                            WHERE exchange = :exchange
                              AND native_market_id = :native_market_id
                              AND source_version = :source_version
                              AND settlement_at IN :times
                            """
                        ).bindparams(bindparam("times", expanding=True)),
                        {
                            "exchange": route.exchange,
                            "native_market_id": route.market_id,
                            "source_version": source_version,
                            "times": list(incoming),
                        },
                    )
                )
                .mappings()
                .all()
            )
            conflicts = [
                (row["settlement_at"], row["funding_rate"], incoming[row["settlement_at"]])
                for row in stored
                if row["settlement_at"] in incoming
                and row["funding_rate"] != incoming[row["settlement_at"]]
            ]
            if conflicts:
                detail = ", ".join(
                    f"{at.isoformat()} stored={stored_rate!r} incoming={incoming_rate!r}"
                    for at, stored_rate, incoming_rate in conflicts[:5]
                )
                raise FundingRateConflictError(
                    f"{route.exchange}:{route.market_id} funding rate changed on re-fetch "
                    f"for {len(conflicts)} settlement(s): {detail}"
                )
            return written

    async def write_coverage_run(
        self,
        route: InstrumentRoute,
        *,
        requested_since: datetime,
        requested_until: datetime,
        status: str,
        request_count: int,
        settlements_written: int,
        error: str | None,
        source_version: str,
    ) -> None:
        from sqlalchemy import text

        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    f"""
                    INSERT INTO {self._app}.hold12h_funding_coverage_runs
                        (exchange, native_market_id, unified_symbol, market_type,
                         requested_since, requested_until, status, request_count,
                         settlements_written, error, source_version)
                    VALUES (:exchange, :native_market_id, :unified_symbol, :market_type,
                            :requested_since, :requested_until, :status, :request_count,
                            :settlements_written, :error, :source_version)
                    """
                ),
                {
                    "exchange": route.exchange,
                    "native_market_id": route.market_id,
                    "unified_symbol": route.unified_symbol,
                    "market_type": route.market_type,
                    "requested_since": requested_since,
                    "requested_until": requested_until,
                    "status": status,
                    "request_count": request_count,
                    "settlements_written": settlements_written,
                    "error": error,
                    "source_version": source_version,
                },
            )


def _json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, default=str, sort_keys=True)


# --- orchestration -------------------------------------------------------------
#
# Reads which closed hold12h probe intervals still lack proven funding coverage (probe
# TIMESTAMPS + route only -- never a return), fetches each once, and records it. Bounded
# to the exact HYP-015 instruments and gated by a settlement-publication lag.


async def pending_windows(
    conn: Any,
    *,
    source_version: str,
    cutoff: datetime,
    max_windows: int,
    app_schema: str = "app",
) -> tuple[tuple[InstrumentRoute, datetime, datetime], ...]:
    """Closed hold12h probe intervals with no ``complete`` coverage run of ``source_version``
    spanning them and whose exit is older than ``cutoff`` (past the settlement lag).

    Only probes with a resolved ``unified_symbol`` (the worker's exact market resolution)
    are returned -- that resolved symbol is what CCXT is queried with, keyed on the native
    ``market_id``; the raw ``symbol`` ticker is never sent to the venue. Bounded by
    ``max_windows`` (oldest exit first) so one run drains a finite slice of the backlog."""
    from sqlalchemy import text

    from .momentum_flow_hold12h_verdict_report import InstrumentRoute as _Route
    from .momentum_flow_paper_contract import HOLD12H_PAPER_CONTRACT

    rows = (
        (
            await conn.execute(
                text(
                    f"""
                    SELECT DISTINCT p.exchange, p.market_type, p.unified_symbol, p.market_id,
                           p.entry_at, p.exit_at
                    FROM {app_schema}.momentum_flow_paper_probes p
                    WHERE p.paper_version = :pv AND p.position_status = 'closed'
                      AND p.entry_at IS NOT NULL AND p.exit_at IS NOT NULL
                      AND p.exit_at <= :cutoff AND p.market_id IS NOT NULL
                      AND p.unified_symbol IS NOT NULL
                      AND NOT EXISTS (
                        SELECT 1 FROM {app_schema}.hold12h_funding_coverage_runs r
                        WHERE r.exchange = p.exchange AND r.native_market_id = p.market_id
                          AND r.source_version = :sv AND r.status = 'complete'
                          AND r.requested_since <= p.entry_at AND r.requested_until >= p.exit_at)
                    ORDER BY p.exit_at
                    LIMIT :max_windows
                    """
                ),
                {
                    "pv": HOLD12H_PAPER_CONTRACT.paper_version,
                    "sv": source_version,
                    "cutoff": cutoff,
                    "max_windows": max_windows,
                },
            )
        )
        .mappings()
        .all()
    )
    return tuple(
        (
            _Route(
                exchange=str(r["exchange"]),
                market_type=str(r["market_type"]),
                market_id=str(r["market_id"]),
                unified_symbol=str(r["unified_symbol"]),
            ),
            r["entry_at"].astimezone(UTC),
            r["exit_at"].astimezone(UTC),
        )
        for r in rows
    )


async def run_capture(
    db_url: str,
    *,
    now: datetime | None = None,
    settlement_lag_hours: float = 8.0,
    boundary_pad_hours: float = 12.0,
    max_windows: int = 500,
    source_version: str = ACTUAL_FUNDING_VERSION,
    app_schema: str = "app",
) -> dict[str, int]:
    """Capture funding for a bounded slice of pending hold12h intervals (``max_windows``,
    oldest first). Returns a small health summary for monitoring."""
    from datetime import timedelta

    from sqlalchemy.ext.asyncio import create_async_engine

    from .exchange_registry import EXCHANGE_FACTORIES
    from .outcome_repository import async_database_url

    now = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = now - timedelta(hours=settlement_lag_hours)

    engine = create_async_engine(async_database_url(db_url), pool_pre_ping=True, pool_size=1)
    try:
        async with engine.connect() as conn:
            windows = await pending_windows(
                conn,
                source_version=source_version,
                cutoff=cutoff,
                max_windows=max_windows,
                app_schema=app_schema,
            )
    finally:
        await engine.dispose()

    repo = Hold12hFundingRepository.from_url(db_url, app_schema=app_schema)
    clients: dict[str, Any] = {}
    summary = {"pending": len(windows), "complete": 0, "incomplete": 0, "integrity_conflicts": 0}
    try:
        for route, entry, exit_at in windows:
            client = clients.get(route.exchange)
            if client is None:
                factory = EXCHANGE_FACTORIES.get(route.exchange)
                if factory is None:
                    summary["incomplete"] += 1
                    continue
                client = factory()
                clients[route.exchange] = client
            capture = await resolve_window(
                client,
                repo,
                route,
                entry=entry,
                exit_at=exit_at,
                now=now,
                boundary_pad_hours=boundary_pad_hours,
                source_version=source_version,
            )
            summary["complete" if capture.status == "complete" else "incomplete"] += 1
            if capture.integrity_conflict:
                summary["integrity_conflicts"] += 1
    finally:
        import contextlib

        for client in clients.values():
            with contextlib.suppress(Exception):
                await client.close()
        await repo.dispose()
    return summary


def main() -> None:
    import argparse
    import asyncio
    import json as _json_mod
    import os
    import sys

    parser = argparse.ArgumentParser(description="HYP-015 hold12h funding capture")
    parser.add_argument("--settlement-lag-hours", type=float, default=8.0)
    parser.add_argument("--boundary-pad-hours", type=float, default=12.0)
    parser.add_argument("--max-windows", type=int, default=500)
    args = parser.parse_args()
    if args.max_windows < 1:
        raise ValueError("--max-windows must be at least 1")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for the hold12h funding capture")
    summary = asyncio.run(
        run_capture(
            db_url,
            settlement_lag_hours=args.settlement_lag_hours,
            boundary_pad_hours=args.boundary_pad_hours,
            max_windows=args.max_windows,
        )
    )
    sys.stdout.write(_json_mod.dumps(summary, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
