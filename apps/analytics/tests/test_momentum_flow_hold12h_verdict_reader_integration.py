"""Real PostgreSQL reader-path test for the HYP-015 hold12h verdict loader.

The mandatory integration test. It is ISOLATED: it creates its OWN schema
(``hold12h_verdict_it``), points the loader at it via ``Schemas``, and drops ONLY that
schema -- it never touches the shared ``timeseries``/``app`` tables, so it cannot delete
real data or the migrated schema. It seeds the loader's queried columns with the REAL
status strings (``opened``/``closed``/``complete``/``ready``) and the contract's WATCH
filters, so a status/filter mismatch would fail the test. Skips without a local Postgres;
runs in CI (same pattern as the other ``*_integration`` tests).
"""

from __future__ import annotations

# ruff: noqa: S608 -- test DDL/inserts into an isolated schema constant; values are bound.
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from schurfer_analytics.momentum_flow_hold12h_verdict import HOLD12H_VERDICT_CONTRACT
from schurfer_analytics.momentum_flow_hold12h_verdict_reader import (
    Schemas,
    load_cohort,
    run_formal_for_test,
)
from schurfer_analytics.momentum_flow_hold12h_verdict_report import (
    FundingCoverage,
    InstrumentRoute,
    ProbeClass,
)
from schurfer_analytics.momentum_flow_paper_contract import HOLD12H_PAPER_CONTRACT

_PG_DSN = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"
_SCHEMA = "hold12h_verdict_it"
_SCHEMAS = Schemas(timeseries=_SCHEMA, app=_SCHEMA)
_D = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
_WV = HOLD12H_PAPER_CONTRACT.watch_version
_EX = HOLD12H_PAPER_CONTRACT.source_exchange
_MT = HOLD12H_PAPER_CONTRACT.market_type


class _ProvenZeroFunding:
    def coverage(
        self, route: InstrumentRoute, entry_at: datetime, exit_at: datetime
    ) -> FundingCoverage | None:
        return FundingCoverage(events=(), proven_full_coverage=True)


def _psycopg_or_skip() -> Any:
    psycopg = pytest.importorskip("psycopg")
    try:
        return psycopg.connect(_PG_DSN, connect_timeout=2, autocommit=True)
    except psycopg.Error as exc:
        pytest.skip(f"no local postgres reachable: {exc}")


_SETUP = f"""
DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE;
CREATE SCHEMA {_SCHEMA};
CREATE TABLE {_SCHEMA}.momentum_flow_watch_evaluations_1m (
    watch_id UUID, decision_at TIMESTAMPTZ, decision_status TEXT, watch_version TEXT,
    exchange TEXT, market_type TEXT, symbol TEXT, episode_id UUID);
CREATE TABLE {_SCHEMA}.momentum_flow_paper_probes (
    paper_id UUID, watch_id UUID, paper_version TEXT, exchange TEXT, market_type TEXT,
    symbol TEXT, unified_symbol TEXT, market_id TEXT, entry_status TEXT,
    position_status TEXT, exit_reason TEXT,
    entry_at TIMESTAMPTZ, exit_at TIMESTAMPTZ, gross_return_pct DOUBLE PRECISION,
    fees_usd DOUBLE PRECISION, max_adverse_return_pct DOUBLE PRECISION,
    entry_filled_notional_usd DOUBLE PRECISION);
CREATE TABLE {_SCHEMA}.momentum_flow_paper_outcomes (
    paper_id UUID, horizon_minutes INTEGER, status TEXT, quote_observed_at TIMESTAMPTZ,
    gross_return_pct DOUBLE PRECISION, fees_usd DOUBLE PRECISION,
    filled_notional_usd DOUBLE PRECISION);
CREATE TABLE {_SCHEMA}.momentum_universe_snapshots (
    exchange TEXT, universe_version TEXT, catalog_version TEXT, captured_at TIMESTAMPTZ);
CREATE TABLE {_SCHEMA}.momentum_universe_instruments (
    exchange TEXT, universe_version TEXT, catalog_version TEXT, native_market_id TEXT,
    identity_status TEXT, identity_key TEXT);
"""


