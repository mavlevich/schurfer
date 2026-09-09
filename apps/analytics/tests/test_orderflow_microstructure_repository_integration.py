"""Real-Postgres coverage for the HYP-024 identity resolution and taker-
imbalance aggregation.

A mocked SQL result cannot prove that the point-in-time snapshot selection,
the base -> native-market resolution, the bars primary-key join, the
capture-version pin, and the ten/five/twenty-minute pre-decision windows are
all scoped together, so this regression test exercises the migrated schema
directly. It skips when no local Postgres is reachable (unless
REQUIRE_INTEGRATION_DB=1), matching the other repository integration tests in
this package.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.orderflow_microstructure import (
    HELD_OUT_START,
    build_coverage,
)
from schurfer_analytics.orderflow_microstructure_repository import (
    HeldOutWindowError,
    OrderflowMicrostructureRepository,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"

_DECISION_BUCKET = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
_COHORT_START = datetime(2026, 8, 10, tzinfo=UTC)
_COHORT_END = HELD_OUT_START

_INSERT_SNAPSHOT = text("""
    INSERT INTO app.momentum_universe_snapshots (
        exchange, universe_version, catalog_version, capture_version,
        schema_version, captured_at, instrument_count, payload_hash
    ) VALUES (
        :exchange, :universe_version, :catalog_version, 'v1',
        'v1', :captured_at, :instrument_count, decode(repeat('ab', 32), 'hex')
    )
""")

_INSERT_INSTRUMENT = text("""
    INSERT INTO app.momentum_universe_instruments (
        exchange, universe_version, catalog_version, native_market_id,
        base, quote, settle, native_market_type, canonical_market_type,
        onboarded_at, identity_status, identity_key, metadata_hash
    ) VALUES (
        :exchange, :universe_version, :catalog_version, :native_market_id,
        :base, 'USDT', 'USDT', 'linear', 'linear',
        :onboarded_at, 'ready', :identity_key, decode(repeat('cd', 32), 'hex')
    )
""")

_INSERT_DECISION = text("""
    INSERT INTO app.trade_decisions (
        ts, base, exchange, action, reason, decision_id, strategy_version, created_at
    ) VALUES (
        :ts, :base, :exchange, 'opened', 'integration test',
        :decision_id, 'pump_short_v1_market_quality', :ts
    )
