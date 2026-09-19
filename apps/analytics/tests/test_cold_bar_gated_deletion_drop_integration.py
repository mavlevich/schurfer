"""Real Timescale test for the gated-deletion targeted drop (drop_one_chunk_under_lock).

Proves the safety-critical properties the design and review require, against a REAL
TimescaleDB hypertable (not a fake): the drop is targeted to exactly one chunk, a mismatched
affected-set is rolled back, a changed source aborts the drop, and the advisory-lock protocol
actually serializes against a concurrent holder of the same key.

ISOLATED: creates its OWN schema (``cold_bar_drop_it``) and a throwaway hypertable, and drops
only that schema. Skips without a local TimescaleDB (the Homebrew PG used for other local runs
has no timescaledb extension); runs in CI, whose Postgres service IS the timescale image.
"""

from __future__ import annotations

# ruff: noqa: S608 -- schema/hypertable are test-only constants; every value is bound.
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from schurfer_analytics.cold_bar_gated_deletion_collectors import (
    COLD_BAR_MUTATION_LOCK_KEY,
    ColdBarDropSetError,
    ColdBarSourceChangedError,
    drop_one_chunk_under_lock,
)

_PG_DSN = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"
_SCHEMA = "cold_bar_drop_it"
_HYPERTABLE = f"{_SCHEMA}.bars"
_D1 = datetime(2026, 1, 1, tzinfo=UTC)
_D2 = datetime(2026, 1, 2, tzinfo=UTC)
_D3 = datetime(2026, 1, 3, tzinfo=UTC)
_DAY = timedelta(days=1)


def _connect_timescale_or_skip() -> Any:
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(_PG_DSN, connect_timeout=2, autocommit=True)
    except psycopg.Error as exc:
        pytest.skip(f"no local postgres reachable: {exc}")
    try:
        conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    except psycopg.Error as exc:
        conn.close()
        pytest.skip(f"no timescaledb extension available: {exc}")
    return conn


def _fresh_hypertable(conn: Any) -> None:
    conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    conn.execute(f"CREATE SCHEMA {_SCHEMA}")
    conn.execute(
        f"CREATE TABLE {_HYPERTABLE} "
        "(bucket_start timestamptz NOT NULL, symbol text NOT NULL, val double precision)"
    )
    conn.execute(
        f"SELECT create_hypertable('{_HYPERTABLE}', 'bucket_start', "
        "chunk_time_interval => INTERVAL '1 day')"
    )
    for day in (_D1, _D2, _D3):  # one row per day -> one 1-day chunk per day
        conn.execute(
            f"INSERT INTO {_HYPERTABLE} (bucket_start, symbol, val) VALUES (%s, %s, %s)",
            (day + timedelta(hours=1), "FOO", 1.0),
        )


def _chunk_days(conn: Any) -> set[str]:
    rows = conn.execute(
        "SELECT (range_start AT TIME ZONE 'UTC')::date::text "
        "FROM timescaledb_information.chunks "
        "WHERE hypertable_schema = %s AND hypertable_name = 'bars' ORDER BY range_start",
        (_SCHEMA,),
    ).fetchall()
    return {r[0] for r in rows}


def _drop_conn() -> Any:
    import psycopg

    return psycopg.connect(_PG_DSN, autocommit=True)


def test_targeted_drop_removes_exactly_one_chunk() -> None:
    admin = _connect_timescale_or_skip()
    try:
        _fresh_hypertable(admin)
        assert _chunk_days(admin) == {"2026-01-01", "2026-01-02", "2026-01-03"}
        conn = _drop_conn()
        try:
            name = drop_one_chunk_under_lock(
                conn,
                hypertable=_HYPERTABLE,
                range_start=_D2,
                range_end=_D2 + _DAY,
                verify_unchanged=lambda: True,
            )
        finally:
            conn.close()
        assert name  # the dropped chunk's name
        # Exactly the middle day is gone; the neighbours survive.
        assert _chunk_days(admin) == {"2026-01-01", "2026-01-03"}
    finally:
        admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        admin.close()


def test_source_changed_aborts_and_keeps_the_chunk() -> None:
    admin = _connect_timescale_or_skip()
    try:
        _fresh_hypertable(admin)
        conn = _drop_conn()
        try:
            with pytest.raises(ColdBarSourceChangedError):
                drop_one_chunk_under_lock(
                    conn,
                    hypertable=_HYPERTABLE,
                    range_start=_D2,
                    range_end=_D2 + _DAY,
                    verify_unchanged=lambda: False,  # fingerprint no longer matches
                )
        finally:
            conn.close()
        # Nothing dropped.
        assert _chunk_days(admin) == {"2026-01-01", "2026-01-02", "2026-01-03"}
    finally:
        admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        admin.close()


def test_multi_chunk_affected_set_is_rolled_back() -> None:
    admin = _connect_timescale_or_skip()
    try:
        _fresh_hypertable(admin)
        conn = _drop_conn()
        try:
            # Bounds spanning TWO days would remove two chunks -> must roll back, drop nothing.
            with pytest.raises(ColdBarDropSetError):
                drop_one_chunk_under_lock(
                    conn,
                    hypertable=_HYPERTABLE,
                    range_start=_D1,
                    range_end=_D2 + _DAY,
                    verify_unchanged=lambda: True,
                )
        finally:
            conn.close()
        # Rollback restored both chunks: all three days still present.
        assert _chunk_days(admin) == {"2026-01-01", "2026-01-02", "2026-01-03"}
    finally:
        admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        admin.close()


def test_absent_chunk_is_a_noop_failure_not_a_drop() -> None:
    admin = _connect_timescale_or_skip()
    try:
        _fresh_hypertable(admin)
        conn = _drop_conn()
        try:
            # A day with no chunk -> zero affected -> not exactly one -> fail-closed.
            with pytest.raises(ColdBarDropSetError):
                drop_one_chunk_under_lock(
                    conn,
                    hypertable=_HYPERTABLE,
                    range_start=_D3 + _DAY,
                    range_end=_D3 + 2 * _DAY,
                    verify_unchanged=lambda: True,
                )
        finally:
            conn.close()
        assert _chunk_days(admin) == {"2026-01-01", "2026-01-02", "2026-01-03"}
    finally:
        admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        admin.close()


def test_concurrent_lock_holder_blocks_the_drop() -> None:
    """A second session holding pg_advisory_xact_lock(KEY) must block the drop's acquisition,
    proving the drop honours the shared protocol (a repair path taking the same key is safe)."""
    import psycopg

    admin = _connect_timescale_or_skip()
    holder = _drop_conn()
    try:
        _fresh_hypertable(admin)
        # Hold the mutation lock in an OPEN transaction (xact lock released only on commit).
        holder.autocommit = False
        holder.execute("SELECT pg_advisory_xact_lock(%s)", (COLD_BAR_MUTATION_LOCK_KEY,))

        conn = _drop_conn()
        conn.execute("SET lock_timeout = '750ms'")
        try:
            with pytest.raises(psycopg.errors.LockNotAvailable):
                drop_one_chunk_under_lock(
                    conn,
                    hypertable=_HYPERTABLE,
                    range_start=_D2,
                    range_end=_D2 + _DAY,
                    verify_unchanged=lambda: True,
                )
        finally:
            conn.close()
        # The competing session blocked the drop; nothing was removed.
        assert _chunk_days(admin) == {"2026-01-01", "2026-01-02", "2026-01-03"}
    finally:
        holder.rollback()  # release the advisory lock
        holder.close()
        admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        admin.close()
