"""Gated deletion of cold minute-bar chunks (design: docs/runbooks/
cold-bar-gated-deletion-design-v1.md).

The automatic 35-day Timescale retention on ``timeseries.bybit_momentum_bars_1m``
drops chunks on schedule with no check that the day was exported and safely
offsite -- the failure that nearly lost 2026-09-11..12 when the exporter was
silently broken. This module replaces that with a gate: a day's chunk is dropped
only after that exact day is proven exported, present in a NAMED offsite archive
(verified by extracting and re-hashing, not by listing), and proven UNCHANGED
since export (an order-independent row fingerprint recomputed from the live
source must equal the one recorded at export). It fails closed: any doubt keeps
the data (disk grows, caught by the disk-runway alert) rather than deleting it.

This file holds the PURE decision logic and its data shapes. The impure steps --
recomputing the fingerprint from Postgres, extracting files from Borg, and the
targeted ``drop_chunks`` call -- gather their results into a ``DayEvidence`` and
feed it here, so every safety rule is unit-testable without a database, a Borg
repo, or production.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Must match cold_bar_export.SCHEMA_VERSION; a receipt written under a different
# schema is not proof about this format and blocks the drop.
EXPECTED_SCHEMA_VERSION = "cold_bars_v1"

DROP = "drop"
BLOCK = "block"


@dataclass(frozen=True)
class DropReceipt:
    """The per-day offsite receipt, written after a day is archived and verified.

    Canonical form is an immutable JSON file beside the manifest (itself archived
    to Borg on the next backup). Every field is evidence the drop gate re-checks.
    """

    day: str
    archive_name: str
    parquet_path: str
    parquet_sha256: str
    manifest_sha256: str
    row_count: int
    schema_version: str
    source_fingerprint: str  # whole-row fingerprint of the source at export
    file_fingerprint: str  # whole-row fingerprint of the exported Parquet
    fidelity_verified: bool  # source_fingerprint == file_fingerprint at export time


@dataclass(frozen=True)
class DayEvidence:
    """Everything gathered for one candidate day, handed to the pure decision.

    Impure collectors populate the fields; ``None`` means "could not obtain",
    which the gate treats as a block, never as a pass.
    """

    day: str
    eligible: bool  # chunk range_end <= cutoff (older than the retention buffer)
    manifest_present: bool
    receipt: DropReceipt | None
    receipt_offsite_confirmed: bool  # the receipt itself is present in an offsite archive
    archive_present: bool  # the receipt's named Borg archive exists
    extracted_parquet_sha256: str | None  # sha of the parquet EXTRACTED from that archive
    extracted_manifest_sha256: str | None  # sha of the manifest EXTRACTED from that archive
    recomputed_fingerprint: str | None  # source fingerprint recomputed from the live source now
    recomputed_file_fingerprint: str | None  # recomputed from the EXTRACTED offsite parquet
    # Set when gathering this day's evidence itself failed (bad receipt JSON, a Borg
    # error, a DB error). A gather failure blocks THIS day with a reason instead of
    # crashing the whole run. Default None keeps older constructions valid.
    gather_error: str | None = None


def is_eligible(chunk_range_end: datetime, now: datetime, cutoff_days: int) -> bool:
    """A chunk is a candidate only once it is entirely older than the buffer.

    Cutoff is anchored to UTC midnight so it does not depend on the time of day
    the job runs OR on the timezone of the ``now`` that is passed in. Both
    arguments must be timezone-aware; a naive datetime is rejected rather than
    guessed at, since guessing its zone is exactly how an off-by-one-day drop
    slips in. ``chunk_range_end`` is the exclusive upper bound of the chunk's
    range; the whole chunk is older than the cutoff iff its end is at or before it.
    """
    if now.tzinfo is None or chunk_range_end.tzinfo is None:
        raise ValueError("is_eligible requires timezone-aware datetimes")
    if cutoff_days <= 0:
        # A zero or negative buffer would make today's (or future) chunks eligible.
        raise ValueError("cutoff_days must be positive")
    now_utc = now.astimezone(UTC)
    midnight = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=UTC)
    cutoff = midnight - timedelta(days=cutoff_days)
    return chunk_range_end.astimezone(UTC) <= cutoff


def is_single_utc_day(range_start: datetime, range_end: datetime) -> bool:
    """True only if a chunk's range is exactly one UTC calendar day: both bounds are
    timezone-aware, the start is UTC midnight, and the end is start + 1 day. Any other
    shape (offset start, multi-day span, sub-day) means the chunk cannot be treated as
    one day, and the caller must fail closed rather than label it by range_start alone."""
    if range_start.tzinfo is None or range_end.tzinfo is None:
        return False
    start = range_start.astimezone(UTC)
    if (start.hour, start.minute, start.second, start.microsecond) != (0, 0, 0, 0):
        return False
    return range_end.astimezone(UTC) == start + timedelta(days=1)


def drop_decision(ev: DayEvidence) -> tuple[str, str]:
    """Return ``(DROP, reason)`` only if EVERY gate passes, else ``(BLOCK, reason)``.

    Order is chosen so the reason names the first missing proof. Every branch that
    cannot affirmatively prove safety blocks.
    """
    if ev.gather_error is not None:
        return BLOCK, f"could not gather evidence: {ev.gather_error}"
    if not ev.eligible:
        return BLOCK, "not yet eligible (inside the retention buffer)"
    if not ev.manifest_present:
        return BLOCK, "local manifest missing"
    if ev.receipt is None:
        return BLOCK, "offsite receipt missing"
    if ev.receipt.day != ev.day:
        return BLOCK, (
            f"receipt is for day {ev.receipt.day!r}, not the candidate {ev.day!r} "
            "(a receipt from another day must never license this drop)"
        )
    if ev.receipt.schema_version != EXPECTED_SCHEMA_VERSION:
        return BLOCK, (
            f"receipt schema_version {ev.receipt.schema_version!r} "
            f"is not {EXPECTED_SCHEMA_VERSION!r}"
        )
    if not ev.receipt.source_fingerprint or not ev.receipt.file_fingerprint:
        return BLOCK, "receipt has no fingerprints (exported before fingerprints existed)"
    if not ev.receipt.fidelity_verified:
        return BLOCK, "export did not verify file/source fidelity for this day (non-droppable)"
    if ev.receipt.source_fingerprint != ev.receipt.file_fingerprint:
        # Re-derive fidelity from the fingerprints themselves; never trust the flag
        # alone. If the two recorded fingerprints differ, the file did not capture
        # the source no matter what fidelity_verified claims.
        return BLOCK, "receipt source and file fingerprints differ (fidelity is not real)"
    if not ev.receipt_offsite_confirmed:
        return BLOCK, "receipt is not itself confirmed present in an offsite archive"
    if not ev.archive_present:
        return BLOCK, f"named offsite archive {ev.receipt.archive_name!r} is missing"
    if ev.extracted_parquet_sha256 is None:
        return BLOCK, "parquet could not be extracted from the offsite archive"
    if ev.extracted_parquet_sha256 != ev.receipt.parquet_sha256:
        return BLOCK, "extracted parquet sha256 does not match the receipt"
    if ev.extracted_manifest_sha256 is None:
        return BLOCK, "manifest could not be extracted from the offsite archive"
    if ev.extracted_manifest_sha256 != ev.receipt.manifest_sha256:
        return BLOCK, "extracted manifest sha256 does not match the receipt"
    if ev.recomputed_file_fingerprint is None:
        return BLOCK, "could not recompute the file fingerprint from the extracted parquet"
    if ev.recomputed_file_fingerprint != ev.receipt.file_fingerprint:
        return BLOCK, "extracted parquet content changed (file fingerprint mismatch)"
    if ev.recomputed_fingerprint is None:
        return BLOCK, "could not recompute the source fingerprint"
    if ev.recomputed_fingerprint != ev.receipt.source_fingerprint:
        return BLOCK, "source changed since export (fingerprint mismatch); needs_versioned_reexport"
    return DROP, "all gates passed"
