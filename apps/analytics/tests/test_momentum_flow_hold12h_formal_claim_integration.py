"""Real PostgreSQL test of the durable, resumable one-read claim for the HYP-015 verdict.

Isolated: the test owns and drops only its own schema, created with the same table
definition as migration 0053. Skips without a local Postgres; runs in CI.
"""

from __future__ import annotations

# ruff: noqa: S608 -- test DDL into an isolated schema constant.
import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics import momentum_flow_hold12h_verdict_reader as reader
from schurfer_analytics.momentum_flow_hold12h_snapshot import snapshot_digest
from schurfer_analytics.momentum_flow_hold12h_verdict import Hold12hVerdictContract
from schurfer_analytics.momentum_flow_hold12h_verdict_reader import (
    FormalClaim,
    FormalCoverage,
    Schemas,
    complete_formal_claim,
    open_formal_claim,
    pin_inputs_digest,
    pinned_inputs,
    publish_formal_result,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

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
_WATCHES = ["w-1", "w-2", "w-3"]
_COVERAGE = FormalCoverage(
    filled=3, open_positions=0, closed=3, funding_covered=3, accounting_complete=3
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
    watch_ids JSONB NOT NULL,
    watch_ids_sha256 VARCHAR(64) NOT NULL,
    inputs_digest VARCHAR(64),
    coverage_closed INTEGER NOT NULL,
    coverage_funding_covered INTEGER NOT NULL,
    coverage_accounting_complete INTEGER NOT NULL,
    coverage_open_positions INTEGER NOT NULL,
    accepted_incomplete_coverage BOOLEAN NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'claimed',
    lease_owner VARCHAR(64) NOT NULL,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    result_fingerprint VARCHAR(128),
    artifact_name TEXT,
    artifact_sha256 VARCHAR(64),
    CONSTRAINT ck_hold12h_formal_read_claim_status CHECK (status IN ('claimed', 'completed')),
    CONSTRAINT ck_hold12h_formal_read_claim_completed_at
        CHECK ((status = 'completed')
               = (completed_at IS NOT NULL AND artifact_sha256 IS NOT NULL)),
    CONSTRAINT uq_hold12h_formal_read_claim_cohort
        UNIQUE (contract_version, cohort_start, decision_prefix_end));
"""


@pytest.fixture
def pg() -> Iterator[Any]:
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(_PG_DSN, autocommit=True)
    except Exception as exc:
        pytest.skip(f"no local postgres reachable: {exc}")
    with conn.cursor() as cur:
        cur.execute(_SETUP)
    try:
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        conn.close()


async def _claim(
    contract: Hold12hVerdictContract = _CONTRACT,
    output_dir: str = "/research/first",
    watch_ids: list[str] = _WATCHES,
) -> FormalClaim:
    return await open_formal_claim(
        _PG_DSN,
        contract,
        cohort_start=_START,
        decision_prefix_end=_END,
        watch_ids=watch_ids,
        coverage=_COVERAGE,
        accepted_incomplete_coverage=False,
        code_revision="abc",
        working_tree_dirty=False,
        output_dir=Path(output_dir),
        schemas=_SCHEMAS,
    )


async def _complete(claim: FormalClaim) -> None:
    await complete_formal_claim(
        _PG_DSN,
        claim,
        "f" * 64,
        artifact_name=f"hold12h_verdict.attempt-{claim.owner}.json",
        artifact_sha256="a" * 64,
        schemas=_SCHEMAS,
    )


def _expire_lease(pg: Any) -> None:
    with pg.cursor() as cur:
        cur.execute(
            f"UPDATE {_SCHEMA}.hold12h_formal_read_claims "
            "SET lease_expires_at = now() - interval '1 second'"
        )


async def test_a_completed_cohort_is_never_read_again(pg: Any) -> None:
    claim = await _claim()
    assert not claim.resumed and claim.watch_ids == tuple(_WATCHES)
    await pin_inputs_digest(_PG_DSN, claim, "d" * 64, schemas=_SCHEMAS)
    await _complete(claim)
    # Another output directory does not open a second read...
    with pytest.raises(SystemExit, match="already claimed"):
        await _claim(output_dir="/research/second")
    # ...and neither does an edited contract with a different sha for the same cohort.
    edited = dataclasses.replace(_CONTRACT, min_portfolio_improvement_usd=1.0)
    assert edited.sha256_hex() != _CONTRACT.sha256_hex()
    with pytest.raises(SystemExit, match="already claimed"):
        await _claim(edited, "/research/third")
    with pg.cursor() as cur:
        cur.execute(
            f"SELECT count(*), min(output_dir), min(status) "
            f"FROM {_SCHEMA}.hold12h_formal_read_claims"
        )
        assert cur.fetchone() == (1, "/research/first", "completed")


async def test_a_run_that_crashed_after_the_claim_is_resumed_not_burned(pg: Any) -> None:
    first = await _claim()
    await pin_inputs_digest(_PG_DSN, first, "d" * 64, schemas=_SCHEMAS)
    # The first run dies here (disk full, crash): no artifact, claim still open.
    with pytest.raises(SystemExit, match="holds the open claim's lease"):
        await _claim()
    _expire_lease(pg)
    resumed = await _claim()
    assert resumed.resumed and resumed.id == first.id
    assert resumed.inputs_digest == "d" * 64
    # The dead run cannot complete once its lease was taken over.
    with pytest.raises(SystemExit, match="no longer owns"):
        await _complete(first)
    await pin_inputs_digest(_PG_DSN, resumed, "d" * 64, schemas=_SCHEMAS)
    await _complete(resumed)
    with pytest.raises(SystemExit, match="already claimed"):
        await _claim()


async def test_a_resume_must_read_the_same_contract_watches_and_inputs(pg: Any) -> None:
    first = await _claim()
    await pin_inputs_digest(_PG_DSN, first, "d" * 64, schemas=_SCHEMAS)
    _expire_lease(pg)
    edited = dataclasses.replace(_CONTRACT, min_portfolio_improvement_usd=1.0)
    with pytest.raises(SystemExit, match="another contract sha"):
        await _claim(edited)
    with pytest.raises(SystemExit, match="WATCH set changed"):
        await _claim(watch_ids=[*_WATCHES, "w-4"])
    resumed = await _claim()
    with pytest.raises(SystemExit, match="inputs changed"):
        await pin_inputs_digest(_PG_DSN, resumed, "e" * 64, schemas=_SCHEMAS)
    with pg.cursor() as cur:
        cur.execute(f"SELECT status, inputs_digest FROM {_SCHEMA}.hold12h_formal_read_claims")
        assert cur.fetchone() == ("claimed", "d" * 64)


async def test_a_stale_owner_never_publishes_or_overwrites_the_winner(
    pg: Any, tmp_path: Path
) -> None:
    stale = await _claim()
    await pin_inputs_digest(_PG_DSN, stale, "d" * 64, schemas=_SCHEMAS)
    _expire_lease(pg)
    winner = await _claim()
    # Both attempts overlap: each writes its own immutable artifact first.
    final = await publish_formal_result(
        _PG_DSN, winner, tmp_path, b'{"attempt":"winner"}\n', "f" * 64, schemas=_SCHEMAS
    )
    with pytest.raises(SystemExit, match="no longer owns"):
        await publish_formal_result(
            _PG_DSN, stale, tmp_path, b'{"attempt":"stale"}\n', "e" * 64, schemas=_SCHEMAS
        )
    assert final.read_bytes() == b'{"attempt":"winner"}\n'
    attempts = sorted(p.name for p in tmp_path.glob("hold12h_verdict.attempt-*.json"))
    assert attempts == sorted(f"hold12h_verdict.attempt-{c.owner}.json" for c in (stale, winner))
    with pg.cursor() as cur:
        cur.execute(f"SELECT status, artifact_name FROM {_SCHEMA}.hold12h_formal_read_claims")
        assert cur.fetchone() == ("completed", f"hold12h_verdict.attempt-{winner.owner}.json")


async def test_a_resume_computes_from_the_pinned_snapshot_not_changed_rows(
    pg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = {"body": b'{"version":"v","exit_resolved":true}'}

    async def load(*_: Any, **__: Any) -> bytes:
        return rows["body"]

    monkeypatch.setattr(reader, "load_snapshot_from_db", load)

    async def pinned(claim: FormalClaim) -> bytes:
        return await pinned_inputs(
            _PG_DSN,
            _CONTRACT,
            claim,
            tmp_path,
            cohort_start=_START,
            decision_prefix_end=_END,
            schemas=_SCHEMAS,
        )

    first = await _claim()
    original = await pinned(first)
    # The first run dies after pinning. Meanwhile a row changes (exit_resolved flips).
    rows["body"] = b'{"version":"v","exit_resolved":false}'
    _expire_lease(pg)
    resumed = await _claim()
    assert resumed.inputs_digest == snapshot_digest(original)
    assert await pinned(resumed) == original  # read from the pinned file, not the rows
    # Without the pinned file, the changed rows are refused rather than accepted.
    (tmp_path / f"inputs.{snapshot_digest(original)}.json").unlink()
    with pytest.raises(SystemExit, match="inputs changed"):
        await pinned(resumed)
