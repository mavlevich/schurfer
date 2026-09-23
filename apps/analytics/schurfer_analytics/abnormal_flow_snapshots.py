# ruff: noqa: E501, S608
import hashlib
import json
from collections.abc import Iterator, Sequence
from datetime import UTC
from pathlib import Path
from typing import Any

import duckdb

from .abnormal_flow_replay import DecisionFeatures, Outcome


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


class SnapshotWriter:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.db = duckdb.connect(":memory:")
        self.db.execute(
            "CREATE TABLE decisions (exchange VARCHAR, market_type VARCHAR, native_market_id VARCHAR, capture_version VARCHAR, symbol VARCHAR, canonical_asset VARCHAR, decision_at TIMESTAMPTZ, oi_growth_pct DOUBLE, buy_pressure DOUBLE, containment DOUBLE, oi_native_amount DOUBLE, oi_native_value_usd DOUBLE, decision_price DOUBLE, pre_decision_turnover_usd DOUBLE, iso_week VARCHAR, unavailable_reason VARCHAR)"
        )

    def append_decisions(self, decisions: Sequence[DecisionFeatures]) -> None:
        if not decisions:
            return
        rows = [
            (
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
        self.db.executemany(
            "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
        )

    def write_decisions(self) -> tuple[Path, str, int]:
        path = self.output_dir / "decisions_snapshot.parquet"
        tmp = path.with_suffix(".tmp")
        self.db.execute(
            f"COPY (SELECT * FROM decisions ORDER BY exchange, market_type, symbol, capture_version, decision_at) TO '{tmp}' (FORMAT PARQUET)"
        )
        row = self.db.execute("SELECT count(*) FROM decisions").fetchone()
        assert row is not None
        row_count = row[0]
        tmp.replace(path)
        return path, _sha256_file(path), row_count


def _create_and_write(
    table_name: str, cols: str, data: list[tuple[Any, ...]], output_dir: Path, name: str
) -> tuple[Path, str, int]:
    db = duckdb.connect(":memory:")
    db.execute(f"CREATE TABLE {table_name} {cols}")
    if data:
        placeholders = ", ".join(["?"] * len(data[0]))
        db.executemany(f"INSERT INTO {table_name} VALUES ({placeholders})", data)
    path = output_dir / f"{name}_snapshot.parquet"
    tmp = path.with_suffix(".tmp")
    db.execute(
        f"COPY (SELECT * FROM {table_name} ORDER BY exchange, market_type, symbol, capture_version, decision_at) TO '{tmp}' (FORMAT PARQUET)"
    )
    row_count = len(data)
    tmp.replace(path)
    return path, _sha256_file(path), row_count


def write_episodes(output_dir: Path, episodes: Sequence[DecisionFeatures]) -> tuple[Path, str, int]:
    cols = "(exchange VARCHAR, market_type VARCHAR, native_market_id VARCHAR, capture_version VARCHAR, symbol VARCHAR, canonical_asset VARCHAR, decision_at TIMESTAMPTZ, oi_growth_pct DOUBLE, buy_pressure DOUBLE, containment DOUBLE, oi_native_amount DOUBLE, oi_native_value_usd DOUBLE, decision_price DOUBLE, pre_decision_turnover_usd DOUBLE, iso_week VARCHAR, unavailable_reason VARCHAR)"
    data = [
        (
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
    return _create_and_write("episodes", cols, data, output_dir, "episodes")


def write_controls(output_dir: Path, controls: Sequence[DecisionFeatures]) -> tuple[Path, str, int]:
    cols = "(exchange VARCHAR, market_type VARCHAR, native_market_id VARCHAR, capture_version VARCHAR, symbol VARCHAR, canonical_asset VARCHAR, decision_at TIMESTAMPTZ, oi_growth_pct DOUBLE, buy_pressure DOUBLE, containment DOUBLE, oi_native_amount DOUBLE, oi_native_value_usd DOUBLE, decision_price DOUBLE, pre_decision_turnover_usd DOUBLE, iso_week VARCHAR, unavailable_reason VARCHAR)"
    data = [
        (
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
    return _create_and_write("controls", cols, data, output_dir, "controls")


def write_outcomes(output_dir: Path, outcomes: Sequence[Outcome]) -> tuple[Path, str, int]:
    cols = "(exchange VARCHAR, market_type VARCHAR, native_market_id VARCHAR, capture_version VARCHAR, symbol VARCHAR, decision_at TIMESTAMPTZ, entry_price DOUBLE, exit_price DOUBLE)"
    data = [
        (
            d.exchange,
            d.market_type,
            d.native_market_id,
            d.capture_version,
            d.symbol,
            d.decision_at,
            d.entry_price,
            d.exit_price,
        )
        for d in outcomes
    ]
    return _create_and_write("outcomes", cols, data, output_dir, "outcomes")


def _row_to_decision(row: tuple[Any, ...]) -> DecisionFeatures:
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


def _row_to_outcome(row: tuple[Any, ...]) -> Outcome:
    return Outcome(
        exchange=row[0],
        market_type=row[1],
        native_market_id=row[2],
        capture_version=row[3],
        symbol=row[4],
        decision_at=row[5].astimezone(UTC),
        entry_price=row[6],
        exit_price=row[7],
    )


def iter_decisions(parquet_path: Path) -> Iterator[DecisionFeatures]:
    db = duckdb.connect(":memory:")
    cursor = db.execute(
        f"SELECT * FROM read_parquet('{parquet_path}') ORDER BY exchange, market_type, symbol, capture_version, decision_at"
    )
    while True:
        rows = cursor.fetchmany(10_000)
        if not rows:
            break
        for row in rows:
            yield _row_to_decision(row)


def load_episodes(parquet_path: Path) -> list[DecisionFeatures]:
    db = duckdb.connect(":memory:")
    return [
        _row_to_decision(row)
        for row in db.execute(
            f"SELECT * FROM read_parquet('{parquet_path}') ORDER BY exchange, market_type, symbol, capture_version, decision_at"
        ).fetchall()
    ]


def load_controls(parquet_path: Path) -> list[DecisionFeatures]:
    db = duckdb.connect(":memory:")
    return [
        _row_to_decision(row)
        for row in db.execute(
            f"SELECT * FROM read_parquet('{parquet_path}') ORDER BY exchange, market_type, symbol, capture_version, decision_at"
        ).fetchall()
    ]


def load_outcomes(parquet_path: Path) -> list[Outcome]:
    db = duckdb.connect(":memory:")
    return [
        _row_to_outcome(row)
        for row in db.execute(
            f"SELECT * FROM read_parquet('{parquet_path}') ORDER BY exchange, market_type, symbol, capture_version, decision_at"
        ).fetchall()
    ]


def check_snapshot_manifest(manifest_path: Path, expected_fingerprint: str) -> None:
    if not manifest_path.exists():
        raise RuntimeError("Snapshot manifest not found")
    data = json.loads(manifest_path.read_text())
    if data["schema_version"] != "1":
        raise RuntimeError(f"Unknown snapshot schema version: {data['schema_version']}")
    if data["evaluation_fingerprint"] != expected_fingerprint:
        raise RuntimeError(
            f"Snapshot fingerprint mismatch. Expected {expected_fingerprint}, found {data['evaluation_fingerprint']}"
        )

    # Check hashes
    for key, expected_hash in data["artifact_hashes"].items():
        if key.endswith("_parquet"):
            path = manifest_path.parent / f"{key.removesuffix('_parquet')}.parquet"
            if not path.exists():
                raise RuntimeError(f"Missing artifact file {path.name}")
            actual_hash = _sha256_file(path)
            if actual_hash != expected_hash:
                raise RuntimeError(f"Snapshot artifact hash mismatch for {path.name}")
