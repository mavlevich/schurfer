"""Real-Postgres coverage for the flow-feature point-in-time join.

The repository reconstructs 121 source bars for each durable episode.  A
mocked SQL result cannot prove that exchange, native symbol, capture version,
universe version, and the inclusive 120-minute window are all scoped together,
so this regression test exercises the migrated schema directly.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.early_momentum_net_evidence import (
    EXPECTED_CONTRACT_SHA256_HEX,
    STRATEGY_NAME,
    STRATEGY_VERSION,
)
from schurfer_analytics.early_momentum_unused_flow_features_repository import (
    EarlyMomentumUnusedFlowFeaturesRepository,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"
_DECISION_BUCKET = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

_INSERT_STRATEGY = text("""
    INSERT INTO app.strategies (name, version, description)
    VALUES (:name, :version, 'unused-flow-feature repository integration test')
    ON CONFLICT (name, version) DO UPDATE SET updated_at = now()
    RETURNING id
""")

_INSERT_EPISODE = text("""
    INSERT INTO app.early_momentum_episodes (
        episode_id, strategy_id, contract_sha256,
        source_exchange, source_native_id,
        exchange, native_market_id, execution_symbol,
        execution_identity_key, source_identity_key, cluster_key,
        ceiling, features, armed_at, expires_at, status
    ) VALUES (
        :episode_id, :strategy_id, decode(:contract_sha256, 'hex'),
        :source_exchange, :source_native_id,
        :source_exchange, :source_native_id, :execution_symbol,
        :identity_key, :identity_key, :cluster_key,
        1.0, CAST(:features AS jsonb), :armed_at, :expires_at, 'closed'
    )
""")

_INSERT_TRADE = text("""
    INSERT INTO app.trades (
        strategy_id, symbol, exchange, market_type, side,
        size_usd, leverage, entry_price, entry_at,
        fees_usd, funding_usd, slippage_usd,
        gross_pnl_usd, gross_pnl_pct, net_pnl_usd, net_pnl_pct,
        accounting_version, accounting_status, status,
        setup_context, notes, episode_id, entry_idempotency_key,
        exit_price, exit_at
    ) VALUES (
        :strategy_id, :symbol, :exchange, 'perp', 'long',
        100.0, 5.0, 1.0, :entry_at,
        0.20, 0.02, 0.01,
        1.25, 1.25, 1.02, 1.02,
        'paper_conservative_costs_v1', 'complete', 'closed',
        CAST(:setup_context AS jsonb), 'integration test', :episode_id,
        :entry_idempotency_key, 1.0125, :exit_at
    )
""")

_INSERT_BAR = text("""
    INSERT INTO timeseries.bybit_momentum_bars_1m (
        exchange, market_type, symbol, capture_version, bucket_start,
        universe_version, close_price,
        buy_total_notional_usd, sell_total_notional_usd,
        buy_hist_counts, buy_hist_notional, sell_hist_counts, sell_hist_notional,
        buy_max_10s_notional_usd, sell_max_10s_notional_usd,
        open_interest, open_interest_value,
        ticker_complete, trades_complete, complete,
        price_complete, open_interest_complete, payload_hash
    ) VALUES (
        :exchange, :market_type, :symbol, :capture_version, :bucket_start,
        :universe_version, 1.0,
        :buy_notional, :sell_notional,
        '{}', '{}', '{}', '{}',
        :buy_burst, :sell_burst,
        100000.0, 100000.0,
        true, true, true,
        true, true, decode(repeat('ab', 32), 'hex')
    )
