"""The actual PostgreSQL joins retain missing shadow and exit observations."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from schurfer_analytics import source_lead_v2_book_cost_diagnostic as diagnostic
from schurfer_analytics.source_lead_exit_capture import exit_target_at
from schurfer_journal.testing_database import integration_database_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DATABASE_URL = integration_database_url(sqlalchemy=True)
FINGERPRINT = "ef" * 32
IDENTITY = "bybit:swap:ABCUSDT:1"


@pytest.mark.asyncio
async def test_real_join_keeps_missing_rows_and_first_read_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            has_column = (
                await connection.execute(
                    text("""
                    SELECT count(*) FROM information_schema.columns
                    WHERE table_schema = 'app' AND table_name = 'source_lead_shadow_attempts'
                      AND column_name = 'send_cost_capture_version'
                """)
                )
            ).scalar_one()
            if not has_column:
                pytest.skip("local PostgreSQL has not applied migration 0056")
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"no local PostgreSQL reachable: {exc}")

    qv = f"test_cost_diag_{uuid4().hex[:12]}"
    base = f"CST{uuid4().hex[:8].upper()}"
    source_at = datetime.now(UTC) - timedelta(hours=2)
    entry_at = source_at + timedelta(seconds=5)
    snapshot = {"s": "ABCUSDT", "b": [["1.99", "100"]], "a": [["2.01", "100"]]}
    book_sha = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    event_ids: list[int] = []
    try:
        async with engine.begin() as connection:
            for index in range(3):
                event_id = (
                    await connection.execute(
                        text("""
                            INSERT INTO app.pump_events (base, peak_pct, last_pct, exchanges)
                            VALUES (:base, 25.0, 20.0, '[]'::jsonb) RETURNING id
                        """),
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
                        "fingerprint": FINGERPRINT,
                        "canonical": f"canonical:{capture_id}",
                        "qualified": entry_at + timedelta(seconds=index),
                    },
                )
                if index == 2:
                    continue
                await connection.execute(
                    text("""
                        INSERT INTO app.source_lead_target_observations (
                            capture_id, target_exchange, status, eligibility_reason,
                            identity_match_method, identity_verified, observed_at,
                            latency_ms, requested_notional_usd, instrument,
                            ticker, liquidity
                        ) VALUES (
                            :capture_id, 'bybit', 'sampled', 'eligible',
                            'registry_exact_v2', true, :entry_at,
                            50, 50.0, CAST(:instrument AS jsonb), '{}'::jsonb,
                            CAST(:liquidity AS jsonb)
                        )
                    """),
                    {
                        "capture_id": capture_id,
                        "entry_at": entry_at,
                        "instrument": json.dumps({"identity_key": IDENTITY}),
                        "liquidity": json.dumps(
                            {
                                "quote_timing": {
                                    "contract_size_source": "instrument",
                                    "book_age_ms": 300,
                                },
                                "spread_bps": 20,
                                "ask_impact_bps": 15,
                            }
                        ),
                    },
                )
                if index == 1:
                    continue
                await connection.execute(
                    text("""
                        INSERT INTO app.source_lead_shadow_attempts (
                            capture_id, qualification_version, shadow_version,
                            instrument_identity_key,
                            source_first_observed_at, observed_at, qualified_at,
                            first_seen_at, outcome, late, gate_to_seen_ms,
                            detect_latency_ms, from_qualified_ms,
                            book_age_ms, quantity, send_cost_capture_version,
                            send_spread_bps, send_notional_ask_impact_bps,
                            send_qty_ask_impact_bps
                        ) VALUES (
                            :capture_id, :qv, 'source_lead_shadow_v1', :identity,
                            :source_at, :entry_at, :qualified,
                            :first_seen, 'shadow_recorded', false, 3000,
                            2000, 1000, 200, 25, 'source_lead_send_book_costs_v1',
                            25, 18, 19
                        )
                    """),
                    {
                        "capture_id": capture_id,
                        "qv": qv,
                        "identity": IDENTITY,
                        "source_at": source_at,
                        "entry_at": entry_at,
                        "qualified": entry_at,
                        "first_seen": entry_at + timedelta(seconds=1),
                    },
                )
                await connection.execute(
                    text("""
                        INSERT INTO app.source_lead_exit_observations (
                            capture_id, qualification_version, exit_version,
                            target_exchange, instrument_identity_key,
                            entry_at, target_at, claimed_at, outcome, timeliness,
                            book_age_ms, contract_size_source, hypothetical_qty,
                            bid_filled_qty, spread_bps, impact_bps,
                            book_snapshot, book_sha256
                        ) VALUES (
                            :capture_id, :qv, 'source_lead_exit_book_v1',
                            'bybit', :identity, :entry_at, :target_at, :target_at,
                            'sampled', 'on_time', 400, 'instrument', 25,
                            25, 30, 22, CAST(:snapshot AS jsonb), :book_sha
                        )
                    """),
                    {
                        "capture_id": capture_id,
                        "qv": qv,
                        "identity": IDENTITY,
                        "entry_at": entry_at,
                        "target_at": exit_target_at(entry_at),
                        "snapshot": json.dumps(snapshot),
                        "book_sha": book_sha,
                    },
                )

        window_start = source_at - timedelta(seconds=1)
        window_end = source_at + timedelta(minutes=1)
        original_rows = diagnostic._ROWS
        monkeypatch.setattr(diagnostic, "_ROWS", text("SELECT 1/0"))
        with pytest.raises(ValueError, match="before the registered read time"):
            await diagnostic.load_rows(
                TEST_DATABASE_URL,
                window_start=window_start,
                window_end=window_end,
                read_after=datetime.now(UTC) + timedelta(days=1),
                qualification_version=qv,
            )
        monkeypatch.setattr(diagnostic, "_ROWS", original_rows)
        _, rows = await diagnostic.load_rows(
            TEST_DATABASE_URL,
            window_start=window_start,
            window_end=window_end,
            read_after=source_at,
            qualification_version=qv,
        )
        assert [row.target_status for row in rows] == ["sampled", "sampled", None]
        assert [row.attempt_outcome for row in rows] == ["shadow_recorded", None, None]
        report = diagnostic.build_report(rows)
        assert report["eligible"] == 3
        assert report["target_status"] == {"no_target": 1, "sampled": 2}
        assert report["attempt_outcome"] == {"no_attempt": 2, "shadow_recorded": 1}
        assert report["paired_on_time"] == 1
        assert report["book_cost_bps"]["exit_on_time_bid_impact_bps"]["mean"] == 22
    finally:
        if event_ids:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM app.pump_events WHERE id = ANY(:ids)"),
                    {"ids": event_ids},
                )
        await engine.dispose()
