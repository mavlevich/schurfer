"""Durable one-read claims for registered research cohorts (migration 0054).

Protocol, per cohort (study, contract version, cohort start):

1. The caller locates the registered checkpoint WITHOUT computing any return and
   only then opens a claim, which stores the exact ordered candidate ids of that
   checkpoint prefix.
2. The verdict is computed on those ids only.
3. `complete_claim` marks the claim `completed` with the artifact fingerprint.

Only one run computes at a time: a claim carries a lease owner and expiry
(`LEASE`, longer than the reader's 20-minute exchange-fetch budget). Another run
may resume the SAME claim on the stored ids (never a new, independent prefix) only
after the lease has expired, taking it over atomically; `complete_claim` succeeds
only for the current owner. A completed claim refuses every later run.

A registered administrative rule may instead close the cohort without a read
(`record_administrative_stop`, migration 0057). The stop is inserted under the same
unique key, so a stop and a claim exclude each other atomically: whichever row is
inserted first wins, a stop never rewrites a claim, and a stopped cohort refuses
every later claim.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime


LEASE = timedelta(minutes=60)


class FormalReadAlreadyClaimedError(RuntimeError):
    """This cohort's single formal read is already completed."""


class FormalReadLeaseHeldError(RuntimeError):
    """Another run holds the claim's lease; wait for it to finish or expire."""


class FormalReadAdministrativelyStoppedError(RuntimeError):
    """A registered administrative rule closed this cohort; it is never read."""


STATUS_ADMIN_STOPPED = "admin_stopped"
ADMIN_STOP_LEASE_OWNER = "administrative_stop"


def stopped_error(study_id: str, contract_version: str) -> FormalReadAdministrativelyStoppedError:
    return FormalReadAdministrativelyStoppedError(
        f"formal read refused: {study_id} {contract_version} was closed by its registered "
        "administrative stop; its episodes are never read"
    )


@dataclass(frozen=True)
class FormalReadClaim:
    id: int
    candidate_ids: tuple[int, ...]
    status: str
    resumed: bool
    owner: str = ""


def candidate_ids_sha256(ids: Iterable[int]) -> str:
    return hashlib.sha256(json.dumps(sorted(ids), separators=(",", ":")).encode()).hexdigest()


_SELECT = """
SELECT id, candidate_ids, status FROM app.formal_read_claims
WHERE study_id = %s AND contract_version = %s AND cohort_start = %s
"""


