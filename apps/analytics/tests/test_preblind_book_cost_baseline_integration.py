"""The real PostgreSQL query masks post-cutoff costs before Python sees them."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from schurfer_analytics import preblind_book_cost_baseline as baseline
from schurfer_journal.testing_database import integration_database_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DATABASE_URL = integration_database_url(sqlalchemy=True)


@pytest.mark.asyncio
async def test_real_queries_keep_missing_targets_and_mask_late_book_costs() -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        try:
            async with engine.connect() as probe:
                await probe.execute(text("SELECT 1"))
        except Exception as exc:
            pytest.skip(f"no local PostgreSQL reachable: {exc}")

        token = uuid4().hex[:12]
        paper_version = f"test_preblind_{token}"
        capture_version = f"test_preblind_{token}"
        entry = datetime(2026, 9, 28, 12, tzinfo=UTC)
        post_cutoff = baseline.WINDOW_END + timedelta(minutes=1)
        async with engine.connect() as connection, connection.begin() as transaction:
            await connection.execute(
                text("""
                        INSERT INTO app.momentum_flow_paper_runs
                            (paper_version, contract_sha256, contract_json, cohort_started_at)
                        VALUES (:version, :sha, CAST(:contract AS jsonb), :start)
                    """),
                {
                    "version": paper_version,
                    "sha": "a" * 64,
                    "contract": json.dumps({"position_notional_usd": 50}),
                    "start": entry,
                },
            )
            paper_ids = [uuid4(), uuid4()]
            for paper_id, exit_time in zip(
                paper_ids, (entry + timedelta(minutes=60), post_cutoff), strict=True
            ):
                await connection.execute(
                    text("""
                            INSERT INTO app.momentum_flow_paper_probes (
                                paper_id, paper_version, watch_version, watch_id,
                                episode_id, exchange, market_type, symbol,
                                watch_bucket_start, watch_decision_at, claimed_at,
                                entry_status, entry_quote_observed_at,
                                entry_exchange_event_at, entry_vwap,
                                entry_filled_notional_usd, entry_spread_bps,
                                entry_impact_bps, entry_at, position_status,
                                exit_reason, exit_quote_observed_at,
                                exit_exchange_event_at, exit_vwap,
                                exit_filled_notional_usd, exit_spread_bps,
                                exit_impact_bps, exit_at, updated_at
                            ) VALUES (
                                :paper_id, :version, 'test_watch', :watch_id,
                                :episode_id, 'bybit', 'swap', 'TESTUSDT',
                                :bucket, :decision, :claimed,
                                'opened', :entry, :entry, 1.01,
                                50, 8, 12, :entry, 'closed',
                                'max_hold', :exit_time, :exit_time, 0.99,
                                50, 10, 18, :exit_time, :exit_time
                            )
                        """),
                    {
                        "paper_id": paper_id,
                        "version": paper_version,
                        "watch_id": uuid4(),
                        "episode_id": uuid4(),
                        "bucket": entry - timedelta(minutes=2),
                        "decision": entry - timedelta(minutes=1),
                        "claimed": entry - timedelta(seconds=30),
                        "entry": entry,
                        "exit_time": exit_time,
                    },
                )

            event_id = (
                await connection.execute(
                    text("""
                            INSERT INTO app.pump_events
                                (base, peak_pct, last_pct, exchanges)
                            VALUES (:base, 25, 20, '[]'::jsonb) RETURNING id
                        """),
                    {"base": f"T{token}"},
                )
            ).scalar_one()
            capture_id = (
                await connection.execute(
                    text("""
                            INSERT INTO app.source_lead_captures (
                                event_id, capture_version, source_exchange, base,
                                source_symbol, source_first_observed_at,
                                collector_started_at, capture_started_at,
                                capture_completed_at, status, eligibility_reason,
                                source_change_pct, first_sources, source_payload,
                                updated_at
                            ) VALUES (
                                :event_id, :version, 'gate', :base, :symbol,
                                :entry, :entry, :entry, :entry, 'complete',
                                'eligible', 20, '[]'::jsonb, '{}'::jsonb, :entry
                            ) RETURNING id
                        """),
                    {
                        "event_id": event_id,
                        "version": capture_version,
                        "base": f"T{token}",
                        "symbol": f"T{token}_USDT",
                        "entry": entry,
                    },
                )
            ).scalar_one()
            await connection.execute(
                text("""
                        INSERT INTO app.source_lead_target_observations (
                            capture_id, target_exchange, status, eligibility_reason,
                            identity_match_method, identity_verified, observed_at,
                            latency_ms, requested_notional_usd, instrument,
                            ticker, liquidity
                        ) VALUES (
                            :capture_id, 'bybit', 'sampled', 'eligible',
                            'registry_exact_v2', true, :observed_at,
                            100, 50, '{}'::jsonb, '{}'::jsonb,
                            CAST(:liquidity AS jsonb)
                        )
                    """),
                {
                    "capture_id": capture_id,
                    "observed_at": post_cutoff,
                    "liquidity": json.dumps(
                        {"spread_bps": 3, "ask_impact_bps": 4, "bid_impact_bps": 5}
                    ),
                },
            )
            params = {
                "start": baseline.WINDOW_START,
                "end": baseline.WINDOW_END,
                "limit": baseline.MAX_ROWS + 1,
            }
            paper = [
                dict(row)
                for row in (await connection.execute(baseline.PAPER_ROWS, params)).mappings()
                if row["paper_version"] == paper_version
            ]
            source = [
                dict(row)
                for row in (await connection.execute(baseline.SOURCE_ROWS, params)).mappings()
                if row["capture_version"] == capture_version
            ]
            excluded = dict(
                (await connection.execute(baseline.EXCLUDED_COUNTS, params)).mappings().one()
            )
            assert len(paper) == 1
            assert paper[0]["exit_impact_bps"] == 18
            assert len(source) == 1
            assert source[0]["target_id"] is None
            assert excluded["paper_updated_after_cutoff"] >= 1
            assert excluded["source_targets_outside_preblind_snapshot"] >= 1
            report = baseline.summarize_cost_rows(paper, source, excluded)
            assert report["counts"][f"paper:{paper_version}:all"] == 1
            assert report["counts"][f"source:{capture_version}:no_pre_cutoff_target"] == 1
            assert report["excluded_from_preblind_snapshot"]["paper_updated_after_cutoff"] >= 1
            await transaction.rollback()
    finally:
        await engine.dispose()
