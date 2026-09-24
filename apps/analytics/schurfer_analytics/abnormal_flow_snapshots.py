"""Immutable Parquet artifacts for outcome-blind abnormal-flow replay inputs.

The bundle is addressed by an input fingerprint, published atomically as one
directory, and verified in full before any row is returned. Outcomes are an
optional, separately populated table; the outcome-blind builder never writes it.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import duckdb

from .abnormal_flow_replay import DecisionFeatures, Outcome

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

SNAPSHOT_SCHEMA_VERSION: Final = "abnormal_flow_replay_snapshot_v3"
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_FILE = "snapshot_manifest.json"
_MANIFEST_HASH_FILE = "snapshot_manifest.sha256"
_DECISION_STAGE_FILE = ".decisions.csv"
_CSV_NULL = "__SCHURFER_SNAPSHOT_NULL__"
_TABLES: Final = ("decisions", "episodes", "controls", "outcomes")


class SnapshotCorruptError(RuntimeError):
    """A published snapshot failed an integrity or relational check."""


class SnapshotInputError(ValueError):
    """Rows supplied to the writer are duplicated or inconsistent."""


class SnapshotPublishOutcome(Enum):
    CREATED = "created"
    ALREADY_EXISTS = "already_exists"


@dataclass(frozen=True)
class ColdBarInput:
    day: str
    file_name: str
    row_count: int
    sha256: str
    source_fingerprint: str


@dataclass(frozen=True)
class SnapshotIdentity:
    pipeline_version: str
    contract_hash: str
    contract_version: str
    identity_snapshot_hash: str
    dependency_start: str
    evaluation_start: str
    evaluation_end_exclusive: str
    cold_bars: tuple[ColdBarInput, ...]

    def payload(self) -> dict[str, Any]:
        return asdict(self)

    def fingerprint(self) -> str:
        return _sha256_text(_canonical_json(self.payload()))


@dataclass(frozen=True)
class ArtifactRecord:
    file_name: str
    sha256: str
    row_count: int
    columns: tuple[tuple[str, str], ...]
    row_order: str


@dataclass(frozen=True)
class SnapshotManifest:
    schema_version: str
    fingerprint: str
    identity: dict[str, Any]
    code_revision: str
    working_tree_dirty: bool
    generated_at: str
    artifacts: dict[str, ArtifactRecord]


@dataclass(frozen=True)
class SnapshotPublishResult:
    outcome: SnapshotPublishOutcome
    directory: Path
    manifest: SnapshotManifest


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_fingerprint(fingerprint: str) -> None:
    if _FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise ValueError("snapshot fingerprint must be 64 lowercase hexadecimal characters")


def snapshot_directory(root: Path, fingerprint: str) -> Path:
    _validate_fingerprint(fingerprint)
    return root / fingerprint[:2] / fingerprint


def decision_id(decision: DecisionFeatures) -> str:
    route = (
        decision.exchange,
        decision.market_type,
        decision.native_market_id,
        decision.capture_version,
        decision.decision_at.astimezone(UTC).isoformat(),
    )
    return _sha256_text(_canonical_json(route))


def _write_durable(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _decision_row(decision: DecisionFeatures) -> tuple[Any, ...]:
    return (
        decision_id(decision),
        decision.exchange,
        decision.market_type,
        decision.native_market_id,
        decision.capture_version,
        decision.symbol,
        decision.canonical_asset,
        decision.decision_at,
        decision.oi_growth_pct,
        decision.buy_pressure,
        decision.containment,
        decision.oi_native_amount,
        decision.oi_native_value_usd,
        decision.decision_price,
        decision.pre_decision_turnover_usd,
        decision.iso_week,
        decision.unavailable_reason,
    )


def _csv_row(row: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(_CSV_NULL if value is None else value for value in row)


_DECISION_COLUMNS = """
    decision_id VARCHAR,
    exchange VARCHAR,
    market_type VARCHAR,
    native_market_id VARCHAR,
    capture_version VARCHAR,
    symbol VARCHAR,
    canonical_asset VARCHAR,
    decision_at TIMESTAMPTZ,
    oi_growth_pct DOUBLE,
    buy_pressure DOUBLE,
    containment DOUBLE,
    oi_native_amount DOUBLE,
    oi_native_value_usd DOUBLE,
    decision_price DOUBLE,
    pre_decision_turnover_usd DOUBLE,
    iso_week VARCHAR,
    unavailable_reason VARCHAR
