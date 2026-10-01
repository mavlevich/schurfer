"""HYP-012 v2 administrative stop: an outcome-blind accrual rule with a hard deadline.

Registered before any v2 return was read: docs/research/source-lead-forward-cohort-v2-
administrative-stop.md. The rule is a calendar governance policy. It decides whether the
passive v2 cohort may keep waiting for its registered checkpoint (100 resolved episodes
over 4 UTC weeks); it never evaluates the hypothesis, and a stop is not a `fail`.

At each checkpoint instant C (00:00 UTC) the rule counts the qualified v2 episodes
captured before C whose qualification is stamped before C, on a tradable venue, with
an exit bar closed by C (`episode_is_matured`). Only identities and timestamps are
read: no book, price, exit bar or return. Qualification rows are stamped with their
transaction's start, so that set is final only once no transaction that began before C
is still open; until then the checkpoint is not evaluated (`snapshot_not_final`) and
the run must be repeated. The cohort continues when the count reaches the frozen
minimum for C and is stopped otherwise. A run evaluates every checkpoint that is due, in
order, each on its own final snapshot, so a late or repeated run reaches the same
decision as an on-time one. The formal reader runs the same evaluation before it loads
any episode or claims, so a missed checkpoint can never be skipped by reading first.

The minimums come from one Poisson justification: at C the count k continues if
`k + U(k) / elapsed_days * remaining_days >= 100`, where U(k) is the one-sided 95% upper
bound on a Poisson mean given k events. That is an optimistic projection under a
model that clustered events, regime changes and outages can break. It is not a 95%
guarantee about future accrual, and repeated monthly checks carry no joint guarantee.
The frozen table, not the model, is the rule.

A stop is recorded once in `app.formal_read_claims` as `admin_stopped` (migration 0057).
It is insert-only: a formal read that already claimed the cohort wins and is never
rewritten, and a stopped cohort refuses every later read before any quote is loaded.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .formal_read_claims import (
    STATUS_ADMIN_STOPPED,
    candidate_ids_sha256,
    existing_claim,
    record_administrative_stop,
)
from .reporting import normalize_code_revision
from .source_lead_forward_cohort import (
    COHORT_CAPTURE_DEADLINE,
    CONTRACT_VERSION,
    EVIDENCE_FLOOR,
    EXIT_BAR_TIMEFRAME_MS,
    QUALIFICATION_VERSION,
    SOURCE_LEAD_FORWARD_COHORT_START,
    TRADABLE_VENUES,
    episode_is_matured,
    expected_exit_boundary_ms,
)
from .source_lead_forward_cohort_repository import (
    ACCRUAL_SNAPSHOT_QUERY_VERSION,
    AccrualSnapshotRow,
    SourceLeadForwardCohortRepository,
)
from .source_lead_multi_source_report import complete_digest, load_verified, write_once

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .formal_read_claims import FormalReadClaim

RULE_VERSION = "hyp012_v2_administrative_stop_v1"
STUDY_ID = "HYP-012"
ARTIFACT_DIR = Path("/runtime/research/hyp012-v2-administrative-stop")
MAX_SNAPSHOT_ROWS = 20_000
UPPER_BOUND_CONFIDENCE = 0.95
# The formal reader may record the deadline stop only after every capture before the
# deadline has had time to qualify and mature.
DEADLINE_SETTLEMENT = timedelta(hours=24)

# Frozen governance table: checkpoint (00:00 UTC) -> minimum matured qualified episodes
# needed to continue. The deadline requires the full resolved floor.
MIN_QUALIFIED_TO_CONTINUE: dict[datetime, int] = {
    datetime(2026, 10, 31, tzinfo=UTC): 12,
    datetime(2026, 11, 30, tzinfo=UTC): 28,
    datetime(2026, 12, 31, tzinfo=UTC): 45,
    datetime(2027, 1, 31, tzinfo=UTC): 64,
    datetime(2027, 2, 28, tzinfo=UTC): 81,
    COHORT_CAPTURE_DEADLINE: 100,
}
CHECKPOINTS: tuple[datetime, ...] = tuple(sorted(MIN_QUALIFIED_TO_CONTINUE))

DECISION_CONTINUE = "continue"
DECISION_STOP = "stop"
DEADLINE_CHECKPOINT_UNREACHED = "checkpoint_unreached_at_deadline"


class AdministrativeStopIntegrityError(ValueError):
    """A stored decision artifact disagrees with its replay from the same snapshot."""


def _poisson_cdf(k: int, mean: float) -> float:
    term = math.exp(-mean)
    total = 0.0
    for i in range(k + 1):
        total += term
        term *= mean / (i + 1)
    return total


def poisson_upper_mean(k: int, confidence: float = UPPER_BOUND_CONFIDENCE) -> float:
    """One-sided upper confidence bound on a Poisson mean after observing k events."""
    if k < 0 or not 0 < confidence < 1:
        raise ValueError("invalid Poisson bound request")
    lower, upper = 0.0, float(k) + 50.0
    for _ in range(200):
        middle = (lower + upper) / 2
        if _poisson_cdf(k, middle) > 1 - confidence:
            lower = middle
        else:
            upper = middle
    return upper


def justified_minimum(checkpoint: datetime) -> int:
    """The Poisson justification of the frozen table (never the rule itself)."""
    floor = EVIDENCE_FLOOR["min_resolved_episodes"]
    elapsed = (checkpoint - SOURCE_LEAD_FORWARD_COHORT_START).total_seconds() / 86_400
    remaining = (COHORT_CAPTURE_DEADLINE - checkpoint).total_seconds() / 86_400
    if elapsed <= 0 or remaining < 0:
        raise ValueError("checkpoint outside the cohort")
    return next(
        k for k in range(floor + 1) if k + poisson_upper_mean(k) / elapsed * remaining >= floor
    )


@dataclass(frozen=True)
class ClosedWindow:
    """The data a closed episode's outcome depends on: its asset's prices, on any
    venue, from entry to the close of its exit bar. Later studies exclude every
    feature, label or outcome window that intersects it."""

    capture_id: int
    canonical_asset_id: str
    start: datetime
    end: datetime


def closed_window(capture_id: int, canonical_asset_id: str, entry: datetime) -> ClosedWindow:
    end_ms = expected_exit_boundary_ms(entry) + EXIT_BAR_TIMEFRAME_MS
    return ClosedWindow(
        capture_id, canonical_asset_id, entry, datetime.fromtimestamp(end_ms / 1000, UTC)
    )


def windows_payload(windows: Sequence[ClosedWindow]) -> list[dict[str, Any]]:
    return [
        {
            "capture_id": w.capture_id,
            "canonical_asset_id": w.canonical_asset_id,
            "start": w.start.isoformat(),
            "end": w.end.isoformat(),
        }
        for w in windows
    ]


@dataclass(frozen=True)
class CheckpointDecision:
    checkpoint: datetime
    qualified_in_snapshot: int
    matured_tradable: int
    min_to_continue: int
    decision: str
    closed_capture_ids: tuple[int, ...]
    closed_windows: tuple[ClosedWindow, ...] = ()


def decide(checkpoint: datetime, snapshot: Sequence[AccrualSnapshotRow]) -> CheckpointDecision:
    """Pure: the registered decision for one checkpoint from its price-free snapshot."""
    if checkpoint not in MIN_QUALIFIED_TO_CONTINUE:
        raise ValueError(f"{checkpoint.isoformat()} is not a registered checkpoint")
    for row in snapshot:
        if not (SOURCE_LEAD_FORWARD_COHORT_START <= row.source_first_observed_at < checkpoint):
            raise ValueError(f"capture {row.capture_id} lies outside the checkpoint snapshot")
        if row.target_exchange not in TRADABLE_VENUES:
            raise ValueError(
                f"capture {row.capture_id} is qualified on non-tradable {row.target_exchange}; "
                "the v2 reader refuses such a cohort, so the rule refuses too"
            )
    matured = sum(episode_is_matured(row.observed_at, checkpoint) for row in snapshot)
    minimum = MIN_QUALIFIED_TO_CONTINUE[checkpoint]
    stop = matured < minimum
    return CheckpointDecision(
        checkpoint=checkpoint,
        qualified_in_snapshot=len(snapshot),
        matured_tradable=matured,
        min_to_continue=minimum,
        decision=DECISION_STOP if stop else DECISION_CONTINUE,
        closed_capture_ids=tuple(row.capture_id for row in snapshot) if stop else (),
        closed_windows=tuple(
            closed_window(row.capture_id, row.canonical_asset_id, row.observed_at)
            for row in snapshot
        )
        if stop
        else (),
    )


def terminal_reason(decision: CheckpointDecision) -> str:
    return f"accrual_below_rule:{decision.checkpoint:%Y-%m-%d}"


def decision_payload(decision: CheckpointDecision) -> dict[str, Any]:
    """The replayable part of a decision artifact (no run-specific fields)."""
    return {
        "rule_version": RULE_VERSION,
        "study_id": STUDY_ID,
        "contract_version": CONTRACT_VERSION,
        "cohort_start": SOURCE_LEAD_FORWARD_COHORT_START.isoformat(),
        "capture_deadline": COHORT_CAPTURE_DEADLINE.isoformat(),
        "qualification_version": QUALIFICATION_VERSION,
        "snapshot_query_version": ACCRUAL_SNAPSHOT_QUERY_VERSION,
        "checkpoint": decision.checkpoint.isoformat(),
        "qualified_in_snapshot": decision.qualified_in_snapshot,
        "matured_tradable": decision.matured_tradable,
        "min_to_continue": decision.min_to_continue,
        "decision": decision.decision,
        "closed_capture_ids": list(decision.closed_capture_ids),
        "closed_capture_ids_sha256": candidate_ids_sha256(decision.closed_capture_ids),
        "closed_windows": windows_payload(decision.closed_windows),
    }


_RUN_FIELDS = ("code_revision", "working_tree_dirty", "evaluated_at")


def persist_decision(
    artifact_dir: Path,
    name: str,
    payload: dict[str, Any],
    *,
    code_revision: str,
    working_tree_dirty: bool,
    evaluated_at: datetime,
) -> str:
    """Write the decision once, or verify the stored one replays identically; returns
    the stored artifact's SHA-256."""
    path = artifact_dir / name
    if path.exists():
        complete_digest(path)
        stored, digest = load_verified(path)
        replayable = {k: v for k, v in stored.items() if k not in _RUN_FIELDS}
        if replayable != payload:
            raise AdministrativeStopIntegrityError(
                f"{path} differs from its replay from the same snapshot; refusing"
            )
        return digest
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return write_once(
        path,
        {
            **payload,
            "code_revision": normalize_code_revision(code_revision),
            "working_tree_dirty": working_tree_dirty,
            "evaluated_at": evaluated_at.isoformat(),
        },
    )


