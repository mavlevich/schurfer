import hashlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

import duckdb

from .abnormal_flow_replay import DecisionFeatures, Outcome

UTC = UTC


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


@dataclass
class SnapshotManifest:
    schema_version: str
    evaluation_fingerprint: str
    artifact_hashes: dict[str, str]
    row_counts: dict[str, int]
    schemas: dict[str, str]


class SnapshotWriter:
    def __init__(self, output_dir: Path, fingerprint: str):
        self.output_dir = output_dir
        self.fingerprint = fingerprint
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.db = duckdb.connect(":memory:")

        self.db.execute("""
            CREATE TABLE decisions (
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
            )
        """)

        self.db.execute("""
            CREATE TABLE episodes (
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
            )
        """)

        self.db.execute("""
            CREATE TABLE controls (
                decision_id VARCHAR,
                primary_decision_id VARCHAR,
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
            )
        """)

        self.db.execute("""
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
        """)

    def _decision_id(self, d: DecisionFeatures) -> str:
        return hashlib.sha256(
            f"{d.exchange}:{d.market_type}:{d.native_market_id}:{d.capture_version}:{d.decision_at.isoformat()}".encode()
        ).hexdigest()

    def append_decisions(self, decisions: Iterable[DecisionFeatures]) -> None:
        rows = [
            (
                self._decision_id(d),
                d.exchange,
                d.market_type,
                d.native_market_id,
                d.capture_version,
                d.symbol,
                d.canonical_asset,
                d.decision_at,
                d.oi_growth_pct,
                d.buy_pressure,
                d.containment,
                d.oi_native_amount,
                d.oi_native_value_usd,
                d.decision_price,
                d.pre_decision_turnover_usd,
                d.iso_week,
                d.unavailable_reason,
            )
            for d in decisions
        ]
        if rows:
            self.db.executemany(
                "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def append_episodes(self, episodes: Iterable[DecisionFeatures]) -> None:
        rows = [
            (
                self._decision_id(d),
                d.exchange,
                d.market_type,
                d.native_market_id,
                d.capture_version,
                d.symbol,
                d.canonical_asset,
                d.decision_at,
                d.oi_growth_pct,
                d.buy_pressure,
                d.containment,
                d.oi_native_amount,
                d.oi_native_value_usd,
                d.decision_price,
                d.pre_decision_turnover_usd,
                d.iso_week,
                d.unavailable_reason,
            )
            for d in episodes
        ]
        if rows:
            self.db.executemany(
                "INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def append_controls(
        self, primary: DecisionFeatures, controls: Iterable[DecisionFeatures]
    ) -> None:
        pid = self._decision_id(primary)
        rows = [
            (
                self._decision_id(d),
                pid,
                d.exchange,
                d.market_type,
                d.native_market_id,
                d.capture_version,
                d.symbol,
                d.canonical_asset,
                d.decision_at,
                d.oi_growth_pct,
                d.buy_pressure,
                d.containment,
                d.oi_native_amount,
                d.oi_native_value_usd,
                d.decision_price,
                d.pre_decision_turnover_usd,
                d.iso_week,
                d.unavailable_reason,
            )
            for d in controls
        ]
        if rows:
            self.db.executemany(
                "INSERT INTO controls VALUES"
                " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def append_outcome(
        self,
        decision: DecisionFeatures,
        role: str,
        outcome: Outcome | None,
        unresolved_reason: str | None = None,
    ) -> None:
        did = self._decision_id(decision)
        if outcome:
            self.db.execute(
                "INSERT INTO outcomes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    did,
                    role,
                    outcome.exchange,
                    outcome.market_type,
                    outcome.native_market_id,
                    outcome.capture_version,
                    outcome.symbol,
                    outcome.decision_at,
                    outcome.entry_price,
                    outcome.exit_price,
                    unresolved_reason,
                ),
            )
        else:
            self.db.execute(
                "INSERT INTO outcomes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    did,
                    role,
                    decision.exchange,
                    decision.market_type,
                    decision.native_market_id,
                    decision.capture_version,
                    decision.symbol,
                    decision.decision_at,
                    None,
                    None,
                    unresolved_reason,
                ),
            )

    def publish(self) -> Path:
        manifest_path = self.output_dir / "snapshot_manifest.json"

        if manifest_path.exists():
            return self.output_dir

        hashes = {}
        row_counts = {}
        schemas = {}

        for table in ["decisions", "episodes", "controls", "outcomes"]:
            path = self.output_dir / f"{table}_snapshot.parquet"
            tmp = path.with_suffix(".tmp")

            order_clause = (
                "ORDER BY exchange, market_type, symbol, capture_version, decision_at"
                if table != "controls"
                else "ORDER BY primary_decision_id"
                ", exchange, market_type, symbol, capture_version, decision_at"
            )

            self.db.execute(
                f"COPY (SELECT * FROM {table} {order_clause}) TO '{tmp}' (FORMAT PARQUET)"  # noqa: S608  # noqa: E501
            )

            row = self.db.execute("SELECT count(*) FROM " + table).fetchone()  # noqa: S608
            row_counts[table] = row[0] if row else 0

            schema = self.db.execute(f"DESCRIBE {table}").fetchall()
            schemas[table] = str([(col[0], col[1]) for col in schema])

            tmp.replace(path)
            hashes[f"{table}_parquet"] = _sha256_file(path)

        manifest = SnapshotManifest(
            schema_version="2",
            evaluation_fingerprint=self.fingerprint,
            artifact_hashes=hashes,
            row_counts=row_counts,
            schemas=schemas,
        )
        _atomic_json(manifest_path, asdict(manifest))

        return self.output_dir