""")


async def _connect_or_skip() -> AsyncEngine:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but Postgres is unreachable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres reachable: {exc}")
    return engine


async def _ensure_strategy(connection: AsyncConnection) -> int:
    result = await connection.execute(
        _INSERT_STRATEGY,
        {"name": STRATEGY_NAME, "version": STRATEGY_VERSION},
    )
    return int(result.scalar_one())


async def _cleanup(connection: AsyncConnection, *, exchange: str) -> None:
    await connection.execute(
        text("DELETE FROM timeseries.bybit_momentum_bars_1m WHERE exchange = :exchange"),
        {"exchange": exchange},
    )
    await connection.execute(
        text(
            "DELETE FROM app.trades WHERE episode_id IN "
            "(SELECT episode_id FROM app.early_momentum_episodes "
            "WHERE source_exchange = :exchange)"
        ),
        {"exchange": exchange},
    )
    await connection.execute(
        text("DELETE FROM app.early_momentum_episodes WHERE source_exchange = :exchange"),
        {"exchange": exchange},
    )


async def test_fetch_reconstructs_only_the_exact_source_series_and_window() -> None:
    engine = await _connect_or_skip()
    exchange = f"test_flow_{uuid.uuid4().hex[:8]}"
    native_market_id = "FLOWTESTUSDT"
    capture_version = "test_capture_v1"
    universe_version = f"test-universe-{uuid.uuid4().hex[:8]}"
    episode_id = str(uuid.uuid4())
    armed_at = _DECISION_BUCKET + timedelta(seconds=5)

    try:
        async with engine.begin() as connection:
            strategy_id = await _ensure_strategy(connection)
            features = json.dumps(
                {
                    "bucket_start": _DECISION_BUCKET.isoformat(),
                    "market_type": "linear",
                    "capture_version": capture_version,
                    "universe_version": universe_version,
                }
            )
            await connection.execute(
                _INSERT_EPISODE,
                {
                    "episode_id": episode_id,
                    "strategy_id": strategy_id,
                    "contract_sha256": EXPECTED_CONTRACT_SHA256_HEX,
                    "source_exchange": exchange,
                    "source_native_id": native_market_id,
                    "execution_symbol": "FLOWTEST/USDT:USDT",
                    "identity_key": f"identity-{episode_id}",
                    "cluster_key": f"cluster-{episode_id}",
                    "features": features,
                    "armed_at": armed_at,
                    "expires_at": armed_at + timedelta(hours=1),
                },
            )
            await connection.execute(
                _INSERT_TRADE,
                {
                    "strategy_id": strategy_id,
                    "symbol": "FLOWTEST/USDT:USDT",
                    "exchange": exchange,
                    "entry_at": armed_at + timedelta(seconds=5),
                    "exit_at": armed_at + timedelta(hours=1),
                    "setup_context": json.dumps({"strategy": "early_momentum_v4", "paper": True}),
                    "episode_id": episode_id,
                    "entry_idempotency_key": f"{episode_id}:entry:base",
                },
            )

            bars = []
            for minute in range(121):
                in_recent_15m = minute >= 106
                bars.append(
                    {
                        "exchange": exchange,
                        "market_type": "linear",
                        "symbol": native_market_id,
                        "capture_version": capture_version,
                        "bucket_start": _DECISION_BUCKET - timedelta(minutes=120 - minute),
                        "universe_version": universe_version,
                        "buy_notional": 80.0 if in_recent_15m else 50.0,
                        "sell_notional": 20.0 if in_recent_15m else 50.0,
                        "buy_burst": 8.0 if in_recent_15m else 5.0,
                        "sell_burst": 2.0 if in_recent_15m else 5.0,
                    }
                )
            await connection.execute(_INSERT_BAR, bars)

            # One high-notional row per join dimension. Any missing predicate
            # would change the count/window/aggregates, while keeping the test
            # much cheaper than cloning all 121 rows for every noise series.
            noise = [
                dict(
                    bars[-1],
                    exchange=f"noise_{exchange}",
                    buy_notional=999999.0,
                ),
                dict(
                    bars[-1],
                    capture_version="wrong_capture_v1",
                    buy_notional=999999.0,
                ),
                dict(
                    bars[-1],
                    market_type="inverse",
                    buy_notional=999999.0,
                ),
                dict(
                    bars[-1],
                    bucket_start=_DECISION_BUCKET - timedelta(seconds=30),
                    universe_version="wrong-universe-v1",
                    buy_notional=999999.0,
                ),
                dict(
                    bars[-1],
                    bucket_start=_DECISION_BUCKET + timedelta(minutes=1),
                    buy_notional=999999.0,
                ),
            ]
            await connection.execute(_INSERT_BAR, noise)

        repository = EarlyMomentumUnusedFlowFeaturesRepository(engine)
        _, rows = await repository.fetch(
            cohort_start=_DECISION_BUCKET - timedelta(days=1),
            cohort_end=_DECISION_BUCKET + timedelta(days=1),
        )
        target = [row for row in rows if row.episode_id == episode_id]
        assert len(target) == 1
        row = target[0]
        assert row.bars_observed == 121
        assert row.distinct_buckets == 121
        assert row.complete_bars == 121
        assert row.first_bucket == _DECISION_BUCKET - timedelta(minutes=120)
        assert row.last_bucket == _DECISION_BUCKET
        assert row.max_gap_seconds == pytest.approx(60.0)
        assert row.buy_15m == pytest.approx(15 * 80.0)
        assert row.sell_15m == pytest.approx(15 * 20.0)
        assert row.buy_prior == pytest.approx(106 * 50.0)
        assert row.sell_prior == pytest.approx(106 * 50.0)
        assert row.buy_burst_15m == pytest.approx(15 * 8.0)
        assert row.sell_burst_15m == pytest.approx(15 * 2.0)
        assert row.oi_value_latest == pytest.approx(100000.0)
    finally:
        async with engine.begin() as connection:
            await _cleanup(connection, exchange=exchange)
            await _cleanup(connection, exchange=f"noise_{exchange}")
        await engine.dispose()
