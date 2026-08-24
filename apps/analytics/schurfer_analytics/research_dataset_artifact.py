"""Immutable, content-fingerprinted, file-based artifacts freezing the exact
set of database rows a formal research report's economics are computed on.

Root problem this closes: today's "formal" reports (exit-liquidity
calibration, early-momentum net evidence, ...) query the live, mutable
production database on every invocation. Two runs of the SAME report with
the SAME filters can silently return different rows -- new trades closed in
between, a backfill script touching old rows, a bug fix changing which rows
match a WHERE clause -- with no way for a reader to tell whether a number
changed because the underlying economics changed or because the *sample*
underneath it quietly changed. `market_path_cache.py` closed exactly this
hazard for raw candle fetches (see that module's own docstring); this module
is the same discipline applied to arbitrary query-result datasets, not
candles.

**Not automatic.** A report opts in explicitly by freezing a dataset once
(via its own consumer-specific `freeze()` helper, e.g.
`exit_liquidity_calibration_dataset_artifact.freeze(...)`) and then reading
that frozen artifact back by fingerprint for every subsequent "formal"
re-run, instead of hitting the database again. A quick discovery pass that
*wants* to see fresh data on every run has no reason to use this at all.

**Contract, mechanically enforced by this module:**

* Exact cohort bounds and dataset/schema/cost-model version are recorded in
  the manifest, not inferred. `cost_model_version` is deliberately optional
  (`None` for datasets, like exit-liquidity's, whose economics have no
  separate priced cost model yet).
* Row order is the caller's responsibility (this module never sorts) but is
  recorded as a human-readable description (`row_order`), and -- unlike
  `code_revision`/`generated_at` -- IS part of the fingerprint: two writers
  claiming a different intended order for what happens to be byte-identical
  row content must not silently collide on one fingerprint where only the
  first writer's `row_order` description survives (colleague review,
  2026-08-25).
* Git revision and working-tree-dirty state are recorded on every artifact
  -- but deliberately NOT part of the fingerprint (see `_fingerprint`'s own
  docstring below): freezing byte-identical rows in the same order from two
  different-but-equivalent code revisions is the same artifact, not two.
* SHA-256 covers each file independently (`data.sha256` next to `data.json`,
  `manifest.sha256` next to `manifest.json`) -- defense in depth on top of
  the `data_sha256` field recorded *inside* the manifest, so a reader can
  catch corruption of either file even if the other is intact. On top of
  that, `read_dataset_artifact` independently RE-DERIVES the fingerprint
  from the manifest's own claimed identity fields (including the now-
  verified-genuine `data_sha256`) and rejects a mismatch -- checking the
  sidecar hashes alone is not enough, because a coordinated edit to
  data.json + both sidecars + the manifest's own `data_sha256`/`fingerprint`
  fields would otherwise pass every per-file check while serving content
  that does not match what the fingerprint cryptographically commits to
  (colleague review, 2026-08-25 -- reproduced exactly this).
* Publish is atomic at the WHOLE-ARTIFACT level: every file is written into
  a private staging directory first (fsync'd individually), then the
  staging directory is published with one `os.rename` onto the final
  `<fingerprint>/` path (fsync'd again, on the parent, for durability).
  A reader can therefore never observe a partially-published artifact --
  either the whole directory exists with everything in it, or it does not
  exist yet. First-writer-wins falls out of `os.rename`'s own atomicity:
  renaming onto an already-populated directory fails (`OSError`/ENOTEMPTY
  on POSIX), which this module treats as "someone else already published
  this fingerprint" and reads back their manifest instead of overwriting
  it. (Earlier version of this module published the four files one at a
  time with independent `os.link` calls -- a concurrent reader could
  observe `data.json` alone and fail closed as "corrupt" on a fingerprint
  that was simply still being written, and a crash between files left a
  permanently half-published fingerprint. Colleague review, 2026-08-25,
  reproduced both failure modes directly.)
* An empty row set, or a row set with a missing/duplicate row-identity
  value, is refused (`REJECTED_EMPTY_OR_AMBIGUOUS`) and nothing is written
  -- unless the caller explicitly passes `allow_empty=True` for the rare
  case where "genuinely zero rows in this cohort" is itself the correct,
  intended result. A silent empty artifact recorded as an ordinary success
  is indistinguishable from a caller bug that queried the wrong cohort.
* `read_dataset_artifact` raises `ResearchDatasetArtifactCorruptError` on
  ANY integrity failure (missing file, malformed JSON, either file's own
  SHA-256 mismatch, the manifest's recorded `data_sha256` not matching the
  data file's actual content, a re-derived fingerprint mismatch, row count
  mismatch) -- never a silent best-effort partial read. Corruption must
  surface loudly, exactly like `market_path_cache.py`'s
  `MarketPathCacheCorruptError`.
* Rows (and `cohort`/`extra`) must be genuinely JSON-native (str, int,
  float, bool, None, list, dict) -- serialization uses no `default=` escape
  hatch, so a `Decimal`, `datetime`, or other object slipping in raises
  `TypeError` immediately instead of being silently, lossily stringified
  into something that could produce an unstable fingerprint across Python
  versions. `NaN`/`Infinity`/`-Infinity` are rejected the same way
  (`allow_nan=False`) rather than silently accepted as literal non-standard
  JSON tokens (colleague review, 2026-08-25).
* A `fingerprint` argument (from a CLI flag or a caller) must be exactly 64
  lowercase hex characters -- anything else is rejected with `ValueError`
  before it is ever used to build a filesystem path, closing off path
  traversal through a malformed `--fingerprint`/`--from-artifact` value
  (colleague review, 2026-08-25).

**Deliberately generic, deliberately not a data lake.** This module knows
nothing about trades, episodes, or exit-liquidity observations -- it stores
whatever JSON-safe row dicts the caller hands it, keyed by a caller-named
`row_id_field`. Each consumer (see `exit_liquidity_calibration_dataset_
artifact.py`) owns its own row <-> dict conversion, its own cohort
definition, its own `freeze()`/`read()` wrapper, and MUST verify the
manifest's `dataset_name`/`dataset_version`/`schema_version`/`row_id_field`
before trusting the rows belong to it at all -- this module has no concept
of "the right kind of artifact" and will happily hand back any well-formed
artifact for any fingerprint asked of it. Building a shared metadata
catalog, a query planner, or automatic dataset discovery is explicitly out
of scope here.

**No offsite/archive policy.** Like `market_path_cache.py`'s own store, this
lives under `/runtime` on a single host with no backup, replication, or
rotation of its own -- "immutable" describes the content once written, not
the durability of the disk it is written to. Losing the host directory
loses every frozen artifact exactly as completely as it would have before
this module existed. A backup/retention policy for
`/runtime/research-dataset-artifacts` is a separate, later operational
concern.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

CONTRACT_VERSION = "research_dataset_artifact_v1"
_ARTIFACT_DIR_ENV_VAR = "SCHURFER_RESEARCH_DATASET_ARTIFACT_DIR"
DEFAULT_ARTIFACT_DIR = "runtime/research-dataset-artifacts"

_MANIFEST_FILENAME = "manifest.json"
_MANIFEST_HASH_FILENAME = "manifest.sha256"
_DATA_FILENAME = "data.json"
_DATA_HASH_FILENAME = "data.sha256"

_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


class ResearchDatasetArtifactCorruptError(Exception):
    """An artifact exists at its expected path but failed an integrity
    check. Never treated as a miss: a formal report reading a corrupt
    artifact as though it were simply absent could silently fall back to a
    completely different code path (e.g. a live DB query) with no signal
    that the frozen cohort it believed it was using never actually loaded.
    Must be resolved explicitly by an operator, typically by re-running the
    consumer's own `freeze()` from a known-good source."""