class SnapshotReader:
    def __init__(self, snapshot_dir: Path, expected_fingerprint: str):
        self.snapshot_dir = snapshot_dir
        self.manifest_path = snapshot_dir / "snapshot_manifest.json"

        if not self.manifest_path.exists():
            raise RuntimeError(f"Snapshot manifest not found at {self.manifest_path}")

        data = json.loads(self.manifest_path.read_text())
        if data["schema_version"] != "2":
            raise RuntimeError(f"Unsupported snapshot schema version: {data['schema_version']}")

        if data["evaluation_fingerprint"] != expected_fingerprint:
            raise RuntimeError(
                f"Snapshot fingerprint mismatch"
                f". Expected {expected_fingerprint}, found {data['evaluation_fingerprint']}"
            )

        for key, expected_hash in data["artifact_hashes"].items():
            if key.endswith("_parquet"):
                path = (
                    self.manifest_path.parent / f"{key.removesuffix('_parquet')}_snapshot.parquet"
                )
                if not path.exists():
                    raise RuntimeError(f"Missing artifact file {path.name}")
                actual_hash = _sha256_file(path)
                if actual_hash != expected_hash:
                    raise RuntimeError(f"Snapshot artifact hash mismatch for {path.name}")

    def iter_decisions(self) -> Iterator[DecisionFeatures]:
        path = self.snapshot_dir / "decisions_snapshot.parquet"
        with duckdb.connect(":memory:") as db:
            cursor = db.execute(
                "SELECT * FROM read_parquet('"  # noqa: S608
                + str(path)
                + "') ORDER BY exchange, market_type, symbol, capture_version, decision_at"
            )
            while True:
                rows = cursor.fetchmany(10_000)
                if not rows:
                    break
                for row in rows:
                    yield self._row_to_decision(row[1:])

    def load_episodes(self) -> list[DecisionFeatures]:
        path = self.snapshot_dir / "episodes_snapshot.parquet"
        with duckdb.connect(":memory:") as db:
            return [
                self._row_to_decision(row[1:])
                for row in db.read_parquet(str(path))
                .order("exchange, market_type, symbol, capture_version, decision_at")
                .fetchall()
            ]

    def load_controls(self) -> dict[str, list[DecisionFeatures]]:
        path = self.snapshot_dir / "controls_snapshot.parquet"
        controls: dict[str, list[DecisionFeatures]] = {}
        with duckdb.connect(":memory:") as db:
            for row in (
                db.read_parquet(str(path))
                .order(
                    "primary_decision_id, exchange, market_type, symbol, capture_version, decision_at"  # noqa: E501
                )
                .fetchall()
            ):
                pid = row[1]
                if pid not in controls:
                    controls[pid] = []
                controls[pid].append(self._row_to_decision(row[2:]))
        return controls

    def load_outcomes(self) -> dict[str, tuple[Outcome | None, str | None]]:
        path = self.snapshot_dir / "outcomes_snapshot.parquet"
        outcomes = {}
        with duckdb.connect(":memory:") as db:
            for row in (
                db.read_parquet(str(path))
                .order("exchange, market_type, symbol, capture_version, decision_at")
                .fetchall()
            ):
                did = row[0]
                unresolved_reason = row[10]
                if row[8] is not None:
                    outcome = Outcome(
                        exchange=row[2],
                        market_type=row[3],
                        native_market_id=row[4],
                        capture_version=row[5],
                        symbol=row[6],
                        decision_at=row[7].astimezone(UTC),
                        entry_price=row[8],
                        exit_price=row[9],
                    )
                else:
                    outcome = None
                outcomes[did] = (outcome, unresolved_reason)
        return outcomes

    def _row_to_decision(self, row: tuple[Any, ...]) -> DecisionFeatures:
        return DecisionFeatures(
            exchange=row[0],
            market_type=row[1],
            native_market_id=row[2],
            capture_version=row[3],
            symbol=row[4],
            canonical_asset=row[5],
            decision_at=row[6].astimezone(UTC),
            oi_growth_pct=row[7],
            buy_pressure=row[8],
            containment=row[9],
            oi_native_amount=row[10],
            oi_native_value_usd=row[11],
            decision_price=row[12],
            pre_decision_turnover_usd=row[13],
            iso_week=row[14],
            unavailable_reason=row[15],
        )
