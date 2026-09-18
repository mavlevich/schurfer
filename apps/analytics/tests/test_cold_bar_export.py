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
    days_needing_fingerprint,
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
            buy_hist_counts INTEGER[],
            payload_hash BLOB
        )
    """)
    start, _ = day_bounds(day)
    for index in range(rows):
        connection.execute(
            "INSERT INTO pg.timeseries.bybit_momentum_bars_1m VALUES "
            "('bybit', 'linear', ?, 'cap_v1', 'uni_v1', ?, ?, [1, 2], ?)",
            [
                f"SYM{index}",
                start + timedelta(minutes=index),
                100.0 + index,
                bytes([index % 256]) * 32,
            ],
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
    with pytest.raises(Exception, match=r"(?i)bybit_momentum_bars_1m"):
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
    first = export_day(connection, _DAY, tmp_path, with_fingerprint=True)
    start, _ = day_bounds(_DAY)
    connection.execute(
        "INSERT INTO pg.timeseries.bybit_momentum_bars_1m VALUES "
        "('bybit', 'linear', 'LATE', 'cap_v1', 'uni_v1', ?, 1.0, [1, 2], ?)",
        [start + timedelta(minutes=99), b"\x99" * 32],
    )
    second = export_day(connection, _DAY, tmp_path, with_fingerprint=True)
    assert second.row_count == first.row_count + 1
    assert second.sha256 != first.sha256
    # the late row also changes the order-independent source fingerprint
    assert second.source_fingerprint != first.source_fingerprint


def test_fingerprint_is_off_by_default(tmp_path: Path) -> None:
    # The production exporter must not run the unbenchmarked fingerprint until it is
    # explicitly enabled; default export leaves the fields unset.
    manifest = export_day(_connection(3), _DAY, tmp_path)
    assert manifest.source_fingerprint is None
    assert manifest.file_fingerprint is None
    assert manifest.fidelity_verified is None


def test_export_records_matching_fidelity_and_versioned_fingerprints(tmp_path: Path) -> None:
    from schurfer_analytics.cold_bar_export import FINGERPRINT_VERSION

    manifest = export_day(_connection(3), _DAY, tmp_path, with_fingerprint=True)
    # the exported file faithfully captured the source: the whole-row fingerprint
    # computed over the Parquet equals the one computed over the source
    assert manifest.source_fingerprint == manifest.file_fingerprint
    assert manifest.fidelity_verified is True
    # fingerprints are versioned so an incompatible construction is never compared
    assert manifest.source_fingerprint is not None
    assert manifest.source_fingerprint.startswith(f"{FINGERPRINT_VERSION}:")


def test_days_needing_fingerprint_selects_only_in_window_unfingerprinted(tmp_path: Path) -> None:
    # A day exported without a fingerprint, one exported WITH, and one out of window.
    export_day(_connection(2, day=date(2026, 8, 20)), date(2026, 8, 20), tmp_path)
    export_day(
        _connection(2, day=date(2026, 8, 21)), date(2026, 8, 21), tmp_path, with_fingerprint=True
    )
    export_day(_connection(2, day=date(2026, 8, 10)), date(2026, 8, 10), tmp_path)

    needing = days_needing_fingerprint(tmp_path, oldest=date(2026, 8, 15), newest=date(2026, 8, 25))
    # 08-20 has no fingerprint and is in window; 08-21 already has one; 08-10 is out of window.
    assert needing == (date(2026, 8, 20),)


def test_days_needing_fingerprint_reexports_a_corrupt_manifest(tmp_path: Path) -> None:
    # A truncated/corrupt manifest is not a finished day: it must be re-exported, not
    # silently treated as done (which would leave it forever unfingerprinted).
    export_day(_connection(2, day=_DAY), _DAY, tmp_path)
    (tmp_path / f"bars-{_DAY.isoformat()}.manifest.json").write_text("{ this is not json")
    needing = days_needing_fingerprint(tmp_path, oldest=_DAY, newest=_DAY)
    assert needing == (_DAY,)


def test_days_needing_fingerprint_ignores_a_corrupt_manifest_out_of_window(tmp_path: Path) -> None:
    # Out of the source window it cannot be re-exported (the source is gone), so it is
    # not offered for refresh regardless of its manifest state.
    export_day(_connection(2, day=date(2026, 8, 10)), date(2026, 8, 10), tmp_path)
    (tmp_path / "bars-2026-08-10.manifest.json").write_text("truncated")
    needing = days_needing_fingerprint(tmp_path, oldest=date(2026, 8, 20), newest=date(2026, 8, 25))
    assert needing == ()


def test_days_needing_fingerprint_is_oldest_first(tmp_path: Path) -> None:
    for day in (date(2026, 8, 22), date(2026, 8, 20), date(2026, 8, 21)):
        export_day(_connection(2, day=day), day, tmp_path)
    needing = days_needing_fingerprint(tmp_path, oldest=date(2026, 8, 1), newest=date(2026, 8, 31))
    assert needing == (date(2026, 8, 20), date(2026, 8, 21), date(2026, 8, 22))


def test_refresh_re_exports_a_day_with_a_fingerprint(tmp_path: Path) -> None:
    # A day exported before fingerprinting: manifest has no fingerprint.
    export_day(_connection(3, day=_DAY), _DAY, tmp_path)
    assert verify_local(tmp_path, _DAY).source_fingerprint is None

    # Re-exporting it with a fingerprint (what --refresh-fingerprints does per day)
    # overwrites the manifest so the day becomes droppable, and it now needs no refresh.
    export_day(_connection(3, day=_DAY), _DAY, tmp_path, with_fingerprint=True)
    refreshed = verify_local(tmp_path, _DAY)
    assert refreshed.source_fingerprint is not None
    assert refreshed.fidelity_verified is True
    assert days_needing_fingerprint(tmp_path, oldest=_DAY, newest=_DAY) == ()