class ResearchDatasetArtifactWriteError(Exception):
    """Raised by a consumer's own `freeze()` wrapper, or by a report's own
    `--freeze-artifact` CLI path, when `write_dataset_artifact` reports
    `WRITE_FAILED` or `REJECTED_EMPTY_OR_AMBIGUOUS`. A freeze step that
    appears to succeed (exit code 0) but did not actually persist anything
    would leave every later `read_dataset_artifact` call for that
    fingerprint failing for a completely different, confusing reason (not
    found) -- and worse, would let an automated pipeline believe a formal
    artifact now exists when it does not (colleague review, 2026-08-25)."""


class ArtifactWriteOutcome(Enum):
    """CREATED: these exact rows are now the durable record at this
    fingerprint. ALREADY_EXISTS: a concurrent (or earlier) writer already
    published byte-identical content (including `row_order`) at this
    fingerprint -- the caller should treat this as success and, if it needs
    the manifest, use the one returned alongside (the winner's, not a
    re-derived one). WRITE_FAILED: nothing durable exists yet.
    REJECTED_EMPTY_OR_AMBIGUOUS: the caller's own rows failed the
    emptiness/identity-uniqueness check before any I/O was attempted --
    nothing was written, nothing to read back."""

    CREATED = "created"
    ALREADY_EXISTS = "already_exists"
    WRITE_FAILED = "write_failed"
    REJECTED_EMPTY_OR_AMBIGUOUS = "rejected_empty_or_ambiguous"


