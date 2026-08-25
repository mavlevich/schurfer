import os

import psycopg
import pytest
from schurfer_execution.early_momentum_prospective_cohort import (
    COHORT_KEY,
    register_prospective_cohort,
)

TEST_DATABASE_URL = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"


async def _connect_or_skip() -> psycopg.AsyncConnection:
    try:
        return await psycopg.AsyncConnection.connect(TEST_DATABASE_URL)
    except Exception as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but Postgres is unreachable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres reachable: {exc}")


async def test_registration_is_first_writer_wins_and_rejects_contract_drift() -> None:
    connection = await _connect_or_skip()
    try:
        await connection.execute(
            "DELETE FROM app.research_cohort_registrations WHERE cohort_key = %s",
            (COHORT_KEY,),
        )
        await connection.commit()
        first = await register_prospective_cohort(
            TEST_DATABASE_URL,
            contract_sha256=b"a" * 32,
            runtime_policy_sha256=b"b" * 32,
        )
        same = await register_prospective_cohort(
            TEST_DATABASE_URL,
            contract_sha256=b"a" * 32,
            runtime_policy_sha256=b"b" * 32,
        )
        assert same == first
        with pytest.raises(RuntimeError, match="different strategy contract"):
            await register_prospective_cohort(
                TEST_DATABASE_URL,
                contract_sha256=b"c" * 32,
                runtime_policy_sha256=b"b" * 32,
            )
    finally:
        await connection.execute(
            "DELETE FROM app.research_cohort_registrations WHERE cohort_key = %s",
            (COHORT_KEY,),
        )
        await connection.commit()
        await connection.close()
