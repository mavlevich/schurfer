"""The real PostgreSQL join keeps every qualified Bybit episode in the denominator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from schurfer_analytics.source_lead_shadow_diagnostic import load_rows
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"
_FINGERPRINT = "cd" * 32


async def test_query_retains_missing_attempt_and_separates_versions() -> None:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"no local postgres reachable: {exc}")
    qv = f"test_shadow_diag_{uuid4().hex[:12]}"
    base = f"SDG{uuid4().hex[:8].upper()}"
    source_at = datetime.now(UTC) - timedelta(hours=7)
    if source_at < datetime(2026, 9, 29, tzinfo=UTC):
        await engine.dispose()
        pytest.skip("test database clock precedes the registered cohort")
    observed = source_at + timedelta(seconds=1)
    qualified = source_at + timedelta(seconds=2)
    event_ids: list[int] = []
    try:
        async with engine.begin() as connection:
            for index in range(3):
                event_id = (
                    await connection.execute(
                        text(
                            "INSERT INTO app.pump_events (base, peak_pct, last_pct, exchanges) "
                            "VALUES (:base, 25.0, 20.0, '[]'::jsonb) RETURNING id"
                        ),
                        {"base": f"{base}{index}"},
                    )
                ).scalar_one()
                event_ids.append(event_id)
                capture_id = (
                    await connection.execute(
                        text("""
                            INSERT INTO app.source_lead_captures (
                                event_id, capture_version, source_exchange, base,
                                source_symbol, source_first_observed_at,
                                collector_started_at, capture_started_at,
                                capture_completed_at, status, eligibility_reason,
                                source_change_pct, first_sources, source_payload
                            ) VALUES (
                                :event_id, 'test_capture_v1', 'gate', :base, :symbol,
                                :source_at, :source_at, :source_at, :source_at,
                                'complete', 'eligible', 20.0, '[]'::jsonb, '{}'::jsonb
                            ) RETURNING id
                        """),
                        {
                            "event_id": event_id,
                            "base": f"{base}{index}",
                            "symbol": f"{base}{index}_USDT",
                            "source_at": source_at + timedelta(seconds=index),
                        },
                    )
                ).scalar_one()
                await connection.execute(
                    text("""
                        INSERT INTO app.source_lead_qualifications (
                            capture_id, qualification_version, identity_registry_version,
                            identity_registry_fingerprint, venue_selector_version,
                            status, reason, canonical_asset_id, selected_target_exchange,
                            selected_round_trip_impact_bps, requested_notional_usd,
                            qualified_at, details
                        ) VALUES (
                            :capture_id, :qv, 'test_registry_v1', :fingerprint,
                            'lowest_round_trip_impact_v1', 'qualified',
                            'lowest_round_trip_impact', :canonical, 'bybit',
                            5.0, 50.0, :qualified, '{}'::jsonb
                        )
                    """),
                    {
                        "capture_id": capture_id,
                        "qv": qv,
                        "fingerprint": _FINGERPRINT,
                        "canonical": f"canonical:{capture_id}",
                        "qualified": qualified + timedelta(seconds=index),
                    },
                )
                await connection.execute(
                    text("""
                        INSERT INTO app.source_lead_target_observations (
                            capture_id, target_exchange, status, eligibility_reason,
                            identity_match_method, identity_verified, observed_at,
                            latency_ms, requested_notional_usd, instrument,
                            ticker, liquidity
                        ) VALUES (
                            :capture_id, 'bybit', 'sampled', 'eligible',
                            'registry_exact_v2', true, :observed,
                            50, 50.0, '{"identity_key":"bybit:swap:ABCUSDT:1"}'::jsonb,
                            '{}'::jsonb, '{}'::jsonb
                        )
                    """),
                    {"capture_id": capture_id, "observed": observed + timedelta(seconds=index)},
                )
                if index in (0, 2):
                    await connection.execute(
                        text("""
                            INSERT INTO app.source_lead_shadow_attempts (
                                capture_id, qualification_version, shadow_version,
                                source_first_observed_at, observed_at, qualified_at,
                                first_seen_at, outcome, late, gate_to_seen_ms,
                                detect_latency_ms, from_qualified_ms,
                                quote_requested_at, quote_received_at, book_ts_ms,
                                quote_change_bps
                            ) VALUES (
                                :capture_id, :qv, :shadow_version,
                                :source_at, :observed, :qualified, :seen,
                                'shadow_recorded', false, 3000, 2000, 1000,
                                :requested, :received, :book_ts, 2.5
                            )
                        """),
                        {
                            "capture_id": capture_id,
                            "qv": qv,
                            "shadow_version": (
                                "source_lead_shadow_v1" if index == 0 else "unexpected_v2"
                            ),
                            "source_at": source_at,
                            "observed": observed,
                            "qualified": qualified,
                            "seen": source_at + timedelta(seconds=3),
                            "requested": source_at + timedelta(seconds=4),
                            "received": source_at + timedelta(seconds=5),
                            "book_ts": round(
                                (source_at + timedelta(seconds=4.7)).timestamp() * 1000
                            ),
                        },
                    )
        _, rows = await load_rows(
            TEST_DATABASE_URL,
            until=source_at + timedelta(minutes=1),
            qualification_version=qv,
        )
        assert len(rows) == 3
        assert rows[0].outcome == "shadow_recorded"
        assert rows[0].quote_change_bps == 2.5
        assert rows[1].outcome is None
        assert rows[1].shadow_version is None
        assert rows[1].observed_at is not None
        assert rows[2].shadow_version == "unexpected_v2"
        with pytest.raises(ValueError, match="registered read grace"):
            await load_rows(
                TEST_DATABASE_URL,
                until=datetime.now(UTC) - timedelta(minutes=1),
                qualification_version=qv,
            )
    finally:
        if event_ids:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM app.pump_events WHERE id = ANY(:ids)"),
                    {"ids": event_ids},
                )
        await engine.dispose()
