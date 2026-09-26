"""Durable one-read claims for registered research cohorts (migration 0054).

A cohort's formal read happens exactly once. The claim is inserted and committed
before any outcome is fetched, unique per (study, contract version, cohort start),
and records the candidate set the read used. A second run refuses instead of
writing a second verdict, even if late rows would change the prefix.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime


class FormalReadAlreadyClaimedError(RuntimeError):
    """This cohort's single formal read was already claimed."""


def candidate_ids_sha256(ids: Iterable[int]) -> str:
    return hashlib.sha256(json.dumps(sorted(ids), separators=(",", ":")).encode()).hexdigest()


async def claim_formal_read(
    db_url: str,
    *,
    study_id: str,
    contract_version: str,
    cohort_start: datetime,
    database_now: datetime,
    candidate_ids: list[int],
    code_revision: str,
    working_tree_dirty: bool,
) -> int:
    """Commit the claim; raise FormalReadAlreadyClaimedError if one exists."""
    import psycopg

    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        cur = await conn.execute(
            """
            INSERT INTO app.formal_read_claims (
                study_id, contract_version, cohort_start, database_now, candidate_count,
                candidate_ids_sha256, code_revision, working_tree_dirty
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT ON CONSTRAINT uq_formal_read_claim_cohort DO NOTHING
            RETURNING id
            """,
            (
                study_id,
                contract_version,
                cohort_start,
                database_now,
                len(candidate_ids),
                candidate_ids_sha256(candidate_ids),
                code_revision,
                working_tree_dirty,
            ),
        )
        row = await cur.fetchone()
    if row is None:
        raise FormalReadAlreadyClaimedError(
            f"formal read refused: {study_id} {contract_version} from {cohort_start.isoformat()} "
            "was already claimed; a registered cohort is read exactly once"
        )
    return int(row[0])
