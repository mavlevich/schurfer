"""Real PostgreSQL test for the hold12h funding capture (idempotent write + load).

ISOLATED: creates its OWN schema (``hold12h_funding_it``), points the repository/loader
at it via ``app_schema``, and drops only that schema -- it never touches shared tables.
Skips without a local Postgres; runs in CI.
"""

from __future__ import annotations

# ruff: noqa: S608 -- the schema name is a test-only constant; every value is bound.
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from schurfer_analytics.momentum_flow_hold12h_funding import (
    ACTUAL_FUNDING_VERSION,
    load_stored_funding,
)
from schurfer_analytics.momentum_flow_hold12h_funding_resolver import (
    FundingRateConflictError,
    Hold12hFundingRepository,
    ParsedSettlement,
    pending_windows,
)
from schurfer_analytics.momentum_flow_hold12h_verdict_report import InstrumentRoute
from schurfer_analytics.momentum_flow_paper_contract import HOLD12H_PAPER_CONTRACT
from schurfer_analytics.outcome_repository import async_database_url
from sqlalchemy.ext.asyncio import create_async_engine

_PG_DSN = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"
_SCHEMA = "hold12h_funding_it"
_ENTRY = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
_EXIT = _ENTRY + timedelta(hours=12)
_ROUTE = InstrumentRoute("bybit", "linear", "FOOUSDT", "FOO/USDT:USDT")
# A second venue + market id so the loader's IN-list is exercised with >1 element
# (a tuple-shaped ANY(...) would silently match nothing on real PostgreSQL).
_ROUTE_2 = InstrumentRoute("binance", "linear", "BARUSDT", "BAR/USDT:USDT")

_SETUP = f"""
DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE;
CREATE SCHEMA {_SCHEMA};
CREATE TABLE {_SCHEMA}.hold12h_funding_settlements (
    id BIGSERIAL PRIMARY KEY, exchange TEXT, native_market_id TEXT, unified_symbol TEXT,
    market_type TEXT, settlement_at TIMESTAMPTZ, funding_rate DOUBLE PRECISION,
    source_at TIMESTAMPTZ, observed_at TIMESTAMPTZ, fetched_at TIMESTAMPTZ,
    native_payload JSONB, source_version TEXT,
    CONSTRAINT uq_hold12h_funding_settlement
        UNIQUE (exchange, native_market_id, settlement_at, source_version));
CREATE TABLE {_SCHEMA}.hold12h_funding_coverage_runs (
    id BIGSERIAL PRIMARY KEY, exchange TEXT, native_market_id TEXT, unified_symbol TEXT,
    market_type TEXT, requested_since TIMESTAMPTZ, requested_until TIMESTAMPTZ,
    status TEXT, request_count INTEGER, settlements_written INTEGER, error TEXT,
    source_version TEXT);
"""


def _psycopg_or_skip() -> Any:
    psycopg = pytest.importorskip("psycopg")
    try:
        return psycopg.connect(_PG_DSN, connect_timeout=2, autocommit=True)
    except psycopg.Error as exc:
        pytest.skip(f"no local postgres reachable: {exc}")


def _settlements() -> list[ParsedSettlement]:
    return [
        ParsedSettlement(_ENTRY + timedelta(hours=4), 0.0001, {"t": 1}),
        ParsedSettlement(_ENTRY + timedelta(hours=8), -0.0002, {"t": 2}),
    ]