"""

_EPISODE_INSERT = "INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
_CONTROL_INSERT = (
    "INSERT INTO controls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class SnapshotWriter:
    """Disk-backed staging writer with atomic whole-bundle publication."""

    def __init__(
        self,
        root: Path,
        identity: SnapshotIdentity,
        *,
        code_revision: str,
        working_tree_dirty: bool,
        memory_limit: str = "512MB",
        threads: int = 2,
    ) -> None:
        if threads < 1:
            raise ValueError("threads must be positive")
        self.root = root
        self.identity = identity
        self.fingerprint = identity.fingerprint()
        self.code_revision = code_revision
        self.working_tree_dirty = working_tree_dirty
        self._published = False
        shard = root / self.fingerprint[:2]
        shard.mkdir(parents=True, exist_ok=True)
        self._staging_dir = Path(tempfile.mkdtemp(dir=shard, prefix=".tmp-"))
        self._db_path = self._staging_dir / "snapshot.duckdb"
        self._db = duckdb.connect(str(self._db_path))
        self._db.execute("SET memory_limit = ?", [memory_limit])
        self._db.execute("SET threads = ?", [threads])
        self._db.execute(f"CREATE TABLE decisions ({_DECISION_COLUMNS})")
        self._db.execute(f"CREATE TABLE episodes ({_DECISION_COLUMNS})")
        self._db.execute(
            f"CREATE TABLE controls (primary_decision_id VARCHAR, {_DECISION_COLUMNS})"
        )
        self._db.execute(
            """
            CREATE TABLE outcomes (
                decision_id VARCHAR,
                role VARCHAR,
                exchange VARCHAR,
                market_type VARCHAR,
                native_market_id VARCHAR,
                capture_version VARCHAR,
                symbol VARCHAR,
                decision_at TIMESTAMPTZ,
                entry_price DOUBLE,
                exit_price DOUBLE,
                unresolved_reason VARCHAR
            )
            """
        )
        self._decision_stage_path = self._staging_dir / _DECISION_STAGE_FILE
        self._decision_stage = self._decision_stage_path.open("x", newline="")
        self._decision_csv = csv.writer(self._decision_stage, lineterminator="\n")
        self._decisions_loaded = False

    def __enter__(self) -> SnapshotWriter:
        return self

    def __exit__(self, exc_type: Any, exc: Any, _traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._decision_stage.close()
        with contextlib.suppress(Exception):
            self._db.close()
        if not self._published:
            shutil.rmtree(self._staging_dir, ignore_errors=True)

    def append_decisions(self, decisions: Iterable[DecisionFeatures]) -> None:
        if self._decisions_loaded:
            raise SnapshotInputError("cannot append decisions after the decision stage is sealed")
        self._decision_csv.writerows(_csv_row(_decision_row(decision)) for decision in decisions)

    def _load_decisions(self) -> None:
        if self._decisions_loaded:
            return
        self._decision_stage.flush()
        os.fsync(self._decision_stage.fileno())
        self._decision_stage.close()
        self._db.execute(
            "COPY decisions FROM ? "
            "(FORMAT CSV, HEADER FALSE, NULL '__SCHURFER_SNAPSHOT_NULL__')",
            [str(self._decision_stage_path)],
        )
        self._decision_stage_path.unlink()
        self._decisions_loaded = True

    def append_episodes(self, episodes: Iterable[DecisionFeatures]) -> None:
        rows = [_decision_row(episode) for episode in episodes]
        if rows:
            self._db.executemany(_EPISODE_INSERT, rows)

    def append_controls(
        self, primary: DecisionFeatures, controls: Iterable[DecisionFeatures]
    ) -> None:
        primary_id = decision_id(primary)
        rows = [(primary_id, *_decision_row(control)) for control in controls]
        if rows:
            self._db.executemany(_CONTROL_INSERT, rows)

    def append_outcome(
        self,
        decision: DecisionFeatures,
        role: str,
        outcome: Outcome | None,
        unresolved_reason: str | None,
    ) -> None:
        if role not in {"primary", "control"}:
            raise SnapshotInputError(f"unsupported outcome role {role!r}")
        if outcome is None and not unresolved_reason:
            raise SnapshotInputError("a missing outcome requires an unresolved reason")
        if outcome is not None and outcome.route_key() != decision.route_key():
            raise SnapshotInputError("outcome route does not match its decision")
        self._db.execute(
            "INSERT INTO outcomes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                decision_id(decision),
                role,
                decision.exchange,
                decision.market_type,
                decision.native_market_id,
                decision.capture_version,
                decision.symbol,
                decision.decision_at,
                outcome.entry_price if outcome else None,
                outcome.exit_price if outcome else None,
                unresolved_reason,
            ),
        )

    def iter_decisions(self) -> Iterator[DecisionFeatures]:
        self._load_decisions()
        cursor = self._db.execute(
            "SELECT * FROM decisions ORDER BY exchange, market_type, native_market_id, "
            "capture_version, decision_at"
        )
        while rows := cursor.fetchmany(10_000):
            for row in rows:
                yield _row_to_decision(row[1:])

    def _scalar(self, sql: str) -> int:
        row = self._db.execute(sql).fetchone()
        return int(row[0]) if row is not None else 0

    def _validate(self) -> None:
        uniqueness_queries = {
            "decisions": "SELECT count(*) - count(DISTINCT decision_id) FROM decisions",
            "episodes": "SELECT count(*) - count(DISTINCT decision_id) FROM episodes",
            "controls": (
                "SELECT count(*) - count(DISTINCT (primary_decision_id, decision_id)) FROM controls"
            ),
            "outcomes": "SELECT count(*) - count(DISTINCT decision_id) FROM outcomes",
        }
        for table, query in uniqueness_queries.items():
            if self._scalar(query):
                raise SnapshotInputError(f"duplicate identity in {table}")

        relation_queries = {
            "episode without decision": (
                "SELECT count(*) FROM episodes e LEFT JOIN decisions d USING (decision_id) "
                "WHERE d.decision_id IS NULL"
            ),
            "control without decision": (
                "SELECT count(*) FROM controls c LEFT JOIN decisions d USING (decision_id) "
                "WHERE d.decision_id IS NULL"
            ),
            "control without primary episode": (
                "SELECT count(*) FROM controls c LEFT JOIN episodes e "
                "ON c.primary_decision_id = e.decision_id WHERE e.decision_id IS NULL"
            ),
            "outcome without requested decision": (
                "SELECT count(*) FROM outcomes o LEFT JOIN ("
                "SELECT decision_id FROM episodes UNION SELECT decision_id FROM controls"
                ") r USING (decision_id) WHERE r.decision_id IS NULL"
            ),
            "ambiguous outcome completeness": (
                "SELECT count(*) FROM outcomes WHERE "
                "((entry_price IS NULL OR exit_price IS NULL) AND unresolved_reason IS NULL) "
                "OR (entry_price IS NOT NULL AND exit_price IS NOT NULL "
                "AND unresolved_reason IS NOT NULL)"
            ),
        }
        for label, query in relation_queries.items():
            if self._scalar(query):
                raise SnapshotInputError(label)

    def _artifact_record(self, table: str, path: Path, row_order: str) -> ArtifactRecord:
        count = self._scalar(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed table registry
        described = self._db.execute(f"DESCRIBE {table}").fetchall()
        columns = tuple((str(row[0]), str(row[1])) for row in described)
        return ArtifactRecord(
            file_name=path.name,
            sha256=_sha256_file(path),
            row_count=count,
            columns=columns,
            row_order=row_order,
        )

    def publish(self) -> SnapshotPublishResult:
        if self._published:
            raise RuntimeError("snapshot writer has already published")
        self._load_decisions()
        self._validate()
        orders = {
            "decisions": "exchange, market_type, native_market_id, capture_version, decision_at",
            "episodes": "exchange, market_type, native_market_id, capture_version, decision_at",
            "controls": (
                "primary_decision_id, exchange, market_type, native_market_id, "
                "capture_version, decision_at"
            ),
            "outcomes": "exchange, market_type, native_market_id, capture_version, decision_at",
        }
        artifacts: dict[str, ArtifactRecord] = {}
        for table in _TABLES:
            path = self._staging_dir / f"{table}_snapshot.parquet"
            self._db.execute(
                f"COPY (SELECT * FROM {table} ORDER BY {orders[table]}) "  # noqa: S608
                "TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
                [str(path)],
            )
            artifacts[table] = self._artifact_record(table, path, orders[table])
            with path.open("rb") as stream:
                os.fsync(stream.fileno())

        manifest = SnapshotManifest(
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            fingerprint=self.fingerprint,
            identity=self.identity.payload(),
            code_revision=self.code_revision,
            working_tree_dirty=self.working_tree_dirty,
            generated_at=datetime.now(UTC).isoformat(),
            artifacts=artifacts,
        )
        manifest_json = _canonical_json(asdict(manifest))
        _write_durable(self._staging_dir / _MANIFEST_FILE, manifest_json.encode())
        _write_durable(
            self._staging_dir / _MANIFEST_HASH_FILE,
            _sha256_text(manifest_json).encode(),
        )
        self._db.close()
        self._db_path.unlink(missing_ok=True)
        self._db_path.with_suffix(".duckdb.wal").unlink(missing_ok=True)
        _fsync_dir(self._staging_dir)

        final_dir = snapshot_directory(self.root, self.fingerprint)
        try:
            self._staging_dir.rename(final_dir)
        except OSError:
            shutil.rmtree(self._staging_dir, ignore_errors=True)
            if not final_dir.exists():
                raise
            winner = SnapshotReader(final_dir, self.fingerprint).manifest
            self._published = True
            return SnapshotPublishResult(
                SnapshotPublishOutcome.ALREADY_EXISTS,
                final_dir,
                winner,
            )

        self._published = True
        with contextlib.suppress(OSError):
            _fsync_dir(final_dir.parent)
        return SnapshotPublishResult(SnapshotPublishOutcome.CREATED, final_dir, manifest)


def _manifest_from_dict(data: dict[str, Any]) -> SnapshotManifest:
    try:
        artifacts = {
            name: ArtifactRecord(
                file_name=str(record["file_name"]),
                sha256=str(record["sha256"]),
                row_count=int(record["row_count"]),
                columns=tuple((str(column[0]), str(column[1])) for column in record["columns"]),
                row_order=str(record["row_order"]),
            )
            for name, record in data["artifacts"].items()
        }
        return SnapshotManifest(
            schema_version=str(data["schema_version"]),
            fingerprint=str(data["fingerprint"]),
            identity=dict(data["identity"]),
            code_revision=str(data["code_revision"]),
            working_tree_dirty=bool(data["working_tree_dirty"]),
            generated_at=str(data["generated_at"]),
            artifacts=artifacts,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotCorruptError(f"invalid snapshot manifest: {exc}") from exc


class SnapshotReader:
    def __init__(self, snapshot_dir: Path, expected_fingerprint: str) -> None:
        _validate_fingerprint(expected_fingerprint)
        self.snapshot_dir = snapshot_dir
        manifest_path = snapshot_dir / _MANIFEST_FILE
        hash_path = snapshot_dir / _MANIFEST_HASH_FILE
        try:
            manifest_json = manifest_path.read_text()
            manifest_hash = hash_path.read_text().strip()
        except OSError as exc:
            raise SnapshotCorruptError(f"cannot read snapshot manifest: {exc}") from exc
        if _sha256_text(manifest_json) != manifest_hash:
            raise SnapshotCorruptError("snapshot manifest hash mismatch")
        try:
            raw = json.loads(manifest_json)
        except json.JSONDecodeError as exc:
            raise SnapshotCorruptError("snapshot manifest is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise SnapshotCorruptError("snapshot manifest must be an object")
        self.manifest = _manifest_from_dict(raw)
        if self.manifest.schema_version != SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotCorruptError(
                f"unsupported snapshot schema {self.manifest.schema_version!r}"
            )
        if self.manifest.fingerprint != expected_fingerprint:
            raise SnapshotCorruptError("snapshot fingerprint mismatch")
        if _sha256_text(_canonical_json(self.manifest.identity)) != expected_fingerprint:
            raise SnapshotCorruptError("snapshot identity does not derive its fingerprint")
        if set(self.manifest.artifacts) != set(_TABLES):
            raise SnapshotCorruptError("snapshot artifact set is incomplete")
        self._verify_artifacts_and_relations()

    @classmethod
    def open(cls, root: Path, fingerprint: str) -> SnapshotReader:
        return cls(snapshot_directory(root, fingerprint), fingerprint)

    def _verify_artifacts_and_relations(self) -> None:
        with duckdb.connect(":memory:") as db:
            for table, record in self.manifest.artifacts.items():
                expected_name = f"{table}_snapshot.parquet"
                if record.file_name != expected_name:
                    raise SnapshotCorruptError(f"unexpected snapshot artifact name for {table}")
                path = self.snapshot_dir / record.file_name
                if not path.is_file():
                    raise SnapshotCorruptError(f"missing snapshot artifact {record.file_name}")
                if _sha256_file(path) != record.sha256:
                    raise SnapshotCorruptError(f"snapshot artifact hash mismatch for {table}")
                db.read_parquet(str(path)).create_view(f"verify_{table}")
                count_row = db.execute(
                    f"SELECT count(*) FROM verify_{table}"  # noqa: S608 - fixed registry
                ).fetchone()
                if count_row is None:
                    raise SnapshotCorruptError(f"cannot count snapshot rows for {table}")
                observed_count = int(count_row[0])
                if observed_count != record.row_count:
                    raise SnapshotCorruptError(f"snapshot row count mismatch for {table}")
                described = db.execute(f"DESCRIBE verify_{table}").fetchall()
                observed_columns = tuple((str(row[0]), str(row[1])) for row in described)
                if observed_columns != record.columns:
                    raise SnapshotCorruptError(f"snapshot schema mismatch for {table}")

            duplicate_queries = {
                "decisions": (
                    "SELECT count(*) - count(DISTINCT decision_id) FROM verify_decisions"
                ),
                "episodes": "SELECT count(*) - count(DISTINCT decision_id) FROM verify_episodes",
                "controls": (
                    "SELECT count(*) - count(DISTINCT (primary_decision_id, decision_id)) "
                    "FROM verify_controls"
                ),
                "outcomes": "SELECT count(*) - count(DISTINCT decision_id) FROM verify_outcomes",
            }
            for table, query in duplicate_queries.items():
                row = db.execute(query).fetchone()
                if row is not None and int(row[0]):
                    raise SnapshotCorruptError(f"duplicate identity in {table}")

            relation_queries = (
                "SELECT count(*) FROM verify_episodes e LEFT JOIN verify_decisions d "
                "USING (decision_id) WHERE d.decision_id IS NULL",
                "SELECT count(*) FROM verify_controls c LEFT JOIN verify_decisions d "
                "USING (decision_id) WHERE d.decision_id IS NULL",
                "SELECT count(*) FROM verify_controls c LEFT JOIN verify_episodes e "
                "ON c.primary_decision_id=e.decision_id WHERE e.decision_id IS NULL",
                "SELECT count(*) FROM verify_outcomes o LEFT JOIN ("
                "SELECT decision_id FROM verify_episodes UNION "
                "SELECT decision_id FROM verify_controls) r USING (decision_id) "
                "WHERE r.decision_id IS NULL",
                "SELECT count(*) FROM verify_outcomes WHERE "
                "((entry_price IS NULL OR exit_price IS NULL) AND unresolved_reason IS NULL) "
                "OR (entry_price IS NOT NULL AND exit_price IS NOT NULL "
                "AND unresolved_reason IS NOT NULL)",
            )
            for query in relation_queries:
                row = db.execute(query).fetchone()
                if row is not None and int(row[0]):
                    raise SnapshotCorruptError("snapshot relational integrity failure")

    def iter_decisions(self) -> Iterator[DecisionFeatures]:
        path = self.snapshot_dir / self.manifest.artifacts["decisions"].file_name
        with duckdb.connect(":memory:") as db:
            cursor = db.execute(
                "SELECT * FROM read_parquet(?) ORDER BY exchange, market_type, "
                "native_market_id, capture_version, decision_at",
                [str(path)],
            )
            while rows := cursor.fetchmany(10_000):
                for row in rows:
                    yield _row_to_decision(row[1:])

    def load_episodes(self) -> list[DecisionFeatures]:
        path = self.snapshot_dir / self.manifest.artifacts["episodes"].file_name
        with duckdb.connect(":memory:") as db:
            rows = db.execute(
                "SELECT * FROM read_parquet(?) ORDER BY exchange, market_type, "
                "native_market_id, capture_version, decision_at",
                [str(path)],
            ).fetchall()
        return [_row_to_decision(row[1:]) for row in rows]

    def load_controls(self) -> dict[str, list[DecisionFeatures]]:
        path = self.snapshot_dir / self.manifest.artifacts["controls"].file_name
        with duckdb.connect(":memory:") as db:
            rows = db.execute(
                "SELECT * FROM read_parquet(?) ORDER BY primary_decision_id, exchange, "
                "market_type, native_market_id, capture_version, decision_at",
                [str(path)],
            ).fetchall()
        controls: dict[str, list[DecisionFeatures]] = {}
        for row in rows:
            controls.setdefault(str(row[0]), []).append(_row_to_decision(row[2:]))
        return controls

    def load_outcomes(self) -> dict[str, tuple[Outcome | None, str | None]]:
        path = self.snapshot_dir / self.manifest.artifacts["outcomes"].file_name
        with duckdb.connect(":memory:") as db:
            rows = db.execute(
                "SELECT * FROM read_parquet(?) ORDER BY exchange, market_type, "
                "native_market_id, capture_version, decision_at",
                [str(path)],
            ).fetchall()
        results: dict[str, tuple[Outcome | None, str | None]] = {}
        for row in rows:
            outcome = None
            if row[8] is not None:
                outcome = Outcome(
                    exchange=str(row[2]),
                    market_type=str(row[3]),
                    native_market_id=str(row[4]),
                    capture_version=str(row[5]),
                    symbol=str(row[6]),
                    decision_at=row[7].astimezone(UTC),
                    entry_price=float(row[8]),
                    exit_price=float(row[9]) if row[9] is not None else None,
                )
            reason = str(row[10]) if row[10] is not None else None
            results[str(row[0])] = (outcome, reason)
        return results


def _row_to_decision(row: tuple[Any, ...]) -> DecisionFeatures:
    numeric = [row[index] for index in range(7, 14)]
    if any(value is not None and not math.isfinite(float(value)) for value in numeric):
        raise SnapshotCorruptError("decision contains a non-finite numeric value")
    return DecisionFeatures(
        exchange=str(row[0]),
        market_type=str(row[1]),
        native_market_id=str(row[2]),
        capture_version=str(row[3]),
        symbol=str(row[4]),
        canonical_asset=str(row[5]),
        decision_at=row[6].astimezone(UTC),
        oi_growth_pct=float(row[7]) if row[7] is not None else None,
        buy_pressure=float(row[8]) if row[8] is not None else None,
        containment=float(row[9]) if row[9] is not None else None,
        oi_native_amount=float(row[10]) if row[10] is not None else None,
        oi_native_value_usd=float(row[11]) if row[11] is not None else None,
        decision_price=float(row[12]) if row[12] is not None else None,
        pre_decision_turnover_usd=float(row[13]) if row[13] is not None else None,
        iso_week=str(row[14]),
        unavailable_reason=str(row[15]) if row[15] is not None else None,
    )
