"""Real-Postgres invariants for migration 0037's derivatives context.

CI migrates its TimescaleDB service to Alembic head before this test. The
test exercises the real hypertable and migration validation trigger rather
than merely asserting strings in the migration source. It skips locally when
the dev database is absent; REQUIRE_INTEGRATION_DB=1 makes that a hard CI
failure.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

TEST_DATABASE_URL = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"


def _connect_or_skip() -> psycopg.Connection:
    try:
        return psycopg.connect(TEST_DATABASE_URL)
    except Exception as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but Postgres is unreachable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres reachable: {exc}")


_INSERT_SQL = """
    INSERT INTO timeseries.bybit_momentum_bars_1m (
        exchange, market_type, symbol, capture_version, bucket_start,
        universe_version, buy_hist_counts, buy_hist_notional,
        sell_hist_counts, sell_hist_notional, ticker_complete,
        trades_complete, complete, payload_hash,
        derivatives_context_version, derivatives_observed_this_minute,
        derivatives_complete,
        mark_price, mark_price_event_at, mark_price_observed_at,
        index_price, index_price_event_at, index_price_observed_at,
        funding_rate, funding_rate_event_at, funding_rate_observed_at,
        next_funding_at, next_funding_event_at, next_funding_observed_at
    ) VALUES (
        'test_migration_0037', 'linear', %(symbol)s, 'test_v1', %(bucket)s,
        'test-universe', '{}', '{}', '{}', '{}', true,
        true, true, decode(repeat('37', 32), 'hex'),
        %(version)s, %(observed)s, %(complete)s,
        %(mark)s, %(mark_event)s, %(mark_observed)s,
        %(index)s, %(index_event)s, %(index_observed)s,
        %(funding)s, %(funding_event)s, %(funding_observed)s,
        %(next_funding)s, %(next_event)s, %(next_observed)s
    )
"""


def _row(symbol: str, bucket: datetime) -> dict[str, object]:
    return {
        "symbol": symbol,
        "bucket": bucket,
        "version": None,
        "observed": None,
        "complete": None,
        "mark": None,
        "mark_event": None,
        "mark_observed": None,
        "index": None,
        "index_event": None,
        "index_observed": None,
        "funding": None,
        "funding_event": None,
        "funding_observed": None,
        "next_funding": None,
        "next_event": None,
        "next_observed": None,
    }


def test_derivatives_context_is_nullable_for_legacy_rows_and_enforces_provenance() -> None:
    connection = _connect_or_skip()
    symbol = f"MIG37{uuid.uuid4().hex[:12]}"
    bucket = datetime(2026, 8, 25, tzinfo=UTC)
    try:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(_INSERT_SQL, _row(symbol, bucket))
            cursor.execute(
                "SELECT derivatives_context_version, derivatives_complete "
                "FROM timeseries.bybit_momentum_bars_1m "
                "WHERE exchange='test_migration_0037' AND symbol=%s",
                (symbol,),
            )
            assert cursor.fetchone() == (None, None)

            with (
                pytest.raises(psycopg.errors.CheckViolation),
                connection.transaction(),
            ):
                bad = _row(symbol + "BAD", bucket)
                bad.update(version="derivatives_context_v1", observed=True, complete=True)
                bad["mark"] = 100.0
                cursor.execute(_INSERT_SQL, bad)

            with (
                pytest.raises(psycopg.errors.CheckViolation),
                connection.transaction(),
            ):
                unknown_version = _row(symbol + "UNKNOWNVERSION", bucket)
                unknown_version.update(
                    version="derivatives_context_v2",
                    observed=False,
                    complete=False,
                )
                cursor.execute(_INSERT_SQL, unknown_version)

            with (
                pytest.raises(psycopg.errors.CheckViolation),
                connection.transaction(),
            ):
                no_version = _row(symbol + "NOVERSION", bucket)
                no_version.update(
                    mark=100.0,
                    mark_event=bucket + timedelta(seconds=10),
                    mark_observed=bucket + timedelta(seconds=11),
                )
                cursor.execute(_INSERT_SQL, no_version)

            event_at = bucket + timedelta(seconds=10)
            observed_at = event_at + timedelta(milliseconds=5)
            valid = _row(symbol + "OK", bucket)
            valid.update(
                version="derivatives_context_v1",
                observed=True,
                complete=True,
                mark=100.0,
                mark_event=event_at,
                mark_observed=observed_at,
                index=99.5,
                index_event=event_at,
                index_observed=observed_at,
                funding=-0.0001,
                funding_event=event_at,
                funding_observed=observed_at,
                next_funding=event_at + timedelta(hours=8),
                next_event=event_at,
                next_observed=observed_at,
            )
            cursor.execute(_INSERT_SQL, valid)
            cursor.execute(
                "SELECT mark_price, mark_price_event_at, mark_price_observed_at "
                "FROM timeseries.bybit_momentum_bars_1m "
                "WHERE exchange='test_migration_0037' AND symbol=%s",
                (symbol + "OK",),
            )
            assert cursor.fetchone() == (100.0, event_at, observed_at)
    finally:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM timeseries.bybit_momentum_bars_1m "
                "WHERE exchange='test_migration_0037' AND symbol LIKE %s",
                (symbol + "%",),
            )
        connection.close()
