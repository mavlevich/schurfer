"""Real PostgreSQL test of the durable one-read claim for the HYP-015 formal verdict.

Isolated: the test owns and drops only its own schema, created with the same table
definition as migration 0051. Skips without a local Postgres; runs in CI.
"""

from __future__ import annotations

# ruff: noqa: S608 -- test DDL into an isolated schema constant.
import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from schurfer_analytics.momentum_flow_hold12h_verdict import Hold12hVerdictContract
from schurfer_analytics.momentum_flow_hold12h_verdict_reader import Schemas, claim_formal_read

_PG_DSN = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"
_SCHEMA = "hold12h_claim_it"
_SCHEMAS = Schemas(timeseries=_SCHEMA, app=_SCHEMA)
_START = datetime(2026, 10, 5, tzinfo=UTC)
_END = datetime(2026, 11, 2, tzinfo=UTC)
_CONTRACT = dataclasses.replace(
    Hold12hVerdictContract(),
    cohort_start_iso=_START.isoformat(),
    decision_prefix_end_iso=_END.isoformat(),
)

_SETUP = f"""
DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE;
CREATE SCHEMA {_SCHEMA};
CREATE TABLE {_SCHEMA}.hold12h_formal_read_claims (
    id BIGSERIAL PRIMARY KEY,
    contract_version VARCHAR(64) NOT NULL,
    contract_sha256 VARCHAR(64) NOT NULL,
    cohort_start TIMESTAMPTZ NOT NULL,
    decision_prefix_end TIMESTAMPTZ NOT NULL,
    code_revision VARCHAR(64) NOT NULL,
    working_tree_dirty BOOLEAN NOT NULL,
    output_dir TEXT NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_hold12h_formal_read_claim_cohort
        UNIQUE (contract_version, cohort_start, decision_prefix_end));
"""


def _psycopg_or_skip() -> Any:
    psycopg = pytest.importorskip("psycopg")
    try:
        return psycopg.connect(_PG_DSN, autocommit=True)
    except Exception as exc:
        pytest.skip(f"no local postgres reachable: {exc}")


async def _claim(contract: Hold12hVerdictContract, output_dir: str) -> None:
    await claim_formal_read(
        _PG_DSN,
        contract,
        cohort_start=_START,
        decision_prefix_end=_END,
        code_revision="abc",
        working_tree_dirty=False,
        output_dir=Path(output_dir),
        schemas=_SCHEMAS,
    )


async def test_a_cohort_can_be_claimed_for_a_formal_read_only_once() -> None:
    conn = _psycopg_or_skip()
    try:
        with conn.cursor() as cur:
            cur.execute(_SETUP)
        await _claim(_CONTRACT, "/research/first")
        # Another output directory does not open a second read...
        with pytest.raises(SystemExit, match="already claimed"):
            await _claim(_CONTRACT, "/research/second")
        # ...and neither does an edited contract with a different sha for the same cohort.
        edited = dataclasses.replace(_CONTRACT, min_portfolio_improvement_usd=1.0)
        assert edited.sha256_hex() != _CONTRACT.sha256_hex()
        with pytest.raises(SystemExit, match="already claimed"):
            await _claim(edited, "/research/third")
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*), min(output_dir) FROM {_SCHEMA}.hold12h_formal_read_claims"
            )
            assert cur.fetchone() == (1, "/research/first")
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        conn.close()
