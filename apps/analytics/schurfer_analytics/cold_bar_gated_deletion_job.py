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
import os
from dataclasses import asdict, dataclass
from datetime import UTC, date, timedelta
from typing import TYPE_CHECKING, Protocol

from .cold_bar_gated_deletion import (
    DROP,
    DayEvidence,
    DropReceipt,
    drop_decision,
    is_eligible,
    is_single_utc_day,
)

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path


# ---------- per-day receipt I/O (canonical: immutable JSON beside the manifest) ----------

RECEIPT_SUFFIX = ".offsite-receipt.json"

# Files are archived under the backed-up directory's path, so an archive member is
# `runtime/cold-bars/bars-<day>.<ext>` (confirmed against a real archive), not the bare
# filename. Defined here so both the receipt writer and the collector agree.
ARCHIVE_MEMBER_PREFIX = "runtime/cold-bars/"


def receipt_path(receipts_dir: Path, day: str) -> Path:
    return receipts_dir / f"bars-{day}{RECEIPT_SUFFIX}"


def write_receipt(receipts_dir: Path, receipt: DropReceipt) -> Path:
    """Write a per-day receipt as immutable JSON. ATOMICALLY refuses to overwrite:
    the file is created with O_CREAT|O_EXCL so two concurrent writers cannot both
    pass a check-then-write and clobber a receipt (a changed day gets a new versioned
    export, never a rewritten receipt)."""
    receipts_dir.mkdir(parents=True, exist_ok=True)
    path = receipt_path(receipts_dir, receipt.day)
    payload = json.dumps(asdict(receipt), indent=2, sort_keys=True) + "\n"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise FileExistsError(f"receipt already exists and is immutable: {path}") from None
    with os.fdopen(fd, "w") as handle:
        handle.write(payload)
    return path