def _claim(row: tuple[Any, ...], *, resumed: bool, owner: str = "") -> FormalReadClaim:
    ids = row[1] if isinstance(row[1], list) else json.loads(row[1])
    return FormalReadClaim(
        id=int(row[0]),
        candidate_ids=tuple(int(i) for i in ids),
        status=str(row[2]),
        resumed=resumed,
        owner=owner,
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
    """Insert the claim and own its lease, or take over an existing open claim
    whose lease has expired (resume on its stored ids). Raises
    FormalReadLeaseHeldError while another run holds the lease and
    FormalReadAlreadyClaimedError once the read is completed."""
    import psycopg
    from psycopg.types.json import Jsonb

    owner = str(uuid.uuid4())
    lease_seconds = LEASE.total_seconds()
    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        cur = await conn.execute(
            """
            INSERT INTO app.formal_read_claims (
                study_id, contract_version, cohort_start, database_now, candidate_count,
                candidate_ids, candidate_ids_sha256, code_revision, working_tree_dirty,
                lease_owner, lease_expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      now() + make_interval(secs => %s))
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
                owner,
                lease_seconds,
            ),
        )
        row = await cur.fetchone()
        if row is not None:
            return _claim(row, resumed=False, owner=owner)
        # Take over only an OPEN claim whose lease has expired, atomically.
        cur = await conn.execute(
            """
            UPDATE app.formal_read_claims
            SET lease_owner = %s, lease_expires_at = now() + make_interval(secs => %s)
            WHERE study_id = %s AND contract_version = %s AND cohort_start = %s
              AND status = 'claimed' AND lease_expires_at < now()
            RETURNING id, candidate_ids, status
            """,
            (owner, lease_seconds, study_id, contract_version, cohort_start),
        )
        taken = await cur.fetchone()
        if taken is not None:
            return _claim(taken, resumed=True, owner=owner)
        cur = await conn.execute(_SELECT, (study_id, contract_version, cohort_start))
        existing = await cur.fetchone()
    if existing is not None and str(existing[2]) == STATUS_ADMIN_STOPPED:
        raise stopped_error(study_id, contract_version)
    if existing is not None and str(existing[2]) == "completed":
        raise FormalReadAlreadyClaimedError(
            f"formal read refused: {study_id} {contract_version} from "
            f"{cohort_start.isoformat()} is already completed; a cohort is read exactly once"
        )
    raise FormalReadLeaseHeldError(
        f"formal read refused: another run holds the lease on {study_id} {contract_version}; "
        f"it resumes only after that run finishes or its lease ({LEASE}) expires"
    )


async def complete_claim(
    db_url: str, claim_id: int, result_fingerprint: str, *, owner: str
) -> None:
    """Only the run that currently owns the lease completes the claim."""
    import psycopg

    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        cur = await conn.execute(
            """
            UPDATE app.formal_read_claims
            SET status = 'completed', completed_at = now(), result_fingerprint = %s
            WHERE id = %s AND status = 'claimed' AND lease_owner = %s
            RETURNING id
            """,
            (result_fingerprint, claim_id, owner),
        )
        if await cur.fetchone() is None:
            raise FormalReadAlreadyClaimedError(
                f"claim {claim_id} is not open or this run no longer owns its lease"
            )


@dataclass(frozen=True)
class AdministrativeStop:
    id: int
    candidate_ids: tuple[int, ...]
    terminal_reason: str
    result_fingerprint: str
    newly_recorded: bool


async def record_administrative_stop(
    db_url: str,
    *,
    study_id: str,
    contract_version: str,
    cohort_start: datetime,
    database_now: datetime,
    closed_ids: list[int],
    terminal_reason: str,
    result_fingerprint: str,
    code_revision: str,
    working_tree_dirty: bool,
) -> AdministrativeStop:
    """Close the cohort without a read, or confirm the identical stop already recorded.

    Insert-only: if a formal read already claimed (or completed) this cohort, the stop
    is refused and the claim is left untouched. A different stop already recorded is
    refused too, so a rerun can never move the closing checkpoint."""
    import psycopg
    from psycopg.types.json import Jsonb

    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        cur = await conn.execute(
            """
            INSERT INTO app.formal_read_claims (
                study_id, contract_version, cohort_start, database_now, candidate_count,
                candidate_ids, candidate_ids_sha256, code_revision, working_tree_dirty,
                status, lease_owner, lease_expires_at, completed_at, result_fingerprint,
                terminal_reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now(), %s, %s)
            ON CONFLICT ON CONSTRAINT uq_formal_read_claim_cohort DO NOTHING
            RETURNING id
            """,
            (
                study_id,
                contract_version,
                cohort_start,
                database_now,
                len(closed_ids),
                Jsonb(list(closed_ids)),
                candidate_ids_sha256(closed_ids),
                code_revision,
                working_tree_dirty,
                STATUS_ADMIN_STOPPED,
                ADMIN_STOP_LEASE_OWNER,
                result_fingerprint,
                terminal_reason,
            ),
        )
        inserted = await cur.fetchone()
        if inserted is not None:
            return AdministrativeStop(
                int(inserted[0]),
                tuple(closed_ids),
                terminal_reason,
                result_fingerprint,
                newly_recorded=True,
            )
        cur = await conn.execute(
            """
            SELECT id, candidate_ids, status, terminal_reason, result_fingerprint
            FROM app.formal_read_claims
            WHERE study_id = %s AND contract_version = %s AND cohort_start = %s
            """,
            (study_id, contract_version, cohort_start),
        )
        row = await cur.fetchone()
    if row is None:  # pragma: no cover - the conflicting row cannot vanish (no deletes)
        raise RuntimeError("administrative stop conflicted with a row that is gone")
    if str(row[2]) != STATUS_ADMIN_STOPPED:
        raise FormalReadAlreadyClaimedError(
            f"administrative stop refused: the formal read of {study_id} {contract_version} "
            f"is already {row[2]}; a started or completed read is never rewritten"
        )
    if row[3] != terminal_reason or row[4] != result_fingerprint:
        raise ValueError(
            f"administrative stop refused: {study_id} {contract_version} is already stopped "
            f"as {row[3]!r} with another artifact; the closing checkpoint never moves"
        )
    stored = _claim((row[0], row[1], row[2]), resumed=True)
    return AdministrativeStop(
        stored.id, stored.candidate_ids, terminal_reason, result_fingerprint, newly_recorded=False
    )