@dataclass(frozen=True)
class RunOutcome:
    status: str
    decisions: tuple[CheckpointDecision, ...]
    detail: str


SNAPSHOT_NOT_FINAL = "snapshot_not_final"


async def evaluate_checkpoints(
    repository: SourceLeadForwardCohortRepository,
    db_url: str,
    prior: FormalReadClaim | None,
    *,
    artifact_dir: Path,
    code_revision: str,
    working_tree_dirty: bool,
) -> RunOutcome:
    """Evaluate every due checkpoint in order, each on its final snapshot, and record
    the first stop. `prior` is the cohort's claim row as the caller read it."""
    if prior is not None and prior.status != STATUS_ADMIN_STOPPED:
        return RunOutcome(
            "formal_read_started",
            (),
            f"the formal read is {prior.status}; the administrative rule no longer applies",
        )
    now = await repository.database_now()
    decisions: list[CheckpointDecision] = []
    for checkpoint in CHECKPOINTS:
        if checkpoint > now:
            break
        open_transactions = await repository.open_transactions_started_before(checkpoint)
        if not open_transactions.snapshot_final:
            return RunOutcome(
                SNAPSHOT_NOT_FINAL,
                tuple(decisions),
                f"{checkpoint:%Y-%m-%d}: {open_transactions.started_before} transaction(s) "
                f"begun before the checkpoint are still open and "
                f"{open_transactions.unverifiable} session(s) cannot be inspected; "
                "nothing was decided, run again later",
            )
        snapshot = await repository.fetch_accrual_snapshot(
            qualification_version=QUALIFICATION_VERSION,
            since=SOURCE_LEAD_FORWARD_COHORT_START,
            as_of=checkpoint,
            limit=MAX_SNAPSHOT_ROWS + 1,
        )
        if len(snapshot) > MAX_SNAPSHOT_ROWS:
            raise ValueError("accrual snapshot row limit exceeded")
        decision = decide(checkpoint, snapshot)
        decisions.append(decision)
        fingerprint = persist_decision(
            artifact_dir,
            f"checkpoint-{checkpoint:%Y-%m-%d}.json",
            decision_payload(decision),
            code_revision=code_revision,
            working_tree_dirty=working_tree_dirty,
            evaluated_at=now,
        )
        if decision.decision == DECISION_STOP:
            stop = await record_administrative_stop(
                db_url,
                study_id=STUDY_ID,
                contract_version=CONTRACT_VERSION,
                cohort_start=SOURCE_LEAD_FORWARD_COHORT_START,
                database_now=now,
                closed_ids=list(decision.closed_capture_ids),
                terminal_reason=terminal_reason(decision),
                result_fingerprint=fingerprint,
                code_revision=normalize_code_revision(code_revision),
                working_tree_dirty=working_tree_dirty,
            )
            status = "stopped" if stop.newly_recorded else "already_stopped"
            return RunOutcome(status, tuple(decisions), terminal_reason(decision))
    if prior is not None:
        if now < COHORT_CAPTURE_DEADLINE:
            raise AdministrativeStopIntegrityError(
                "the cohort is recorded as stopped, but no due checkpoint replays a stop"
            )
        return RunOutcome(
            "already_stopped",
            tuple(decisions),
            "stopped by the formal reader: checkpoint unreached among pre-deadline captures",
        )
    return RunOutcome(DECISION_CONTINUE, tuple(decisions), "no due checkpoint stops the cohort")


