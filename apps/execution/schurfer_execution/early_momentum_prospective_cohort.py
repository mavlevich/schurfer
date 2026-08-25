"""Durably register the early_momentum prospective cohort before workers start."""

from __future__ import annotations

from datetime import datetime

import psycopg
from psycopg.rows import dict_row

COHORT_KEY = "early_momentum_v4_prospective_v1"
STRATEGY_NAME = "early_momentum"
STRATEGY_VERSION = "4"


async def register_prospective_cohort(
    db_url: str, *, contract_sha256: bytes, runtime_policy_sha256: bytes
) -> datetime:
    """First startup wins; later startups must match the registered contract."""
    async with (
        await psycopg.AsyncConnection.connect(db_url) as conn,
        conn.cursor(row_factory=dict_row) as cur,
    ):
        await cur.execute(
            """
            INSERT INTO app.research_cohort_registrations (
                cohort_key, strategy_name, strategy_version,
                contract_sha256, runtime_policy_sha256, cohort_started_at
            )
            VALUES (%s, %s, %s, %s, %s, clock_timestamp())
            ON CONFLICT (cohort_key) DO NOTHING
            """,
            (
                COHORT_KEY,
                STRATEGY_NAME,
                STRATEGY_VERSION,
                contract_sha256,
                runtime_policy_sha256,
            ),
        )
        await cur.execute(
            """
            SELECT strategy_name, strategy_version, contract_sha256,
                   runtime_policy_sha256, cohort_started_at
            FROM app.research_cohort_registrations
            WHERE cohort_key = %s
            """,
            (COHORT_KEY,),
        )
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError(f"failed to register prospective cohort {COHORT_KEY!r}")
    expected = (STRATEGY_NAME, STRATEGY_VERSION, contract_sha256, runtime_policy_sha256)
    observed = (
        row["strategy_name"],
        row["strategy_version"],
        bytes(row["contract_sha256"]),
        bytes(row["runtime_policy_sha256"]),
    )
    if observed != expected:
        raise RuntimeError(
            f"prospective cohort {COHORT_KEY!r} is already registered under a different "
            "strategy contract/runtime policy"
        )
    started_at = row["cohort_started_at"]
    if not isinstance(started_at, datetime):
        raise RuntimeError(f"prospective cohort {COHORT_KEY!r} has an invalid start timestamp")
    return started_at


__all__ = ["COHORT_KEY", "register_prospective_cohort"]
