"""Real PostgreSQL test for the outcome-blind HYP-015 health checkpoint query.

Isolated like the reader integration test: it owns and drops only its own schema. It seeds
the exact columns the checkpoint reads (statuses, claim/decision timestamps, coverage
runs) and no return column at all, so the query provably needs no outcome data. Skips
without a local Postgres; runs in CI.
"""

from __future__ import annotations

# ruff: noqa: S608 -- test DDL/inserts into an isolated schema constant; values are bound.
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from schurfer_analytics.momentum_flow_hold12h_verdict_reader import (
    Schemas,
    health_breaches,
    load_health_checkpoint,
)
from schurfer_analytics.momentum_flow_paper_contract import (
    FROZEN_PAPER_CONTRACT,
    HOLD12H_PAPER_CONTRACT,
)

_PG_DSN = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"
_SCHEMA = "hold12h_health_it"
_SCHEMAS = Schemas(timeseries=_SCHEMA, app=_SCHEMA)
_D = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
_WV = HOLD12H_PAPER_CONTRACT.watch_version
_EX = HOLD12H_PAPER_CONTRACT.source_exchange
_MT = HOLD12H_PAPER_CONTRACT.market_type
_FV = "hold12h_actual_funding_v2"

_SETUP = f"""
DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE;
CREATE SCHEMA {_SCHEMA};
CREATE TABLE {_SCHEMA}.momentum_flow_watch_evaluations_1m (
    watch_id UUID, decision_at TIMESTAMPTZ, decision_status TEXT, watch_version TEXT,
    exchange TEXT, market_type TEXT, symbol TEXT, episode_id UUID);
CREATE TABLE {_SCHEMA}.momentum_flow_paper_probes (
    watch_id UUID, paper_version TEXT, exchange TEXT, market_id TEXT, entry_status TEXT,
    position_status TEXT, accounting_status TEXT, watch_decision_at TIMESTAMPTZ,
    claimed_at TIMESTAMPTZ, entry_at TIMESTAMPTZ, exit_at TIMESTAMPTZ);
CREATE TABLE {_SCHEMA}.hold12h_funding_coverage_runs (
    exchange TEXT, native_market_id TEXT, source_version TEXT, status TEXT,
    requested_since TIMESTAMPTZ, requested_until TIMESTAMPTZ);
"""


def _psycopg_or_skip() -> Any:
    psycopg = pytest.importorskip("psycopg")
    try:
        return psycopg.connect(_PG_DSN, autocommit=True)
    except Exception as exc:
        pytest.skip(f"no local postgres reachable: {exc}")


def _seed(cur: Any) -> None:
    cur.execute(_SETUP)
    for index in range(4):
        watch_id = str(uuid.uuid4())
        decision_at = _D + timedelta(minutes=index)
        cur.execute(
            f"INSERT INTO {_SCHEMA}.momentum_flow_watch_evaluations_1m "
            "VALUES (%s,%s,'watch',%s,%s,%s,'FOOUSDT',%s)",
            (watch_id, decision_at, _WV, _EX, _MT, str(uuid.uuid4())),
        )
        market_id = f"M{index}USDT"
        # Baseline: never stale. hold12h: the last watch is stale (claimed 58s late).
        hold_stale = index == 3
        for version, stale, claim_s in (
            (FROZEN_PAPER_CONTRACT.paper_version, False, 10),
            (HOLD12H_PAPER_CONTRACT.paper_version, hold_stale, 58 if hold_stale else 15),
        ):
            entry_at = None if stale else decision_at + timedelta(seconds=claim_s + 1)
            cur.execute(
                f"INSERT INTO {_SCHEMA}.momentum_flow_paper_probes VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    watch_id,
                    version,
                    _EX,
                    market_id,
                    "rejected_stale" if stale else "opened",
                    "not_open" if stale else "closed",
                    None if stale else "complete",
                    decision_at,
                    decision_at + timedelta(seconds=claim_s),
                    entry_at,
                    None if stale else decision_at + timedelta(hours=12),
                ),
            )
        if index < 2:  # a complete run for two of the three closed hold12h positions
            cur.execute(
                f"INSERT INTO {_SCHEMA}.hold12h_funding_coverage_runs VALUES "
                "(%s,%s,%s,'complete',%s,%s)",
                (_EX, market_id, _FV, decision_at - timedelta(hours=1), _D + timedelta(days=1)),
            )
        if index == 1:  # ...but an overlapping integrity conflict invalidates the second
            cur.execute(
                f"INSERT INTO {_SCHEMA}.hold12h_funding_coverage_runs VALUES "
                "(%s,%s,%s,'integrity_conflict',%s,%s)",
                (_EX, market_id, _FV, decision_at, decision_at + timedelta(hours=2)),
            )
    # A WATCH the baseline claimed but hold12h never saw (a stopped hold12h worker).
    orphan = str(uuid.uuid4())
    cur.execute(
        f"INSERT INTO {_SCHEMA}.momentum_flow_watch_evaluations_1m "
        "VALUES (%s,%s,'watch',%s,%s,%s,'FOOUSDT',%s)",
        (orphan, _D + timedelta(minutes=10), _WV, _EX, _MT, str(uuid.uuid4())),
    )
    cur.execute(
        f"INSERT INTO {_SCHEMA}.momentum_flow_paper_probes VALUES "
        "(%s,%s,%s,'ORPHANUSDT','opened','open',NULL,%s,%s,%s,NULL)",
        (
            orphan,
            FROZEN_PAPER_CONTRACT.paper_version,
            _EX,
            _D + timedelta(minutes=10),
            _D + timedelta(minutes=10, seconds=9),
            _D + timedelta(minutes=10, seconds=10),
        ),
    )


async def test_health_checkpoint_counts_shared_watches_outcome_blind() -> None:
    conn = _psycopg_or_skip()
    try:
        with conn.cursor() as cur:
            _seed(cur)
        checkpoint = await load_health_checkpoint(
            _PG_DSN,
            since=_D - timedelta(hours=1),
            until=_D + timedelta(days=2),
            funding_version=_FV,
            schemas=_SCHEMAS,
        )
        assert checkpoint.eligible_watches == 5
        assert checkpoint.baseline_unclaimed == 0
        assert checkpoint.baseline_stale == 0
        assert checkpoint.hold12h_unclaimed == 1
        assert checkpoint.hold12h_stale == 1
        assert checkpoint.hold12h_claim_p50_seconds == pytest.approx(15.0)
        assert checkpoint.closed_positions_past_lag == 3
        assert checkpoint.funding_covered == 1  # the conflicted window does not count
        assert checkpoint.accounting_complete == 3
        # 40% lost on hold12h (stale + never claimed) vs 0% baseline breaches both parts.
        assert len(health_breaches(checkpoint)) == 2
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        conn.close()