def _seed(cur: Any) -> tuple[str, str]:
    cur.execute(_SETUP)
    # Two universe snapshots: the NEWER one no longer lists DOGE, so DOGE resolves to None
    # (proves latest-snapshot-first, no fallback to the older snapshot).
    cur.execute(
        f"INSERT INTO {_SCHEMA}.momentum_universe_snapshots VALUES "
        "(%s,'uv_old','cv_old',%s),(%s,'uv_new','cv_new',%s)",
        (_EX, _D - timedelta(days=2), _EX, _D - timedelta(hours=1)),
    )
    cur.execute(
        f"INSERT INTO {_SCHEMA}.momentum_universe_instruments VALUES "
        "(%s,'uv_old','cv_old','DOGEUSDT','ready','DOGE-canon'),"
        "(%s,'uv_new','cv_new','BTCUSDT','ready','BTC-canon')",
        (_EX, _EX),
    )
    w_ok, w_noid = str(uuid.uuid4()), str(uuid.uuid4())
    paper_id = str(uuid.uuid4())
    for wid, sym in ((w_ok, "BTCUSDT"), (w_noid, "DOGEUSDT")):
        cur.execute(
            f"INSERT INTO {_SCHEMA}.momentum_flow_watch_evaluations_1m "
            "VALUES (%s,%s,'watch',%s,%s,%s,%s,%s)",
            (wid, _D, _WV, _EX, _MT, sym, str(uuid.uuid4())),
        )
    entry_at = _D + timedelta(minutes=5)
    cur.execute(
        f"INSERT INTO {_SCHEMA}.momentum_flow_paper_probes VALUES "
        "(%s,%s,%s,%s,%s,'BTCUSDT','BTC/USDT:USDT','BTCUSDT','opened','closed','max_hold',"
        "%s,%s,2.5,0.5,-1.0,50.0)",
        (
            paper_id,
            w_ok,
            HOLD12H_PAPER_CONTRACT.paper_version,
            _EX,
            _MT,
            entry_at,
            entry_at + timedelta(minutes=720),
        ),
    )
    cur.execute(
        f"INSERT INTO {_SCHEMA}.momentum_flow_paper_outcomes VALUES "
        "(%s,240,'complete',%s,1.5,0.5,50.0),(%s,720,'complete',%s,2.5,0.5,50.0)",
        (paper_id, entry_at + timedelta(minutes=240), paper_id, entry_at + timedelta(minutes=720)),
    )
    return w_ok, w_noid


async def test_reader_maps_real_rows_into_the_pure_pipeline() -> None:
    conn = _psycopg_or_skip()
    try:
        with conn.cursor() as cur:
            _seed(cur)

        # Full formal pipeline (load -> evaluate -> verdict -> fingerprint) with an
        # injected proven funding source, over the isolated schema.
        artifact, ev = await run_formal_for_test(
            HOLD12H_VERDICT_CONTRACT,
            _PG_DSN,
            cohort_start=_D - timedelta(days=1),
            decision_prefix_end=_D + timedelta(days=1),
            funding=_ProvenZeroFunding(),
            schemas=_SCHEMAS,
        )
        # Denominator preserved; identity resolved for BTC (ready in the LATEST snapshot),
        # unresolved for DOGE (dropped from the latest snapshot -> no fallback to the older).
        assert ev.funnel[ProbeClass.ANALYZABLE] == 1
        assert ev.funnel[ProbeClass.IDENTITY_UNRESOLVED] == 1
        assert sum(ev.funnel.values()) == 2
        pair = ev.pairs[0]
        assert pair.canonical_asset == "BTC-canon"
        assert abs(pair.net_720 - 1.5) < 1e-9  # 2.5 - 0.5/50*100, zero funding
        assert abs(pair.net_240cf - 0.5) < 1e-9  # 1.5 - 0.5/50*100
        # The route carries the exact resolved unified symbol, not the raw ticker.
        _watches, probes = await load_cohort(
            _PG_DSN,
            cohort_start=_D - timedelta(days=1),
            decision_prefix_end=_D + timedelta(days=1),
            schemas=_SCHEMAS,
        )
        (route,) = {probe.route for probe in probes.values()}
        assert route.unified_symbol == "BTC/USDT:USDT"
        assert route.market_id == "BTCUSDT"
        assert artifact["portfolio"]["720m"]["taken"] == 1
        assert artifact["portfolio"]["240m"]["skipped_slots_full"] == 0
        assert artifact["mode"] == "formal"
        assert len(artifact["fingerprint"]) == 64
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")  # only our own schema
        conn.close()