@dataclass(frozen=True)
class DatasetArtifactManifest:
    contract_version: str
    dataset_name: str
    dataset_version: str
    schema_version: str
    cost_model_version: str | None
    cohort: dict[str, Any]
    code_revision: str
    working_tree_dirty: bool
    generated_at: str
    row_count: int
    row_id_field: str
    row_order: str
    exclusion_reasons: dict[str, int]
    data_sha256: str
    fingerprint: str
    extra: dict[str, Any] = field(default_factory=dict)


def artifact_dir(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    return Path(os.environ.get(_ARTIFACT_DIR_ENV_VAR) or DEFAULT_ARTIFACT_DIR)


def _canonical_json(payload: Any) -> str:
    # No `default=`: anything that is not natively JSON-safe (Decimal,
    # datetime, an arbitrary object) must raise TypeError here, loudly, not
    # be silently stringified into something that could hash differently
    # across Python versions. `allow_nan=False` rejects NaN/Infinity the
    # same way instead of emitting non-standard JSON tokens for them.
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _fingerprint(
    *,
    dataset_name: str,
    dataset_version: str,
    schema_version: str,
    cost_model_version: str | None,
    cohort: dict[str, Any],
    row_id_field: str,
    row_order: str,
    data_sha256: str,
) -> str:
    """Deliberately excludes `code_revision`, `working_tree_dirty`, and
    `generated_at`: those describe *who published this instance and when*,
    not *what the frozen content is*. Two runs -- possibly from different
    (but result-equivalent) code revisions, possibly minutes apart -- that
    produce byte-identical rows in the same order for the same cohort/
    schema/cost-model are the same artifact by design, satisfying "same
    input -> same fingerprint" without requiring every consumer to also pin
    an exact git SHA. If a code change actually alters what a row means,
    that must be expressed as a `schema_version` (or `cost_model_version`)
    bump -- a field this fingerprint already covers -- not smuggled in via
    `code_revision`.

    `row_order` and `row_id_field` ARE included: they describe what the
    frozen content itself claims to be (a specific ordering, a specific
    identity key), not who/when produced it -- two writers disagreeing on
    either for otherwise-identical row content must not silently collide on
    one fingerprint."""
    payload = {
        "dataset_name": dataset_name,
        "dataset_version": dataset_version,
        "schema_version": schema_version,
        "cost_model_version": cost_model_version,
        "cohort": cohort,
        "row_id_field": row_id_field,
        "row_order": row_order,
        "data_sha256": data_sha256,
    }
    return _sha256_text(_canonical_json(payload))


def _validate_fingerprint_format(fingerprint: str) -> None:
    """A `fingerprint` that did not come from this module's own
    `_fingerprint()` -- e.g. a CLI `--fingerprint`/`--from-artifact` value
    typed or scripted by something else -- must never reach a filesystem
    path unchecked. Rejects anything that is not exactly 64 lowercase hex
    characters, which also closes off path traversal (`../`, absolute
    paths, embedded separators) through that argument."""
    if not _FINGERPRINT_RE.match(fingerprint):
        raise ValueError(
            f"invalid fingerprint {fingerprint!r}: expected exactly 64 lowercase hex characters"
        )


def _artifact_paths(directory: Path, fingerprint: str) -> tuple[Path, Path, Path, Path]:
    _validate_fingerprint_format(fingerprint)
    # Two-level fan-out, the same convention market_path_cache.py and Git's
    # own loose-object store use, so one artifact directory never ends up
    # with hundreds of thousands of entries.
    base = directory / fingerprint[:2] / fingerprint
    return (
        base / _MANIFEST_FILENAME,
        base / _MANIFEST_HASH_FILENAME,
        base / _DATA_FILENAME,
        base / _DATA_HASH_FILENAME,
    )


def _write_file_durable(path: Path, content: str) -> None:
    with path.open("w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_dataset_artifact(
    *,
    dataset_name: str,
    dataset_version: str,
    schema_version: str,
    rows: list[dict[str, Any]],
    row_id_field: str,
    row_order: str,
    cohort: dict[str, Any],
    code_revision: str,
    working_tree_dirty: bool,
    cost_model_version: str | None = None,
    exclusion_reasons: dict[str, int] | None = None,
    extra: dict[str, Any] | None = None,
    allow_empty: bool = False,
    directory: str | Path | None = None,
) -> tuple[ArtifactWriteOutcome, DatasetArtifactManifest | None]:
    """`rows` must already be JSON-safe (str/int/float/bool/None/list/dict
    only, no `NaN`/`Infinity`) and in the caller's chosen final order --
    this module never sorts or coerces types, and raises `TypeError`/
    `ValueError` immediately (not `WRITE_FAILED`) if a row fails that
    requirement, since that is a caller bug worth a specific, actionable
    error rather than a generic outcome code. Each row must contain
    `row_id_field`; a missing or duplicate value there is treated the same
    as an empty row set (see `ArtifactWriteOutcome.REJECTED_EMPTY_OR_
    AMBIGUOUS`)."""
    if not rows and not allow_empty:
        return ArtifactWriteOutcome.REJECTED_EMPTY_OR_AMBIGUOUS, None
    if rows:
        ids = [row.get(row_id_field) for row in rows]
        if any(i is None for i in ids) or len(set(ids)) != len(ids):
            return ArtifactWriteOutcome.REJECTED_EMPTY_OR_AMBIGUOUS, None

    # Deliberately outside any `except OSError` below: a TypeError/ValueError
    # here means the caller handed this function a non-JSON-safe row, which
    # must surface as exactly that, not get mapped onto WRITE_FAILED.
    data_json = _canonical_json(rows)
    data_sha256 = _sha256_text(data_json)
    fingerprint = _fingerprint(
        dataset_name=dataset_name,
        dataset_version=dataset_version,
        schema_version=schema_version,
        cost_model_version=cost_model_version,
        cohort=cohort,
        row_id_field=row_id_field,
        row_order=row_order,
        data_sha256=data_sha256,
    )
    manifest = DatasetArtifactManifest(
        contract_version=CONTRACT_VERSION,
        dataset_name=dataset_name,
        dataset_version=dataset_version,
        schema_version=schema_version,
        cost_model_version=cost_model_version,
        cohort=cohort,
        code_revision=code_revision,
        working_tree_dirty=working_tree_dirty,
        generated_at=datetime.now(UTC).isoformat(),
        row_count=len(rows),
        row_id_field=row_id_field,
        row_order=row_order,
        exclusion_reasons=dict(exclusion_reasons or {}),
        data_sha256=data_sha256,
        fingerprint=fingerprint,
        extra=dict(extra or {}),
    )
    manifest_json = _canonical_json(asdict(manifest))

    root = artifact_dir(directory)
    shard_dir = root / fingerprint[:2]
    final_dir = shard_dir / fingerprint

    try:
        shard_dir.mkdir(parents=True, exist_ok=True)
        staging_dir = Path(tempfile.mkdtemp(dir=shard_dir, prefix=".tmp-"))
    except OSError:
        return ArtifactWriteOutcome.WRITE_FAILED, None

    try:
        _write_file_durable(staging_dir / _DATA_FILENAME, data_json)
        _write_file_durable(staging_dir / _DATA_HASH_FILENAME, _sha256_text(data_json))
        _write_file_durable(staging_dir / _MANIFEST_FILENAME, manifest_json)
        _write_file_durable(staging_dir / _MANIFEST_HASH_FILENAME, _sha256_text(manifest_json))
        _fsync_dir(staging_dir)
        # Atomic at the whole-directory level: renaming onto an already-
        # populated `final_dir` fails (OSError/ENOTEMPTY on POSIX) rather
        # than merging or overwriting -- there is no directory-level
        # equivalent of os.link's create-if-absent, so the race is resolved
        # by attempting the rename and checking `final_dir` afterward
        # instead of checking existence beforehand (which would itself
        # race).
        staging_dir.rename(final_dir)
    except OSError:
        shutil.rmtree(staging_dir, ignore_errors=True)
        if final_dir.exists():
            # Someone else's rename won -- their published content is the
            # durable record now, never this call's own copy.
            existing_manifest, _existing_rows = read_dataset_artifact(
                fingerprint, directory=directory
            )
            return ArtifactWriteOutcome.ALREADY_EXISTS, existing_manifest
        return ArtifactWriteOutcome.WRITE_FAILED, None

    # The rename itself already succeeded; this is best-effort durability on top.
    with contextlib.suppress(OSError):
        _fsync_dir(shard_dir)

    return ArtifactWriteOutcome.CREATED, manifest


def _read_text_or_raise(path: Path, *, what: str) -> str:
    try:
        return path.read_text()
    except FileNotFoundError as exc:
        raise ResearchDatasetArtifactCorruptError(f"{what} missing at {path}") from exc
    except OSError as exc:
        raise ResearchDatasetArtifactCorruptError(f"cannot read {what} at {path}: {exc}") from exc


def read_dataset_artifact(
    fingerprint: str, *, directory: str | Path | None = None
) -> tuple[DatasetArtifactManifest, list[dict[str, Any]]]:
    """Raises `ResearchDatasetArtifactCorruptError` on ANY integrity
    failure. Returns the manifest and the exact JSON-safe row dicts as
    published -- restoring consumer-specific types (datetimes, dataclasses)
    from those dicts is each consumer's own `read()` wrapper's job, not
    this generic module's. Callers building a consumer-specific `read()`
    MUST additionally check `manifest.dataset_name`/`dataset_version`/
    `schema_version`/`row_id_field` themselves -- this function has no way
    to know which dataset a given fingerprint is "supposed" to belong to."""
    manifest_path, manifest_hash_path, data_path, data_hash_path = _artifact_paths(
        artifact_dir(directory), fingerprint
    )

    manifest_json = _read_text_or_raise(manifest_path, what="manifest.json")
    manifest_hash = _read_text_or_raise(manifest_hash_path, what="manifest.sha256").strip()
    if _sha256_text(manifest_json) != manifest_hash:
        raise ResearchDatasetArtifactCorruptError(
            f"manifest.json at {manifest_path} failed its manifest.sha256 check"
        )
    try:
        manifest_payload = json.loads(manifest_json)
        manifest = DatasetArtifactManifest(**manifest_payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ResearchDatasetArtifactCorruptError(
            f"manifest.json at {manifest_path} is not valid: {exc}"
        ) from exc

    data_json = _read_text_or_raise(data_path, what="data.json")
    data_hash = _read_text_or_raise(data_hash_path, what="data.sha256").strip()
    if _sha256_text(data_json) != data_hash:
        raise ResearchDatasetArtifactCorruptError(
            f"data.json at {data_path} failed its data.sha256 check"
        )
    if _sha256_text(data_json) != manifest.data_sha256:
        raise ResearchDatasetArtifactCorruptError(
            f"data.json at {data_path} does not match the manifest's recorded data_sha256 "
            "-- the data file was replaced after the manifest was published"
        )

    # Re-derive the fingerprint from the manifest's OWN claimed identity
    # fields (including the data_sha256 just independently verified above)
    # rather than trusting the `fingerprint`/`manifest.fingerprint` fields
    # at face value. Per-file sidecar hashes alone cannot catch a
    # coordinated edit that updates data.json, both sidecars, and the
    # manifest's own data_sha256 field consistently while leaving
    # `fingerprint` unchanged -- every check above would pass for tampered
    # content in that case. A mismatch here is only possible via a SHA-256
    # collision or exactly this kind of tampering (colleague review,
    # 2026-08-25, reproduced this directly against the previous version).
    recomputed_fingerprint = _fingerprint(
        dataset_name=manifest.dataset_name,
        dataset_version=manifest.dataset_version,
        schema_version=manifest.schema_version,
        cost_model_version=manifest.cost_model_version,
        cohort=manifest.cohort,
        row_id_field=manifest.row_id_field,
        row_order=manifest.row_order,
        data_sha256=manifest.data_sha256,
    )
    if recomputed_fingerprint != fingerprint or manifest.fingerprint != fingerprint:
        raise ResearchDatasetArtifactCorruptError(
            f"artifact at {manifest_path.parent} does not match its own fingerprint: "
            f"requested={fingerprint!r} manifest.fingerprint={manifest.fingerprint!r} "
            f"recomputed={recomputed_fingerprint!r}"
        )

    try:
        rows = json.loads(data_json)
    except json.JSONDecodeError as exc:
        raise ResearchDatasetArtifactCorruptError(
            f"data.json at {data_path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(rows, list) or len(rows) != manifest.row_count:
        raise ResearchDatasetArtifactCorruptError(
            f"data.json at {data_path} has {len(rows) if isinstance(rows, list) else 'non-list'} "
            f"rows, manifest records row_count={manifest.row_count}"
        )

    return manifest, rows


def validate_dataset_artifact(fingerprint: str, *, directory: str | Path | None = None) -> None:
    """Thin, intention-revealing wrapper: `read_dataset_artifact` already
    performs every check this needs: a full validation IS a full read.
    Discards the (necessarily correct-by-this-point) result rather than
    handing back potentially-large row data to a caller that only wanted a
    health check."""
    read_dataset_artifact(fingerprint, directory=directory)


def iter_artifact_fingerprints(*, directory: str | Path | None = None) -> list[str]:
    """Every fingerprint (i.e. every leaf `<xx>/<fingerprint>/` directory)
    currently on disk under `directory`, for a whole-store validate sweep.
    Does not itself validate anything -- a directory listing, not a read.
    Silently skips any leaf name that is not a well-formed fingerprint
    (e.g. a leftover `.tmp-*` staging directory from an interrupted write)
    rather than passing it on to a caller that will hand it straight to
    `_artifact_paths`."""
    root = artifact_dir(directory)
    if not root.is_dir():
        return []
    return sorted(
        leaf.name
        for shard in root.iterdir()
        if shard.is_dir()
        for leaf in shard.iterdir()
        if leaf.is_dir() and _FINGERPRINT_RE.match(leaf.name)
    )


def _build_validate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate one or every research dataset artifact on disk"
    )
    parser.add_argument(
        "--fingerprint", help="Validate only this fingerprint (default: validate every artifact)"
    )
    parser.add_argument("--directory", help=f"Override {_ARTIFACT_DIR_ENV_VAR}/default directory")
    return parser


def main() -> None:
    args = _build_validate_parser().parse_args()
    fingerprints = (
        [args.fingerprint]
        if args.fingerprint
        else iter_artifact_fingerprints(directory=args.directory)
    )
    if not fingerprints:
        sys.stdout.write("No artifacts found.\n")
        return
    failures: list[str] = []
    for fingerprint in fingerprints:
        try:
            manifest, rows = read_dataset_artifact(fingerprint, directory=args.directory)
            sys.stdout.write(
                f"OK    {fingerprint}  {manifest.dataset_name}@{manifest.dataset_version}  "
                f"{len(rows)} rows\n"
            )
        except (ResearchDatasetArtifactCorruptError, ValueError) as exc:
            failures.append(fingerprint)
            sys.stdout.write(f"FAIL  {fingerprint}  {exc}\n")
    if failures:
        sys.stderr.write(f"{len(failures)} of {len(fingerprints)} artifact(s) failed validation.\n")
        sys.exit(1)


__all__ = [
    "CONTRACT_VERSION",
    "ArtifactWriteOutcome",
    "DatasetArtifactManifest",
    "ResearchDatasetArtifactCorruptError",
    "ResearchDatasetArtifactWriteError",
    "artifact_dir",
    "iter_artifact_fingerprints",
    "read_dataset_artifact",
    "validate_dataset_artifact",
    "write_dataset_artifact",
]
