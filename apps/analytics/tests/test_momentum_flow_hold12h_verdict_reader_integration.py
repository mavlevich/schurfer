"""Real PostgreSQL reader-path test for the HYP-015 hold12h verdict loader.

The mandatory integration test: it seeds the ACTUAL table/column shapes the loader
queries (WATCH decisions, hold12h probes, per-horizon outcomes, and the universe identity
snapshot) in a real Postgres, runs ``load_cohort`` over the SQL path, and checks the
mapping -- point-in-time identity, ex-funding percent return, exit semantics, and the
identity-unresolved path -- feeding the pure ``evaluate_cohort``. Skips without a local
Postgres; runs in CI (same pattern as the other ``*_integration`` tests).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from schurfer_analytics.momentum_flow_hold12h_verdict import HOLD12H_VERDICT_CONTRACT
from schurfer_analytics.momentum_flow_hold12h_verdict_reader import load_cohort
from schurfer_analytics.momentum_flow_hold12h_verdict_report import (
    FundingCoverage,
    InstrumentRoute,
    ProbeClass,
    evaluate_cohort,
)
from schurfer_analytics.momentum_flow_paper_contract import HOLD12H_PAPER_CONTRACT

_PG_DSN = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"
_D = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


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


_SETUP = """
CREATE SCHEMA IF NOT EXISTS timeseries;
CREATE SCHEMA IF NOT EXISTS app;
DROP TABLE IF EXISTS timeseries.momentum_flow_watch_evaluations_1m;
DROP TABLE IF EXISTS app.momentum_flow_paper_probes;
DROP TABLE IF EXISTS app.momentum_flow_paper_outcomes;
DROP TABLE IF EXISTS app.momentum_universe_snapshots;
DROP TABLE IF EXISTS app.momentum_universe_instruments;
CREATE TABLE timeseries.momentum_flow_watch_evaluations_1m (
    watch_id UUID, decision_at TIMESTAMPTZ, decision_status TEXT,
    exchange TEXT, market_type TEXT, symbol TEXT);
CREATE TABLE app.momentum_flow_paper_probes (
    paper_id UUID, watch_id UUID, paper_version TEXT, exchange TEXT, market_type TEXT,
    symbol TEXT, market_id TEXT, entry_status TEXT, position_status TEXT, exit_reason TEXT,
    entry_at TIMESTAMPTZ, exit_at TIMESTAMPTZ, gross_return_pct DOUBLE PRECISION,
    fees_usd DOUBLE PRECISION, max_adverse_return_pct DOUBLE PRECISION,
    entry_filled_notional_usd DOUBLE PRECISION);
CREATE TABLE app.momentum_flow_paper_outcomes (
    paper_id UUID, horizon_minutes INTEGER, status TEXT, quote_observed_at TIMESTAMPTZ,
    gross_return_pct DOUBLE PRECISION, fees_usd DOUBLE PRECISION,
    filled_notional_usd DOUBLE PRECISION);
CREATE TABLE app.momentum_universe_snapshots (
    exchange TEXT, universe_version TEXT, catalog_version TEXT, captured_at TIMESTAMPTZ);
CREATE TABLE app.momentum_universe_instruments (
    exchange TEXT, universe_version TEXT, catalog_version TEXT, native_market_id TEXT,
    identity_status TEXT, identity_key TEXT);
"""


def _seed(cur: Any) -> tuple[str, str]:
    """Seed one fully-analyzable WATCH (BTC, identity resolved) and one WATCH whose
    instrument is absent from the universe (identity unresolved). Returns their ids."""
    cur.execute(_SETUP)
    cur.execute(
        "INSERT INTO app.momentum_universe_snapshots VALUES ('bybit','uv1','cv1',%s)",
        (_D - timedelta(hours=1),),
    )
    cur.execute(
        "INSERT INTO app.momentum_universe_instruments "
        "VALUES ('bybit','uv1','cv1','BTCUSDT','resolved','BTC-canon')"
    )
    w_ok, w_noid = str(uuid.uuid4()), str(uuid.uuid4())
    paper_id = str(uuid.uuid4())
    for wid, sym in ((w_ok, "BTCUSDT"), (w_noid, "DOGEUSDT")):
        cur.execute(
            "INSERT INTO timeseries.momentum_flow_watch_evaluations_1m "
            "VALUES (%s,%s,'watch','bybit','linear',%s)",
            (wid, _D, sym),
        )
    entry_at = _D + timedelta(minutes=5)
    cur.execute(
        "INSERT INTO app.momentum_flow_paper_probes VALUES "
        "(%s,%s,%s,'bybit','linear','BTCUSDT','BTCUSDT','opened','closed','max_hold',"
        "%s,%s,2.5,0.5,-1.0,50.0)",
        (
            paper_id,
            w_ok,
            HOLD12H_PAPER_CONTRACT.paper_version,
            entry_at,
            entry_at + timedelta(minutes=720),
        ),
    )
    cur.execute(
        "INSERT INTO app.momentum_flow_paper_outcomes VALUES "
        "(%s,240,'resolved',%s,1.5,0.5,50.0),(%s,720,'resolved',%s,2.5,0.5,50.0)",
        (paper_id, entry_at + timedelta(minutes=240), paper_id, entry_at + timedelta(minutes=720)),
    )
    return w_ok, w_noid


async def test_reader_maps_real_rows_into_the_pure_pipeline() -> None:
    conn = _psycopg_or_skip()
    try:
        with conn.cursor() as cur:
            w_ok, w_noid = _seed(cur)

        watches, probes = await load_cohort(
            _PG_DSN, cohort_start=_D - timedelta(days=1), decision_prefix_end=_D + timedelta(days=1)
        )
        by_id = {w.watch_id: w for w in watches}
        assert by_id[w_ok].canonical_asset == "BTC-canon"  # point-in-time identity
        assert by_id[w_noid].canonical_asset is None  # unresolved -> identity_unresolved

        probe = probes[w_ok]
        assert probe.entry_ok and probe.exit_resolved
        # ex-funding percent: gross 2.5 - fees 0.5/50*100 = 1.5
        assert abs((probe.actual_gross_return_pct or 0.0) - 1.5) < 1e-9

        ev = evaluate_cohort(HOLD12H_VERDICT_CONTRACT, watches, probes, _ProvenZeroFunding())
        assert ev.funnel[ProbeClass.ANALYZABLE] == 1
        assert ev.funnel[ProbeClass.IDENTITY_UNRESOLVED] == 1
        assert sum(ev.funnel.values()) == 2
        pair = ev.pairs[0]
        assert pair.canonical_asset == "BTC-canon"
        assert abs(pair.net_720 - 1.5) < 1e-9  # 720 ex-funding, zero funding
        assert abs(pair.net_240cf - 0.5) < 1e-9  # 240 mark: 1.5 - 0.5/50*100 = 0.5
    finally:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS timeseries.momentum_flow_watch_evaluations_1m")
            cur.execute("DROP TABLE IF EXISTS app.momentum_flow_paper_probes")
            cur.execute("DROP TABLE IF EXISTS app.momentum_flow_paper_outcomes")
            cur.execute("DROP TABLE IF EXISTS app.momentum_universe_snapshots")
            cur.execute("DROP TABLE IF EXISTS app.momentum_universe_instruments")
        conn.close()
