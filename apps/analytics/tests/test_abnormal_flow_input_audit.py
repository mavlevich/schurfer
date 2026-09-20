"""The real DuckDB/Parquet input path of the outcome-blind flow audit."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import duckdb
import pytest
from schurfer_analytics.abnormal_flow_input_audit import audit_directory
from schurfer_analytics.cold_bar_export import (
    EXPORT_VERSION,
    SCHEMA_VERSION,
    SOURCE_TABLE,
    ExportManifest,
    sha256_file,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_day(
    directory: Path,
    day: date,
    *,
    exchange: str = "bybit",
    oi_value: float | None = 1000.0,
    complete: bool = True,
    flow: float = 20.0,
    fidelity: bool = True,
    row_count: int = 1,
    bucket_day: date | None = None,
) -> Path:
    directory.mkdir(exist_ok=True)
    path = directory / f"bars-{day.isoformat()}.parquet"
    day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    bucket = bucket_day or day
    bucket_start = datetime(bucket.year, bucket.month, bucket.day, tzinfo=UTC)
    connection = duckdb.connect()
    try:
        connection.execute(
            """
            CREATE TABLE bars AS SELECT
                ?::VARCHAR AS exchange,
                'linear'::VARCHAR AS market_type,
                'AAAUSDT'::VARCHAR AS symbol,
                'v1'::VARCHAR AS capture_version,
                ?::TIMESTAMPTZ AS bucket_start,
                'u1'::VARCHAR AS universe_version,
                ?::BOOLEAN AS price_complete,
                ?::BOOLEAN AS trades_complete,
                ?::BOOLEAN AS open_interest_complete,
                123.0::DOUBLE AS open_interest,
                ?::TIMESTAMPTZ AS open_interest_event_at,
                ?::TIMESTAMPTZ AS open_interest_observed_at,
                ?::DOUBLE AS open_interest_value,
                99.0::DOUBLE AS last_bid_price,
                101.0::DOUBLE AS last_ask_price,
                ?::DOUBLE AS buy_total_notional_usd,
                0.0::DOUBLE AS sell_total_notional_usd,
                ?::TIMESTAMPTZ AS created_at
            """,
            [
                exchange,
                bucket_start,
                complete,
                complete,
                complete,
                day_start,
                day_start,
                oi_value,
                flow,
                day_start + timedelta(minutes=2),
            ],
        )
        quoted = str(path).replace("'", "''")
        connection.execute(f"COPY bars TO '{quoted}' (FORMAT PARQUET)")
    finally:
        connection.close()

    manifest = ExportManifest(
        schema_version=SCHEMA_VERSION,
        export_version=EXPORT_VERSION,
        source_table=SOURCE_TABLE,
        day=day.isoformat(),
        bucket_start_from=day_start.isoformat(),
        bucket_start_until=(day_start + timedelta(days=1)).isoformat(),
        row_count=row_count,
        file_name=path.name,
        file_bytes=path.stat().st_size,
        sha256=sha256_file(path),
        data_keys=(
            {
                "exchange": exchange,
                "market_type": "linear",
                "capture_version": "v1",
                "universe_version": "u1",
            },
        ),
        exported_at=day_start.isoformat(),
        source_fingerprint="cbfp_v1:fixture" if fidelity else None,
        file_fingerprint="cbfp_v1:fixture" if fidelity else None,
        fidelity_verified=fidelity,
    )
    (directory / f"bars-{day.isoformat()}.manifest.json").write_text(manifest.to_json())
    return path


def test_audit_reads_real_parquet_without_outcomes(tmp_path: Path) -> None:
    start = date(2026, 9, 18)
    _write_day(tmp_path, start)
    _write_day(
        tmp_path,
        start + timedelta(days=1),
        exchange="binance",
        oi_value=None,
        complete=False,
        flow=0.0,
    )

    audit = audit_directory(tmp_path, start=start, end=start + timedelta(days=2))

    assert len(audit.days) == 2
    assert len(audit.coverage) == 2
    bybit, binance = audit.coverage
    assert (bybit.exchange, bybit.rows, bybit.price_complete, bybit.positive_flow) == (
        "bybit",
        1,
        1,
        1,
    )
    assert binance.exchange == "binance"
    assert binance.native_oi_present == 1
    assert binance.oi_value_present == 0
    assert binance.price_complete == 0
    assert binance.positive_flow == 0
    assert binance.finalized_within_5m == 1


def test_audit_refuses_missing_day(tmp_path: Path) -> None:
    start = date(2026, 9, 18)
    _write_day(tmp_path, start)
    with pytest.raises(FileNotFoundError):
        audit_directory(tmp_path, start=start, end=start + timedelta(days=2))


def test_audit_refuses_unproven_source_fidelity(tmp_path: Path) -> None:
    start = date(2026, 9, 18)
    _write_day(tmp_path, start, fidelity=False)
    with pytest.raises(ValueError, match="source fidelity is not proven"):
        audit_directory(tmp_path, start=start, end=start + timedelta(days=1))


def test_audit_refuses_parquet_changed_after_manifest(tmp_path: Path) -> None:
    start = date(2026, 9, 18)
    path = _write_day(tmp_path, start)
    with path.open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(ValueError, match="bytes, manifest says"):
        audit_directory(tmp_path, start=start, end=start + timedelta(days=1))


def test_audit_refuses_manifest_row_count_mismatch(tmp_path: Path) -> None:
    start = date(2026, 9, 18)
    _write_day(tmp_path, start, row_count=2)
    with pytest.raises(ValueError, match="row count mismatch"):
        audit_directory(tmp_path, start=start, end=start + timedelta(days=1))


def test_audit_refuses_swapped_day_contents_even_when_total_matches(tmp_path: Path) -> None:
    start = date(2026, 9, 18)
    following = start + timedelta(days=1)
    _write_day(tmp_path, start, bucket_day=following)
    _write_day(tmp_path, following, bucket_day=start)
    with pytest.raises(ValueError, match="rows outside manifest day"):
        audit_directory(tmp_path, start=start, end=following + timedelta(days=1))


def test_audit_refuses_invalid_day_window(tmp_path: Path) -> None:
    start = date(2026, 9, 18)
    with pytest.raises(ValueError, match="end day must be after"):
        audit_directory(tmp_path, start=start, end=start)