""")

_INSERT_OUTCOME = text("""
    INSERT INTO app.trade_decision_outcomes (
        decision_id, horizon_minutes, resolver_version, timeframe_minutes,
        short_return_pct, mfe_pct, mae_pct, bars_count, expected_bars,
        status, resolved_at
    ) VALUES (
        :decision_id, 60, 'forward_v1', 1,
        :short_return_pct, :mfe_pct, :mae_pct, 60, 60,
        'complete', :resolved_at
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
        :exchange, 'linear', :symbol, :capture_version, :bucket_start,
        :universe_version, 1.0,
        :buy_notional, :sell_notional,
        '{}', '{}', '{}', '{}',
        5.0, 5.0,
        100000.0, 100000.0,
        true, :trades_complete, true,
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


async def _cleanup(connection: AsyncConnection, *, exchange: str) -> None:
    await connection.execute(
        text("DELETE FROM timeseries.bybit_momentum_bars_1m WHERE exchange = :exchange"),
        {"exchange": exchange},
    )
    await connection.execute(
        text(
            "DELETE FROM app.trade_decision_outcomes WHERE decision_id IN "
            "(SELECT decision_id FROM app.trade_decisions WHERE exchange = :exchange)"
        ),
        {"exchange": exchange},
    )
    await connection.execute(
        text("DELETE FROM app.trade_decisions WHERE exchange = :exchange"),
        {"exchange": exchange},
    )
    await connection.execute(
        text("DELETE FROM app.momentum_universe_instruments WHERE exchange = :exchange"),
        {"exchange": exchange},
    )
    await connection.execute(
        text("DELETE FROM app.momentum_universe_snapshots WHERE exchange = :exchange"),
        {"exchange": exchange},
    )


async def _seed_snapshot_and_instruments(
    connection: AsyncConnection,
    *,
    exchange: str,
    universe_version: str,
    catalog_version: str,
    native_market_ids: list[str],
    base: str,
) -> None:
    await connection.execute(
        _INSERT_SNAPSHOT,
        {
            "exchange": exchange,
            "universe_version": universe_version,
            "catalog_version": catalog_version,
            "captured_at": _DECISION_BUCKET - timedelta(days=1),
            "instrument_count": len(native_market_ids),
        },
    )
    for native in native_market_ids:
        await connection.execute(
            _INSERT_INSTRUMENT,
            {
                "exchange": exchange,
                "universe_version": universe_version,
                "catalog_version": catalog_version,
                "native_market_id": native,
                "base": base,
                "onboarded_at": _DECISION_BUCKET - timedelta(days=2),
                "identity_key": f"identity-{native}",
            },
        )


async def _seed_decision_with_outcome(
    connection: AsyncConnection,
    *,
    exchange: str,
    base: str,
    decision_id: str,
    ts: datetime,
    short_return_pct: float,
) -> None:
    await connection.execute(
        _INSERT_DECISION,
        {"ts": ts, "base": base, "exchange": exchange, "decision_id": decision_id},
    )
    await connection.execute(
        _INSERT_OUTCOME,
        {
            "decision_id": decision_id,
            "short_return_pct": short_return_pct,
            "mfe_pct": 2.5,
            "mae_pct": -1.5,
            "resolved_at": ts + timedelta(hours=2),
        },
    )


async def _seed_bars(
    connection: AsyncConnection,
    *,
    exchange: str,
    symbol: str,
    universe_version: str,
    capture_version: str,
    count: int,
    buy_notional: float,
    sell_notional: float,
) -> None:
    bars = [
        {
            "exchange": exchange,
            "symbol": symbol,
            "capture_version": capture_version,
            "bucket_start": _DECISION_BUCKET - timedelta(minutes=count - i),
            "universe_version": universe_version,
            "buy_notional": buy_notional,
            "sell_notional": sell_notional,
            "trades_complete": True,
        }
        for i in range(count)
    ]
    await connection.execute(_INSERT_BAR, bars)


async def test_resolves_identity_and_sums_taker_imbalance_over_the_pre_window() -> None:
    engine = await _connect_or_skip()
    exchange = f"test_of_{uuid.uuid4().hex[:8]}"
    native = "OFTESTUSDT"
    universe_version = f"uni-{uuid.uuid4().hex[:8]}"
    catalog_version = f"cat-{uuid.uuid4().hex[:8]}"
    decision_id = str(uuid.uuid4())
    try:
        async with engine.begin() as connection:
            await _seed_snapshot_and_instruments(
                connection,
                exchange=exchange,
                universe_version=universe_version,
                catalog_version=catalog_version,
                native_market_ids=[native],
                base="OFTEST",
            )
            await _seed_decision_with_outcome(
                connection,
                exchange=exchange,
                base="OFTEST",
                decision_id=decision_id,
                ts=_DECISION_BUCKET + timedelta(seconds=5),
                short_return_pct=3.5,
            )
            # 20 complete bars, each (sell - buy)/(sell + buy) = (75-25)/100 = 0.5.
            await _seed_bars(
                connection,
                exchange=exchange,
                symbol=native,
                universe_version=universe_version,
                capture_version="v1",
                count=20,
                buy_notional=25.0,
                sell_notional=75.0,
            )
            # Noise rows that any missing predicate would wrongly include.
            noise_bar = {
                "exchange": exchange,
                "symbol": native,
                "capture_version": "wrong_capture",
                "bucket_start": _DECISION_BUCKET - timedelta(minutes=1),
                "universe_version": universe_version,
                "buy_notional": 999999.0,
                "sell_notional": 0.0,
                "trades_complete": True,
            }
            await connection.execute(_INSERT_BAR, noise_bar)
            # An incomplete bar inside the window must not be counted.
            incomplete_bar = dict(noise_bar, capture_version="v1", trades_complete=False)
            await connection.execute(_INSERT_BAR, incomplete_bar)
            # The decision-minute bar itself is not "before" the decision.
            on_minute_bar = dict(noise_bar, capture_version="v1", bucket_start=_DECISION_BUCKET)
            await connection.execute(_INSERT_BAR, on_minute_bar)

        repository = OrderflowMicrostructureRepository(engine)
        _, rows = await repository.fetch(cohort_start=_COHORT_START, cohort_end=_COHORT_END)
        mine = [r for r in rows if r.decision_id == decision_id]
        assert len(mine) == 1
        row = mine[0]
        assert row.match_count == 1
        assert row.native_market_id == native
        assert row.market_type == "linear"
        assert row.bars_10m == 10
        assert row.bars_5m == 5
        assert row.bars_20m == 20
        # incomplete + on-minute + wrong-capture rows all excluded.
        assert row.imbalance_10m == pytest.approx(0.5 * 10)
        assert row.imbalance_5m == pytest.approx(0.5 * 5)
        assert row.imbalance_20m == pytest.approx(0.5 * 20)

        coverage = build_coverage(tuple(mine))
        assert len(coverage.measured) == 1
        assert coverage.measured[0].taker_imbalance_10m == pytest.approx(5.0)
    finally:
        async with engine.begin() as connection:
            await _cleanup(connection, exchange=exchange)
        await engine.dispose()


async def test_ambiguous_identity_is_coverage_loss_not_a_negative() -> None:
    engine = await _connect_or_skip()
    exchange = f"test_of_{uuid.uuid4().hex[:8]}"
    universe_version = f"uni-{uuid.uuid4().hex[:8]}"
    catalog_version = f"cat-{uuid.uuid4().hex[:8]}"
    decision_id = str(uuid.uuid4())
    try:
        async with engine.begin() as connection:
            # Same base resolves to TWO native markets in one snapshot.
            await _seed_snapshot_and_instruments(
                connection,
                exchange=exchange,
                universe_version=universe_version,
                catalog_version=catalog_version,
                native_market_ids=["AMBIGUSDT", "AMBIGUSDC"],
                base="AMBIG",
            )
            await _seed_decision_with_outcome(
                connection,
                exchange=exchange,
                base="AMBIG",
                decision_id=decision_id,
                ts=_DECISION_BUCKET + timedelta(seconds=5),
                short_return_pct=3.5,
            )
        repository = OrderflowMicrostructureRepository(engine)
        _, rows = await repository.fetch(cohort_start=_COHORT_START, cohort_end=_COHORT_END)
        mine = [r for r in rows if r.decision_id == decision_id]
        assert len(mine) == 1
        assert mine[0].match_count == 2
        coverage = build_coverage(tuple(mine))
        assert coverage.measured == ()
        assert coverage.by_exchange[0].ambiguous_identity == 1
    finally:
        async with engine.begin() as connection:
            await _cleanup(connection, exchange=exchange)
        await engine.dispose()


async def test_fetch_refuses_the_held_out_window() -> None:
    engine = await _connect_or_skip()
    repository = OrderflowMicrostructureRepository(engine)
    try:
        with pytest.raises(HeldOutWindowError):
            await repository.fetch(
                cohort_start=_COHORT_START, cohort_end=HELD_OUT_START + timedelta(days=1)
            )
    finally:
        await engine.dispose()