async def evaluate_due_checkpoints(
    repository: SourceLeadForwardCohortRepository,
    db_url: str,
    *,
    artifact_dir: Path,
    code_revision: str,
    working_tree_dirty: bool,
) -> RunOutcome:
    """The CLI entry: read the cohort's claim row, then evaluate."""
    prior = await existing_claim(
        db_url,
        study_id=STUDY_ID,
        contract_version=CONTRACT_VERSION,
        cohort_start=SOURCE_LEAD_FORWARD_COHORT_START,
    )
    return await evaluate_checkpoints(
        repository,
        db_url,
        prior,
        artifact_dir=artifact_dir,
        code_revision=code_revision,
        working_tree_dirty=working_tree_dirty,
    )


def deadline_payload(windows: Sequence[ClosedWindow], matured: int) -> dict[str, Any]:
    """The reader's deadline stop: the capture window closed and the registered
    checkpoint was not reached among its captures (decided without any return)."""
    return {
        "rule_version": RULE_VERSION,
        "study_id": STUDY_ID,
        "contract_version": CONTRACT_VERSION,
        "cohort_start": SOURCE_LEAD_FORWARD_COHORT_START.isoformat(),
        "capture_deadline": COHORT_CAPTURE_DEADLINE.isoformat(),
        "qualification_version": QUALIFICATION_VERSION,
        "decision": DECISION_STOP,
        "reason": DEADLINE_CHECKPOINT_UNREACHED,
        "matured_qualified": matured,
        "closed_capture_ids": [w.capture_id for w in windows],
        "closed_capture_ids_sha256": candidate_ids_sha256([w.capture_id for w in windows]),
        "closed_windows": windows_payload(windows),
    }


def deadline_stop_due(database_now: datetime) -> bool:
    return database_now >= COHORT_CAPTURE_DEADLINE + DEADLINE_SETTLEMENT


def render(outcome: RunOutcome) -> str:
    lines = [f"HYP-012 v2 administrative rule ({RULE_VERSION}): {outcome.status}"]
    lines.append(outcome.detail)
    for d in outcome.decisions:
        lines.append(
            f"- {d.checkpoint:%Y-%m-%d}: matured qualified {d.matured_tradable} "
            f"(snapshot {d.qualified_in_snapshot}), minimum {d.min_to_continue}: {d.decision}"
        )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--clean-tree", action="store_true")
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required")

    async def run() -> RunOutcome:
        repository = SourceLeadForwardCohortRepository.from_url(db_url)
        try:
            return await evaluate_due_checkpoints(
                repository,
                db_url,
                artifact_dir=args.artifact_dir,
                code_revision=args.code_revision,
                working_tree_dirty=not args.clean_tree,
            )
        finally:
            await repository.close()

    outcome = asyncio.run(run())
    sys.stdout.write(render(outcome))
    sys.stdout.write(json.dumps({"status": outcome.status}) + "\n")


if __name__ == "__main__":
    main()
