"""Real PostgreSQL -> Parquet fingerprint parity (gated-deletion enablement gate).

`fidelity_verified` trusts that the whole-row `to_json` fingerprint renders a row
IDENTICALLY whether the row is scanned from PostgreSQL (via DuckDB's postgres
scanner) or read back from the exported Parquet. Every other cold-bar test uses
DuckDB standing in for Postgres, so it cannot prove that cross-engine equality --
exactly what the drop gate depends on. This test runs the actual fingerprint
expression over a real Postgres table and over a Parquet exported from it, on the
type mix that could serialize differently (timestamptz, double, integer[],
double precision[], bytea, boolean, and NULLs).

It skips when the local migrated development Postgres is unavailable, and runs in
CI where Postgres is present (same pattern as the other *_integration tests).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics.cold_bar_export import FINGERPRINT_VERSION, _fingerprint_over, connect

if TYPE_CHECKING:
    from pathlib import Path

# libpq form (no +psycopg): shared by psycopg and DuckDB's postgres attach.
_PG_DSN = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"
_PROBE_TABLE = "timeseries.cbfp_parity_probe"


def _psycopg_or_skip() -> Any:
    psycopg = pytest.importorskip("psycopg")
    try:
        connection = psycopg.connect(_PG_DSN, connect_timeout=2, autocommit=True)
    except psycopg.Error as exc:
        pytest.skip(f"no local postgres reachable: {exc}")
    return connection


def test_source_and_file_fingerprints_match_across_postgres_and_parquet(tmp_path: Path) -> None:
    pg = _psycopg_or_skip()
    try:
        with pg.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS timeseries")
            cur.execute(f"DROP TABLE IF EXISTS {_PROBE_TABLE}")
            cur.execute(
                f"""
                CREATE TABLE {_PROBE_TABLE} (
                    symbol         VARCHAR(32) NOT NULL,
                    bucket_start   TIMESTAMPTZ NOT NULL,
                    close_price    DOUBLE PRECISION,
                    hist_counts    INTEGER[] NOT NULL,
                    hist_notional  DOUBLE PRECISION[] NOT NULL,
                    complete       BOOLEAN NOT NULL,
                    payload_hash   BYTEA NOT NULL,
                    created_at     TIMESTAMPTZ NOT NULL
                )
                """
            )
            cur.executemany(
                f"INSERT INTO {_PROBE_TABLE} VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",  # noqa: S608
                [
                    (
                        "SYM0",
                        "2099-01-01T00:00:00+00:00",
                        100.5,
                        [1, 2, 3],
                        [1.5, 2.5],
                        True,
                        b"\x00" * 32,
                        "2099-01-01T00:00:01+00:00",
                    ),
                    (
                        "SYM1",
                        "2099-01-01T00:01:00+00:00",
                        None,  # a NULL double
                        [],  # an empty array
                        [],
                        False,
                        bytes(range(32)),
                        "2099-01-01T00:02:03+00:00",
                    ),
                ],
            )

        duck = connect(_PG_DSN)
        parquet = tmp_path / "probe.parquet"
        duck.execute(
            f"COPY (SELECT * FROM pg.{_PROBE_TABLE}) "  # noqa: S608
            f"TO '{parquet}' (FORMAT PARQUET, COMPRESSION zstd)"
        )

        source_fp = _fingerprint_over(duck, f"pg.{_PROBE_TABLE}", None)
        file_fp = _fingerprint_over(duck, f"read_parquet('{parquet}')", None)

        assert source_fp == file_fp
        assert source_fp.startswith(f"{FINGERPRINT_VERSION}:")
    finally:
        with pg.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {_PROBE_TABLE}")
        pg.close()
