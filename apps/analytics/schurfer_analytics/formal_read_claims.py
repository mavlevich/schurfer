"""Durable one-read claims for registered research cohorts (migration 0054).

Protocol, per cohort (study, contract version, cohort start):

1. The caller locates the registered checkpoint WITHOUT computing any return and
   only then opens a claim, which stores the exact ordered candidate ids of that
   checkpoint prefix.
2. The verdict is computed on those ids only.
3. `complete_claim` marks the claim `completed` with the artifact fingerprint.

A run that fails between 1 and 3 resumes the SAME claim on the stored ids (never a
new, independent prefix). A completed claim refuses every later run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime


class FormalReadAlreadyClaimedError(RuntimeError):
    """This cohort's single formal read is already completed."""


@dataclass(frozen=True)
class FormalReadClaim:
    id: int
    candidate_ids: tuple[int, ...]
    status: str
    resumed: bool


def candidate_ids_sha256(ids: Iterable[int]) -> str:
    return hashlib.sha256(json.dumps(sorted(ids), separators=(",", ":")).encode()).hexdigest()


_SELECT = """
SELECT id, candidate_ids, status FROM app.formal_read_claims
WHERE study_id = %s AND contract_version = %s AND cohort_start = %s
"""


def _claim(row: tuple[Any, ...], *, resumed: bool) -> FormalReadClaim:
    ids = row[1] if isinstance(row[1], list) else json.loads(row[1])
    return FormalReadClaim(
        id=int(row[0]),
        candidate_ids=tuple(int(i) for i in ids),
        status=str(row[2]),
        resumed=resumed,
    )


async def existing_claim(
    db_url: str, *, study_id: str, contract_version: str, cohort_start: datetime
) -> FormalReadClaim | None:
    import psycopg

    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        cur = await conn.execute(_SELECT, (study_id, contract_version, cohort_start))
        row = await cur.fetchone()
    return _claim(row, resumed=True) if row else None


async def open_claim(
    db_url: str,
    *,
    study_id: str,
    contract_version: str,
    cohort_start: datetime,
    database_now: datetime,
    candidate_ids: list[int],
    code_revision: str,
    working_tree_dirty: bool,
) -> FormalReadClaim:
    """Insert the claim, or return the existing open claim (resume). A
    completed claim raises FormalReadAlreadyClaimedError."""
    import psycopg
    from psycopg.types.json import Jsonb

    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        cur = await conn.execute(
            """
            INSERT INTO app.formal_read_claims (
                study_id, contract_version, cohort_start, database_now, candidate_count,
                candidate_ids, candidate_ids_sha256, code_revision, working_tree_dirty
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT ON CONSTRAINT uq_formal_read_claim_cohort DO NOTHING
            RETURNING id, candidate_ids, status
            """,
            (
                study_id,
                contract_version,
                cohort_start,
                database_now,
                len(candidate_ids),
                Jsonb(list(candidate_ids)),
                candidate_ids_sha256(candidate_ids),
                code_revision,
                working_tree_dirty,
            ),
        )
        row = await cur.fetchone()
        if row is not None:
            return _claim(row, resumed=False)
        cur = await conn.execute(_SELECT, (study_id, contract_version, cohort_start))
        existing = await cur.fetchone()
    if existing is None:  # pragma: no cover - the conflict implies a row
        raise RuntimeError("claim conflict without a row")
    claim = _claim(existing, resumed=True)
    if claim.status == "completed":
        raise FormalReadAlreadyClaimedError(
            f"formal read refused: {study_id} {contract_version} from "
            f"{cohort_start.isoformat()} is already completed; a cohort is read exactly once"
        )
    return claim


async def complete_claim(db_url: str, claim_id: int, result_fingerprint: str) -> None:
    import psycopg

    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        cur = await conn.execute(
            """
            UPDATE app.formal_read_claims
            SET status = 'completed', completed_at = now(), result_fingerprint = %s
            WHERE id = %s AND status = 'claimed'
            RETURNING id
            """,
            (result_fingerprint, claim_id),
        )
        if await cur.fetchone() is None:
            raise FormalReadAlreadyClaimedError(f"claim {claim_id} is not open")
