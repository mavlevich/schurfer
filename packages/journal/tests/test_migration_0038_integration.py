"""Real-Postgres invariants for public liquidation capture migration 0038."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest

TEST_DATABASE_URL = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"


def _connect_or_skip() -> psycopg.Connection:
    try:
        connection = psycopg.connect(TEST_DATABASE_URL)
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('timeseries.liquidation_events') IS NOT NULL")
            if cursor.fetchone() != (True,):
                connection.close()
                pytest.skip("migration 0038 is not applied")
        return connection
    except Exception as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but Postgres/head is unavailable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres/head reachable: {exc}")


_EVENT_SQL = """
    INSERT INTO timeseries.liquidation_events (
        capture_version, exchange, market_type, native_market_id,
        universe_version, source_contract_variant, coverage_kind, position_side, event_at,
        exchange_published_at, received_at, source_session_id,
        source_event_key, payload_hash, quantity, quantity_unit, raw_payload
    ) VALUES (
        'liquidation_event_v1', %(exchange)s, 'linear', 'BTCUSDT',
        'test-universe', 'bybit_all_liquidation_v1', %(coverage)s, 'long', %(at)s, %(at)s, %(at)s,
        'session', decode(repeat(%(event_hash)s, 32), 'hex'),
        decode(repeat('38', 32), 'hex'), 1.0, 'base_asset', '{}'::jsonb
    )
"""


def test_liquidation_event_and_heartbeat_constraints_fail_closed() -> None:
    connection = _connect_or_skip()
    exchange = f"m38_{uuid.uuid4().hex[:12]}"
    at = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    try:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                _EVENT_SQL,
                {
                    "exchange": exchange,
                    "coverage": "complete_stream",
                    "at": at,
                    "event_hash": "11",
                },
            )
            with (
                pytest.raises(psycopg.errors.CheckViolation),
                connection.transaction(),
            ):
                cursor.execute(
                    _EVENT_SQL,
                    {
                        "exchange": exchange,
                        "coverage": "pretend_complete",
                        "at": at,
                        "event_hash": "12",
                    },
                )
            with (
                pytest.raises(psycopg.errors.CheckViolation),
                connection.transaction(),
            ):
                cursor.execute(
                    _EVENT_SQL.replace("'bybit_all_liquidation_v1'", "'unknown_contract'"),
                    {
                        "exchange": exchange,
                        "coverage": "complete_stream",
                        "at": at,
                        "event_hash": "13",
                    },
                )

            cursor.execute(
                """
                INSERT INTO timeseries.liquidation_capture_heartbeats_1m (
                    exchange, capture_version, market_type, coverage_kind,
                    process_session_id, universe_version, bucket_start,
                    expected_connections, connected_connections,
                    data_loss_detected, complete, events_received_total,
                    events_persisted_total, duplicate_events_total,
                    queue_drops_total, invalid_events_total, out_of_scope_total,
                    scope_tag_missing_accepted_total, reconnect_total, read_timeout_total
                ) VALUES (
                    %s, 'liquidation_event_v1', 'linear', 'complete_stream',
                    'session-ok', 'test-universe', %s, 1, 1, false, true,
                    1, 1, 0, 0, 0, 0, 0, 0, 0
                )
                """,
                (exchange, at),
            )
            with (
                pytest.raises(psycopg.errors.CheckViolation),
                connection.transaction(),
            ):
                cursor.execute(
                    """
                    INSERT INTO timeseries.liquidation_capture_heartbeats_1m (
                        exchange, capture_version, market_type, coverage_kind,
                        process_session_id, universe_version, bucket_start,
                        expected_connections, connected_connections,
                        data_loss_detected, complete, events_received_total,
                        events_persisted_total, duplicate_events_total,
                        queue_drops_total, invalid_events_total, out_of_scope_total,
                        scope_tag_missing_accepted_total, reconnect_total, read_timeout_total
                    ) VALUES (
                        %s, 'liquidation_event_v1', 'linear', 'complete_stream',
                        'session-bad', 'test-universe', %s, 1, 0, false, true,
                        0, 0, 0, 0, 0, 0, 0, 0, 0
                    )
                    """,
                    (exchange, at),
                )
    finally:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM timeseries.liquidation_events WHERE exchange=%s",
                (exchange,),
            )
            cursor.execute(
                "DELETE FROM timeseries.liquidation_capture_heartbeats_1m WHERE exchange=%s",
                (exchange,),
            )
        connection.close()