def write_receipts_for_archive(cold_bars_dir: Path, archive_name: str) -> list[str]:
    """Write a per-day receipt for each day whose Parquet is still LOCAL (so it is in
    the archive just created) and whose manifest carries fingerprints. Returns the days
    written.

    Must run inside the offsite backup, AFTER the bars archive is verified and BEFORE
    the local Parquet is reclaimed: a local Parquet is exactly a day contained in the
    archive named here. A manifest without fingerprints (exported before they were
    enabled) is skipped -- that day stays non-droppable until it is re-exported. An
    existing receipt is immutable, so a re-run does not overwrite it.
    """
    from .cold_bar_export import sha256_file  # local import: keeps the export dep lazy

    written: list[str] = []
    for parquet in sorted(cold_bars_dir.glob("bars-*.parquet")):
        day = parquet.name[len("bars-") : -len(".parquet")]
        manifest_path = cold_bars_dir / f"bars-{day}.manifest.json"
        if not manifest_path.exists():
            continue
        m = json.loads(manifest_path.read_text())
        if (
            not (m.get("source_fingerprint") and m.get("file_fingerprint"))
            or m.get("fidelity_verified") is None
        ):
            continue  # fingerprint-less day -> cannot make a complete receipt; skip (fail-closed)
        receipt = DropReceipt(
            day=day,
            archive_name=archive_name,
            parquet_path=f"{ARCHIVE_MEMBER_PREFIX}bars-{day}.parquet",
            parquet_sha256=m["sha256"],
            manifest_sha256=sha256_file(manifest_path),
            row_count=m["row_count"],
            schema_version=m["schema_version"],
            source_fingerprint=m["source_fingerprint"],
            file_fingerprint=m["file_fingerprint"],
            fidelity_verified=m["fidelity_verified"],
        )
        try:
            write_receipt(cold_bars_dir, receipt)
            written.append(day)
        except FileExistsError:
            pass  # already recorded; receipts are immutable
    return written


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
    cannot be obtained becomes None/False, which the gate treats as a block. Any
    EXCEPTION while gathering (bad receipt JSON, a Borg or DB error) is caught and
    turned into a blocked evidence for THIS day, so one bad day never crashes the run."""
    eligible = is_eligible(candidate.range_end, now, cutoff_days)
    try:
        receipt = read_receipt(receipts_dir, candidate.day)
        extracted = collectors.extract_offsite(receipt) if receipt is not None else None
        return DayEvidence(
            day=candidate.day,
            eligible=eligible,
            manifest_present=collectors.manifest_present(candidate.day),
            receipt=receipt,
            receipt_offsite_confirmed=(
                collectors.receipt_offsite_confirmed(candidate.day)
                if receipt is not None
                else False
            ),
            archive_present=(
                collectors.archive_present(receipt.archive_name) if receipt is not None else False
            ),
            extracted_parquet_sha256=extracted.parquet_sha256 if extracted is not None else None,
            extracted_manifest_sha256=extracted.manifest_sha256 if extracted is not None else None,
            recomputed_fingerprint=collectors.recompute_source_fingerprint(candidate.day),
            recomputed_file_fingerprint=(
                extracted.file_fingerprint if extracted is not None else None
            ),
        )
    except Exception as exc:  # any gather failure must block this day, not crash the run
        return DayEvidence(
            day=candidate.day,
            eligible=eligible,
            manifest_present=False,
            receipt=None,
            receipt_offsite_confirmed=False,
            archive_present=False,
            extracted_parquet_sha256=None,
            extracted_manifest_sha256=None,
            recomputed_fingerprint=None,
            recomputed_file_fingerprint=None,
            gather_error=f"{type(exc).__name__}: {exc}",
        )


def validate_chunks(chunks: tuple[ChunkCandidate, ...]) -> dict[str, ChunkCandidate]:
    """Cheap (metadata-only) fail-closed validation before any expensive evidence
    work: every chunk must be exactly one UTC day whose label matches its range, and
    no two chunks may claim the same day. A malformed or duplicated chunk means the
    hypertable's shape is not what this tool assumes (config drift), so we REFUSE the
    whole run rather than guess -- a deletion tool must never proceed while confused."""
    by_day: dict[str, ChunkCandidate] = {}
    for c in chunks:
        if not is_single_utc_day(c.range_start, c.range_end):
            raise ValueError(
                f"chunk {c.day!r} is not a single UTC day "
                f"(range {c.range_start.isoformat()}..{c.range_end.isoformat()}); "
                "refusing (Timescale chunk_time_interval drift?)"
            )
        if c.day != c.range_start.astimezone(UTC).date().isoformat():
            raise ValueError(f"chunk day {c.day!r} does not match range_start {c.range_start}")
        if c.day in by_day:
            raise ValueError(f"two chunks claim the same day {c.day!r}; refusing")
        by_day[c.day] = c
    return by_day


@dataclass(frozen=True)
class RunResult:
    dropped: tuple[str, ...]  # days actually dropped (empty in dry-run)
    dry_run: bool
    n_chunks: int  # total chunks the source reported
    n_eligible: int  # chunks past the retention buffer
    verdicts: tuple[tuple[str, str, str], ...]  # (day, decision, reason) actually evaluated
    to_drop: tuple[str, ...]  # cleared oldest-first prefix
    blocked_at: tuple[str, str] | None  # (day, reason) that halted the frontier
    held: tuple[str, ...]  # eligible days after the halt, not evaluated
    bounded_at: int | None  # if evaluation stopped early on max_eval_days


def run_gated_deletion(
    *,
    now: datetime,
    cutoff_days: int,
    receipts_dir: Path,
    collectors: Collectors,
    dry_run: bool = True,
    max_eval_days: int | None = None,
) -> RunResult:
    """Validate chunks cheaply, then evaluate eligible days oldest-first LAZILY --
    gathering the expensive per-day evidence (Borg extract + fingerprint) only as far
    as the frontier can advance, stopping at the first block. This bounds the work to
    the cleared prefix plus one blocking day (plus ``max_eval_days`` for reconciliation
    diagnostics), and records a per-day verdict for everything actually evaluated,
    instead of extracting every eligible day and reporting only the first block.

    The per-chunk targeted drop is issued only when ``dry_run`` is False.
    """
    by_day = validate_chunks(collectors.list_chunks())
    eligible = sorted(
        (c for c in by_day.values() if is_eligible(c.range_end, now, cutoff_days)),
        key=lambda c: c.day,
    )

    verdicts: list[tuple[str, str, str]] = []
    to_drop: list[str] = []
    dropped: list[str] = []
    blocked_at: tuple[str, str] | None = None
    bounded_at: int | None = None
    prev: date | None = None
    stop_idx = len(eligible)

    for idx, c in enumerate(eligible):
        if max_eval_days is not None and idx >= max_eval_days:
            bounded_at = max_eval_days
            stop_idx = idx
            break
        cur = date.fromisoformat(c.day)
        if prev is not None and cur != prev + timedelta(days=1):
            blocked_at = (c.day, f"calendar gap after {prev.isoformat()}")
            stop_idx = idx
            break
        ev = gather_evidence(
            c, now=now, cutoff_days=cutoff_days, receipts_dir=receipts_dir, collectors=collectors
        )
        decision, reason = drop_decision(ev)
        verdicts.append((c.day, decision, reason))
        if decision == DROP:
            to_drop.append(c.day)
            prev = cur
            if not dry_run:
                collectors.drop_chunk(by_day[c.day])
                dropped.append(c.day)
        else:
            blocked_at = (c.day, reason)
            stop_idx = idx + 1  # the blocking day was evaluated
            break

    held = tuple(c.day for c in eligible[stop_idx:])
    return RunResult(
        dropped=tuple(dropped),
        dry_run=dry_run,
        n_chunks=len(by_day),
        n_eligible=len(eligible),
        verdicts=tuple(verdicts),
        to_drop=tuple(to_drop),
        blocked_at=blocked_at,
        held=held,
        bounded_at=bounded_at,
    )


def render_plan(result: RunResult) -> str:
    """A human-readable report for the job log: the per-day verdicts, what would be
    (or was) dropped, and why the frontier stopped."""
    lines = [
        f"gated-deletion run (dry_run={result.dry_run}); "
        f"{result.n_chunks} chunk(s), {result.n_eligible} eligible past the buffer"
    ]
    if result.n_chunks == 0:
        lines.append("  source reported no chunks")
    elif result.n_eligible == 0:
        lines.append("  no chunk is old enough to be a candidate yet")
    for day, decision, reason in result.verdicts:
        lines.append(f"  {day}: {decision} ({reason})")
    verb = "DROPPED" if not result.dry_run else "would drop"
    lines.append(f"  {verb} {len(result.to_drop)} day(s): {', '.join(result.to_drop) or '-'}")
    if result.blocked_at is not None:
        day, reason = result.blocked_at
        lines.append(f"  frontier halted at {day}: {reason}")
    if result.bounded_at is not None:
        lines.append(f"  evaluation bounded at {result.bounded_at} day(s) (reconciliation limit)")
    if result.held:
        lines.append(f"  held (not evaluated): {len(result.held)} day(s)")
    return "\n".join(lines)


def write_receipts_main() -> None:
    """CLI: write per-day offsite receipts for the days in a just-created bars archive.

    Invoked from the offsite backup AFTER the bars archive is verified and BEFORE the
    local Parquet is reclaimed (only then is "a local Parquet" a day in the archive).
    Pure file operations -- no database or Borg -- so it is safe to run anywhere the
    cold-bars directory is mounted."""
    import argparse
    import sys
    from pathlib import Path as _Path

    parser = argparse.ArgumentParser(description="Write cold-bar offsite receipts")
    parser.add_argument("--cold-bars-dir", type=_Path, required=True)
    parser.add_argument("--archive", required=True, help="the bars archive just created")
    args = parser.parse_args()

    written = write_receipts_for_archive(args.cold_bars_dir, args.archive)
    sys.stdout.write(
        f"wrote {len(written)} receipt(s) for {args.archive}"
        + (": " + ", ".join(written) if written else "")
        + "\n"
    )
