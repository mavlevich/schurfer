"""Coverage for the cold-bar Parquet export.

These run against a real DuckDB with a second in-memory database attached as
`pg`, so the module's actual SQL executes rather than a mock of it. The source
table is dropped by Timescale after 35 days and the PostgreSQL dump only holds
what is still in the database when it runs, so a bug that silently exports less
than it claims is a bug that destroys history rather than one that annoys
someone.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import duckdb
import pytest
from schurfer_analytics.cold_bar_export import (
    SCHEMA_VERSION,
    day_bounds,
    days_to_export,
    existing_days,
    export_day,
    verify_local,
)

if TYPE_CHECKING:
    from pathlib import Path

_DAY = date(2026, 8, 20)


def _connection(rows: int = 3, *, day: date = _DAY) -> Any:
    connection = duckdb.connect()
    connection.execute("ATTACH ':memory:' AS pg")
    connection.execute("CREATE SCHEMA pg.timeseries")
    connection.execute("""
        CREATE TABLE pg.timeseries.bybit_momentum_bars_1m (
            exchange VARCHAR,
            market_type VARCHAR,
            symbol VARCHAR,
            capture_version VARCHAR,
            universe_version VARCHAR,
            bucket_start TIMESTAMPTZ,
            close_price DOUBLE,
            buy_hist_counts INTEGER[]
        )
    """)
    start, _ = day_bounds(day)
    for index in range(rows):
        connection.execute(
            "INSERT INTO pg.timeseries.bybit_momentum_bars_1m VALUES "
            "('bybit', 'linear', ?, 'cap_v1', 'uni_v1', ?, ?, [1, 2])",
            [f"SYM{index}", start + timedelta(minutes=index), 100.0 + index],
        )
    return connection


def test_exports_and_verifies_a_day(tmp_path: Path) -> None:
    manifest = export_day(_connection(3), _DAY, tmp_path)
    assert manifest.row_count == 3
    assert manifest.schema_version == SCHEMA_VERSION
    assert manifest.data_keys == (
        {
            "exchange": "bybit",
            "market_type": "linear",
            "capture_version": "cap_v1",
            "universe_version": "uni_v1",
        },
    )
    # Read back from disk, not from the values the export just computed.
    verified = verify_local(tmp_path, _DAY)
    assert verified.sha256 == manifest.sha256
    assert verified.row_count == 3


def test_array_columns_survive_the_round_trip(tmp_path: Path) -> None:
    """The source table carries integer[] and double precision[] histograms.
    A column set that quietly drops or flattens them would be a schema change
    nobody noticed until the source was already gone."""
    export_day(_connection(2), _DAY, tmp_path)
    connection = duckdb.connect()
    value = connection.execute(
        f"SELECT buy_hist_counts FROM read_parquet('{tmp_path}/bars-{_DAY.isoformat()}.parquet') "  # noqa: S608
        "LIMIT 1"
    ).fetchone()
    assert value is not None
    assert list(value[0]) == [1, 2]


def test_a_day_with_no_rows_is_refused(tmp_path: Path) -> None:
    """An empty file and a day nobody exported look identical from the outside,
    and the difference decides whether the source may be deleted."""
    with pytest.raises(ValueError, match="no rows"):
        export_day(_connection(0), _DAY, tmp_path)
    assert list(tmp_path.glob("*")) == []


def test_a_failed_export_leaves_nothing_under_the_real_name(tmp_path: Path) -> None:
    """A short file sitting under the final name is the failure that matters:
    the next run would take it for a finished export and move on."""
    connection = _connection(3)
    connection.execute("DROP TABLE pg.timeseries.bybit_momentum_bars_1m")
    with pytest.raises(Exception, match="(?i)bybit_momentum_bars_1m"):
        export_day(connection, _DAY, tmp_path)
    assert not (tmp_path / f"bars-{_DAY.isoformat()}.parquet").exists()
    assert list(tmp_path.glob(".*partial")) == []


def test_verification_catches_a_changed_file(tmp_path: Path) -> None:
    export_day(_connection(3), _DAY, tmp_path)
    target = tmp_path / f"bars-{_DAY.isoformat()}.parquet"
    target.write_bytes(target.read_bytes() + b"junk")
    with pytest.raises(ValueError, match="bytes"):
        verify_local(tmp_path, _DAY)


def test_verification_catches_a_file_replaced_at_the_same_size(tmp_path: Path) -> None:
    """Size alone is a weak check. The checksum is what makes silent corruption
    on the way to remote storage detectable."""
    export_day(_connection(3), _DAY, tmp_path)
    target = tmp_path / f"bars-{_DAY.isoformat()}.parquet"
    payload = bytearray(target.read_bytes())
    payload[-1] ^= 0xFF
    target.write_bytes(bytes(payload))
    with pytest.raises(ValueError, match="sha256"):
        verify_local(tmp_path, _DAY)


def test_verification_refuses_an_unknown_schema_version(tmp_path: Path) -> None:
    """A reader that guesses at an unfamiliar layout is worse than one that
    stops: the guess is silent and the data is the only copy."""
    export_day(_connection(3), _DAY, tmp_path)
    manifest_path = tmp_path / f"bars-{_DAY.isoformat()}.manifest.json"
    manifest_path.write_text(manifest_path.read_text().replace(SCHEMA_VERSION, "cold_bars_v99"))
    with pytest.raises(ValueError, match="schema version"):
        verify_local(tmp_path, _DAY)


def test_missing_file_beside_a_manifest_is_an_error(tmp_path: Path) -> None:
    export_day(_connection(3), _DAY, tmp_path)
    (tmp_path / f"bars-{_DAY.isoformat()}.parquet").unlink()
    with pytest.raises(ValueError, match="missing"):
        verify_local(tmp_path, _DAY)


def test_rerunning_an_exported_day_is_detected_as_already_done(tmp_path: Path) -> None:
    export_day(_connection(3), _DAY, tmp_path)
    assert existing_days(tmp_path) == (_DAY.isoformat(),)
    remaining = days_to_export(_DAY, _DAY + timedelta(days=2), existing_days(tmp_path))
    assert remaining == (_DAY + timedelta(days=1), _DAY + timedelta(days=2))


def test_days_are_exported_oldest_first() -> None:
    """The oldest day is the one retention deletes next. A run that is cut short
    must have saved the days closest to being lost."""
    days = days_to_export(date(2026, 8, 1), date(2026, 8, 5), already=())
    assert days[0] == date(2026, 8, 1)
    assert days[-1] == date(2026, 8, 5)


def test_day_bounds_are_calendar_utc_not_a_rolling_window() -> None:
    """Two runs of the same day must produce the same file, whatever time the
    timer happened to fire."""
    start, until = day_bounds(_DAY)
    assert start == datetime(2026, 8, 20, tzinfo=UTC)
    assert until == datetime(2026, 8, 21, tzinfo=UTC)


def test_a_late_row_changes_the_manifest_rather_than_being_lost(tmp_path: Path) -> None:
    """A row written after the export is not in the file, and the manifest is
    what says so. Re-exporting the day produces a different row count and
    checksum, which is exactly the signal a controlled deletion must refuse to
    ignore."""
    connection = _connection(3)
    first = export_day(connection, _DAY, tmp_path)
    start, _ = day_bounds(_DAY)
    connection.execute(
        "INSERT INTO pg.timeseries.bybit_momentum_bars_1m VALUES "
        "('bybit', 'linear', 'LATE', 'cap_v1', 'uni_v1', ?, 1.0, [1, 2])",
        [start + timedelta(minutes=99)],
    )
    second = export_day(connection, _DAY, tmp_path)
    assert second.row_count == first.row_count + 1
    assert second.sha256 != first.sha256
