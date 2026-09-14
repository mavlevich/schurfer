"""Orchestration for cold-bar gated deletion (design: docs/runbooks/
cold-bar-gated-deletion-design-v1.md). PR 1: everything needed to run the gate in
DRY-RUN -- gather evidence for each eligible day and compute a drop plan -- without
deleting anything. The actual ``drop_chunks`` is only ever issued when ``dry_run``
is False, which the CLI defaults to off.

All impure work (Borg extraction, Postgres/Timescale queries, reading receipt and
manifest files) is behind the ``Collectors`` protocol, so this orchestration -- the
part that decides what gets deleted -- is unit-tested with fakes, no Borg repo or
database required. The pure safety rules live in ``cold_bar_gated_deletion``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Protocol

from .cold_bar_gated_deletion import (
    DayEvidence,
    DropPlan,
    DropReceipt,
    is_eligible,
    plan_drops,
)

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path


# ---------- per-day receipt I/O (canonical: immutable JSON beside the manifest) ----------

RECEIPT_SUFFIX = ".offsite-receipt.json"


def receipt_path(receipts_dir: Path, day: str) -> Path:
    return receipts_dir / f"bars-{day}{RECEIPT_SUFFIX}"


def write_receipt(receipts_dir: Path, receipt: DropReceipt) -> Path:
    """Write a per-day receipt as immutable JSON. Refuses to overwrite: a receipt is
    a record of one archived export and must never be silently replaced (a changed
    day gets a new versioned export, not a rewritten receipt)."""
    path = receipt_path(receipts_dir, receipt.day)
    if path.exists():
        raise FileExistsError(f"receipt already exists and is immutable: {path}")
    receipts_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(receipt), indent=2, sort_keys=True) + "\n")
    return path


def read_receipt(receipts_dir: Path, day: str) -> DropReceipt | None:
    """Load a per-day receipt, or None if there is none for that day."""
    path = receipt_path(receipts_dir, day)
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    if payload.get("day") != day:
        raise ValueError(f"{path}: receipt day {payload.get('day')!r} does not match {day!r}")
    return DropReceipt(**payload)


# ---------- injectable impure boundary ----------


@dataclass(frozen=True)
class ChunkCandidate:
    """One day's chunk as reported by Timescale."""

    day: str
    range_start: datetime
    range_end: datetime


@dataclass(frozen=True)
class ExtractedOffsite:
    """What extracting a day's files from its named Borg archive yields."""

    parquet_sha256: str
    manifest_sha256: str
    file_fingerprint: str


class Collectors(Protocol):
    """The impure operations the runner needs; a real implementation wraps Borg and
    the database, a test supplies fakes."""

    def list_chunks(self) -> tuple[ChunkCandidate, ...]:
        """Every chunk currently in the source hypertable, with its time range."""

    def manifest_present(self, day: str) -> bool: ...

    def receipt_offsite_confirmed(self, day: str) -> bool:
        """Whether the day's receipt file is itself present in an offsite archive."""

    def archive_present(self, archive_name: str) -> bool: ...

    def extract_offsite(self, receipt: DropReceipt) -> ExtractedOffsite | None:
        """Extract the receipt's parquet+manifest from its named archive and return
        their recomputed shas and the parquet's recomputed file fingerprint; None if
        the files cannot be extracted."""

    def recompute_source_fingerprint(self, day: str) -> str | None:
        """Recompute the whole-row source fingerprint from the LIVE table now."""

    def drop_chunk(self, candidate: ChunkCandidate) -> None:
        """Targeted, per-chunk drop_chunks. Called ONLY for a cleared day when not dry-run."""


# ---------- orchestration ----------


def gather_evidence(
    candidate: ChunkCandidate,
    *,
    now: datetime,
    cutoff_days: int,
    receipts_dir: Path,
    collectors: Collectors,
) -> DayEvidence:
    """Assemble everything the pure gate needs for one candidate day. Anything that
    cannot be obtained becomes None/False, which the gate treats as a block."""
    receipt = read_receipt(receipts_dir, candidate.day)
    extracted = collectors.extract_offsite(receipt) if receipt is not None else None
    return DayEvidence(
        day=candidate.day,
        eligible=is_eligible(candidate.range_end, now, cutoff_days),
        manifest_present=collectors.manifest_present(candidate.day),
        receipt=receipt,
        receipt_offsite_confirmed=(
            collectors.receipt_offsite_confirmed(candidate.day) if receipt is not None else False
        ),
        archive_present=(
            collectors.archive_present(receipt.archive_name) if receipt is not None else False
        ),
        extracted_parquet_sha256=extracted.parquet_sha256 if extracted is not None else None,
        extracted_manifest_sha256=extracted.manifest_sha256 if extracted is not None else None,
        recomputed_fingerprint=collectors.recompute_source_fingerprint(candidate.day),
        recomputed_file_fingerprint=extracted.file_fingerprint if extracted is not None else None,
    )


@dataclass(frozen=True)
class RunResult:
    plan: DropPlan
    dropped: tuple[str, ...]  # days actually dropped (empty in dry-run)
    dry_run: bool


def run_gated_deletion(
    *,
    now: datetime,
    cutoff_days: int,
    receipts_dir: Path,
    collectors: Collectors,
    dry_run: bool = True,
) -> RunResult:
    """Compute the drop plan over eligible chunks (oldest first) and, only when
    ``dry_run`` is False, issue the targeted per-chunk drop for each cleared day.

    Eligibility filters candidates to those past the buffer; ``plan_drops`` then
    enforces strict ascending order and the contiguous validated prefix, so the set
    dropped is provably exactly the cleared oldest-first prefix.
    """
    by_day = {c.day: c for c in collectors.list_chunks()}
    eligible = sorted(
        (c for c in by_day.values() if is_eligible(c.range_end, now, cutoff_days)),
        key=lambda c: c.day,
    )
    evidence = tuple(
        gather_evidence(
            c, now=now, cutoff_days=cutoff_days, receipts_dir=receipts_dir, collectors=collectors
        )
        for c in eligible
    )
    plan = plan_drops(evidence)

    dropped: list[str] = []
    if not dry_run:
        for day in plan.to_drop:
            collectors.drop_chunk(by_day[day])
            dropped.append(day)
    return RunResult(plan=plan, dropped=tuple(dropped), dry_run=dry_run)


def render_plan(result: RunResult) -> str:
    """A human-readable report for the job log: what would be (or was) dropped, and
    why the frontier stopped."""
    lines = [f"gated-deletion run (dry_run={result.dry_run})"]
    verb = "DROPPED" if not result.dry_run else "would drop"
    if result.plan.to_drop:
        lines.append(
            f"  {verb} {len(result.plan.to_drop)} day(s): {', '.join(result.plan.to_drop)}"
        )
    else:
        lines.append(f"  {verb} 0 days")
    if result.plan.blocked_at is not None:
        day, reason = result.plan.blocked_at
        lines.append(f"  frontier halted at {day}: {reason}")
        if result.plan.held_after_block:
            lines.append(f"  held (untouched): {', '.join(result.plan.held_after_block)}")
    return "\n".join(lines)