async def test_capture_write_is_idempotent_and_loads_back() -> None:
    conn = _psycopg_or_skip()
    try:
        with conn.cursor() as cur:
            cur.execute(_SETUP)

        repo = Hold12hFundingRepository.from_url(_PG_DSN, app_schema=_SCHEMA)
        try:
            n1 = await repo.write_settlements(
                _ROUTE, _settlements(), now=_ENTRY, source_version=ACTUAL_FUNDING_VERSION
            )
            n2 = await repo.write_settlements(  # retry -> nothing new
                _ROUTE, _settlements(), now=_ENTRY, source_version=ACTUAL_FUNDING_VERSION
            )
            assert n1 == 2
            assert n2 == 0  # idempotent on the unique key
            # A second venue/market id so the loader's IN-list carries >1 element.
            await repo.write_settlements(
                _ROUTE_2,
                [ParsedSettlement(_ENTRY + timedelta(hours=6), 0.0005, {"t": 9})],
                now=_ENTRY,
                source_version=ACTUAL_FUNDING_VERSION,
            )
            for route, written in ((_ROUTE, 2), (_ROUTE_2, 1)):
                await repo.write_coverage_run(
                    route,
                    requested_since=_ENTRY - timedelta(hours=1),
                    requested_until=_EXIT + timedelta(hours=1),
                    status="complete",
                    request_count=1,
                    settlements_written=written,
                    error=None,
                    source_version=ACTUAL_FUNDING_VERSION,
                )
        finally:
            await repo.dispose()

        source = await load_stored_funding(_PG_DSN, [_ROUTE, _ROUTE_2], app_schema=_SCHEMA)
        cov = source.coverage(_ROUTE, _ENTRY, _EXIT)
        assert cov is not None and cov.proven_full_coverage
        assert sorted(e.rate for e in cov.events) == [-0.0002, 0.0001]
        # The second venue/market id loaded too: proves the IN-list matched >1 key
        # (a tuple-shaped ANY(...) would match neither on real PostgreSQL).
        cov2 = source.coverage(_ROUTE_2, _ENTRY, _EXIT)
        assert cov2 is not None and [e.rate for e in cov2.events] == [0.0005]
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        conn.close()


async def test_changed_rate_on_refetch_is_an_integrity_conflict() -> None:
    conn = _psycopg_or_skip()
    try:
        with conn.cursor() as cur:
            cur.execute(_SETUP)

        repo = Hold12hFundingRepository.from_url(_PG_DSN, app_schema=_SCHEMA)
        try:
            await repo.write_settlements(
                _ROUTE, _settlements(), now=_ENTRY, source_version=ACTUAL_FUNDING_VERSION
            )
            # A complete run makes the interval proven-covered to the reader.
            await repo.write_coverage_run(
                _ROUTE,
                requested_since=_ENTRY - timedelta(hours=12),
                requested_until=_EXIT + timedelta(hours=12),
                status="complete",
                request_count=1,
                settlements_written=2,
                error=None,
                source_version=ACTUAL_FUNDING_VERSION,
            )
            # Identical repeat is silently idempotent (no conflict).
            n_same = await repo.write_settlements(
                _ROUTE, _settlements(), now=_ENTRY, source_version=ACTUAL_FUNDING_VERSION
            )
            assert n_same == 0
            # Same key, different rate -> hard integrity failure, stale value kept.
            changed = [ParsedSettlement(_ENTRY + timedelta(hours=4), 0.9999, {"t": 1})]
            with pytest.raises(FundingRateConflictError):
                await repo.write_settlements(
                    _ROUTE, changed, now=_ENTRY, source_version=ACTUAL_FUNDING_VERSION
                )

            # Before the conflict is recorded, the complete run still proves coverage and the
            # ORIGINAL rate is intact (the conflicting write did not overwrite it).
            source = await load_stored_funding(_PG_DSN, [_ROUTE], app_schema=_SCHEMA)
            cov = source.coverage(_ROUTE, _ENTRY, _EXIT)
            assert cov is not None
            assert 0.9999 not in {e.rate for e in cov.events}
            assert 0.0001 in {e.rate for e in cov.events}

            # resolve_window records a blocking integrity_conflict run on the window.
            await repo.write_coverage_run(
                _ROUTE,
                requested_since=_ENTRY - timedelta(hours=12),
                requested_until=_EXIT + timedelta(hours=12),
                status="integrity_conflict",
                request_count=1,
                settlements_written=0,
                error="funding rate changed on re-fetch",
                source_version=ACTUAL_FUNDING_VERSION,
            )
        finally:
            await repo.dispose()

        # The overlapping integrity_conflict now invalidates the complete run: the interval
        # is accounting_incomplete until resolved, so the verdict cannot use the stale rate.
        source = await load_stored_funding(_PG_DSN, [_ROUTE], app_schema=_SCHEMA)
        assert source.coverage(_ROUTE, _ENTRY, _EXIT) is None
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        conn.close()


