"""Prospective funding-settlement capture for the HYP-015 hold12h verdict.

For each exact HYP-015 instrument window this fetches the venue's actual funding-rate
history (via the shared bounded fetcher), parses each settlement, and persists it
idempotently along with a coverage run recording the requested bounds and terminal status.
It captures the COST side only -- it never reads a probe return.

Idempotent by construction: settlements are written ON CONFLICT DO NOTHING against the
``(exchange, native_market_id, settlement_at, source_version)`` unique key, so a retry
re-writes nothing. A window is recorded ``complete`` ONLY when the fetch succeeded and was
not truncated by pagination -- otherwise it is a non-complete status that leaves the
interval accounting_incomplete downstream, never a silently-assumed full coverage.
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
    since: datetime,
    until: datetime,
    now: datetime,
    source_version: str = ACTUAL_FUNDING_VERSION,
    limit: int = 200,
    max_pages: int = 10,
    timeout_seconds: float = 30.0,
) -> WindowCapture:
    """Fetch, parse and persist the funding settlements for one instrument window, then
    record the coverage run. Written before the run so a crash mid-write leaves the run
    non-complete (fail-closed)."""
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
    written = await repo.write_settlements(route, parsed, now=now, source_version=source_version)
    status = coverage_status(fetch)
    await repo.write_coverage_run(
        route,
        requested_since=since,
        requested_until=until,
        status=status,
        request_count=fetch.request_count,
        settlements_written=written,
        error=fetch.error,
        source_version=source_version,
    )
    return WindowCapture(status, written, fetch.request_count, fetch.error)


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
        """Idempotent insert; returns how many NEW rows were written."""
        if not settlements:
            return 0
        from sqlalchemy import text

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
            return int(result.rowcount or 0)

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
    conn: Any, *, source_version: str, cutoff: datetime, app_schema: str = "app"
) -> tuple[tuple[InstrumentRoute, datetime, datetime], ...]:
    """Closed hold12h probe intervals with no ``complete`` coverage run of ``source_version``
    spanning them and whose exit is older than ``cutoff`` (past the settlement lag)."""
    from sqlalchemy import text

    from .momentum_flow_hold12h_verdict_report import InstrumentRoute as _Route
    from .momentum_flow_paper_contract import HOLD12H_PAPER_CONTRACT

    rows = (
        (
            await conn.execute(
                text(
                    f"""
                    SELECT DISTINCT p.exchange, p.market_type, p.symbol, p.market_id,
                           p.entry_at, p.exit_at
                    FROM {app_schema}.momentum_flow_paper_probes p
                    WHERE p.paper_version = :pv AND p.position_status = 'closed'
                      AND p.entry_at IS NOT NULL AND p.exit_at IS NOT NULL
                      AND p.exit_at <= :cutoff AND p.market_id IS NOT NULL
                      AND NOT EXISTS (
                        SELECT 1 FROM {app_schema}.hold12h_funding_coverage_runs r
                        WHERE r.exchange = p.exchange AND r.native_market_id = p.market_id
                          AND r.source_version = :sv AND r.status = 'complete'
                          AND r.requested_since <= p.entry_at AND r.requested_until >= p.exit_at)
                    """
                ),
                {
                    "pv": HOLD12H_PAPER_CONTRACT.paper_version,
                    "sv": source_version,
                    "cutoff": cutoff,
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
                unified_symbol=str(r["symbol"]),
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
    source_version: str = ACTUAL_FUNDING_VERSION,
    app_schema: str = "app",
) -> dict[str, int]:
    """Capture funding for every pending hold12h interval. Returns a small health summary."""
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
                conn, source_version=source_version, cutoff=cutoff, app_schema=app_schema
            )
    finally:
        await engine.dispose()

    repo = Hold12hFundingRepository.from_url(db_url, app_schema=app_schema)
    clients: dict[str, Any] = {}
    summary = {"pending": len(windows), "complete": 0, "incomplete": 0}
    try:
        for route, since, until in windows:
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
                since=since,
                until=until,
                now=now,
                source_version=source_version,
            )
            summary["complete" if capture.status == "complete" else "incomplete"] += 1
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
    args = parser.parse_args()
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for the hold12h funding capture")
    summary = asyncio.run(run_capture(db_url, settlement_lag_hours=args.settlement_lag_hours))
    sys.stdout.write(_json_mod.dumps(summary, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