_SCHEMA_Q = "hold12h_funding_q_it"
_QUEUE_SETUP = f"""
DROP SCHEMA IF EXISTS {_SCHEMA_Q} CASCADE;
CREATE SCHEMA {_SCHEMA_Q};
CREATE TABLE {_SCHEMA_Q}.momentum_flow_paper_probes (
    id BIGSERIAL PRIMARY KEY, paper_version TEXT, position_status TEXT,
    exchange TEXT, market_type TEXT, symbol TEXT, unified_symbol TEXT, market_id TEXT,
    entry_at TIMESTAMPTZ, exit_at TIMESTAMPTZ);
CREATE TABLE {_SCHEMA_Q}.hold12h_funding_coverage_runs (
    id BIGSERIAL PRIMARY KEY, exchange TEXT, native_market_id TEXT, unified_symbol TEXT,
    market_type TEXT, requested_since TIMESTAMPTZ, requested_until TIMESTAMPTZ,
    status TEXT, request_count INTEGER, settlements_written INTEGER, error TEXT,
    source_version TEXT, created_at TIMESTAMPTZ DEFAULT now());
"""


async def test_pending_windows_fair_queue_never_starves_a_fresh_window() -> None:
    # 500 already-attempted-but-still-incomplete windows plus one brand-new one; the new
    # window (never attempted) must appear within a small max_windows budget, i.e. a stuck
    # backlog can never permanently occupy the run limit.
    conn = _psycopg_or_skip()
    pv = HOLD12H_PAPER_CONTRACT.paper_version
    base = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    try:
        with conn.cursor() as cur:
            cur.execute(_QUEUE_SETUP)
            # 500 stuck windows, OLDER exit than the fresh one, each with a recent
            # 'incomplete' attempt (so they sort AFTER the never-attempted fresh window).
            for i in range(500):
                entry = base - timedelta(hours=48) - timedelta(hours=i)
                exit_at = entry + timedelta(hours=12)
                mid = f"STUCK{i}USDT"
                cur.execute(
                    f"INSERT INTO {_SCHEMA_Q}.momentum_flow_paper_probes "
                    "(paper_version, position_status, exchange, market_type, symbol, "
                    " unified_symbol, market_id, entry_at, exit_at) "
                    "VALUES (%s,'closed','bybit','linear',%s,%s,%s,%s,%s)",
                    (pv, mid, f"{mid}:linear", mid, entry, exit_at),
                )
                cur.execute(
                    f"INSERT INTO {_SCHEMA_Q}.hold12h_funding_coverage_runs "
                    "(exchange, native_market_id, unified_symbol, market_type, "
                    " requested_since, requested_until, status, request_count, "
                    " settlements_written, error, source_version, created_at) "
                    "VALUES ('bybit',%s,%s,'linear',%s,%s,'incomplete',1,0,NULL,%s,now())",
                    (
                        mid,
                        f"{mid}:linear",
                        entry - timedelta(hours=12),
                        exit_at + timedelta(hours=12),
                        ACTUAL_FUNDING_VERSION,
                    ),
                )
            # One fresh, never-attempted window with the NEWEST exit.
            fresh_entry = base - timedelta(hours=1)
            fresh_exit = fresh_entry + timedelta(hours=12)
            cur.execute(
                f"INSERT INTO {_SCHEMA_Q}.momentum_flow_paper_probes "
                "(paper_version, position_status, exchange, market_type, symbol, "
                " unified_symbol, market_id, entry_at, exit_at) "
                "VALUES (%s,'closed','bybit','linear','FRESHUSDT','FRESH:linear',"
                "'FRESHUSDT',%s,%s)",
                (pv, fresh_entry, fresh_exit),
            )

        engine = create_async_engine(async_database_url(_PG_DSN), pool_pre_ping=True, pool_size=1)
        try:
            async with engine.connect() as aconn:
                windows = await pending_windows(
                    aconn,
                    source_version=ACTUAL_FUNDING_VERSION,
                    cutoff=base + timedelta(days=7),
                    max_windows=3,
                    capture_start=base - timedelta(days=60),
                    app_schema=_SCHEMA_Q,
                )
        finally:
            await engine.dispose()

        assert len(windows) == 3
        market_ids = {route.market_id for route, _entry, _exit in windows}
        assert "FRESHUSDT" in market_ids  # the fresh window is not starved by the backlog
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA_Q} CASCADE")
        conn.close()
