"""Real-Postgres coverage for the HYP-024 identity resolution, outcome
qualification, and taker-imbalance aggregation.

A mocked SQL result cannot prove that the episode dedup, the point-in-time
snapshot selection, the base -> native-market resolution, the bars primary-key
join, the capture-version pin, the complete-same-venue outcome filter, the
outcome-straddle guard, the pre-decision availability rule, and the
ten/five/twenty-minute windows are all scoped together, so this regression test
exercises the migrated schema directly. It skips when no local Postgres is
reachable (unless REQUIRE_INTEGRATION_DB=1), matching the other repository
integration tests in this package.
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
_DECISION_TS = _DECISION_BUCKET + timedelta(seconds=5)
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

_INSERT_PUMP_EVENT = text("""
    INSERT INTO app.pump_events
        (base, episode, miss_count, first_seen_at, last_seen_at,
         peak_pct, last_pct, exchanges)
    VALUES (:base, 1, 0, :ts, :ts, 50, 40, '[]'::jsonb)
    RETURNING id
""")

_INSERT_DECISION = text("""
    INSERT INTO app.trade_decisions (
        ts, base, exchange, action, reason, decision_id, pump_event_id,
        strategy_version, created_at
    ) VALUES (
        :ts, :base, :exchange, :action, 'integration test',
        :decision_id, :pump_event_id, 'pump_short_v1_market_quality', :ts
    )
""")

# status and source/anchor exchange are parameterised so a test can seed a
# 'partial' or a cross-exchange (source != anchor) outcome and prove it does
# NOT qualify.
_INSERT_OUTCOME = text("""
    INSERT INTO app.trade_decision_outcomes (
        decision_id, horizon_minutes, resolver_version, timeframe_minutes,
        short_return_pct, mfe_pct, mae_pct, bars_count, expected_bars,
        status, anchor_exchange, source_exchange, resolved_at
    ) VALUES (
        :decision_id, 60, 'forward_v1', 1,
        :short_return_pct, :mfe_pct, :mae_pct, 60, 60,
        :status, :anchor_exchange, :source_exchange, :resolved_at
    )
""")

# complete flags are parameterised so a test can seed a legitimately incomplete
# bar (complete=false) without violating the migration CHECK
# (NOT complete OR (ticker_complete AND trades_complete)); last_trade_received_at
# is explicit so the availability rule can be exercised in both directions.
_INSERT_BAR = text("""
    INSERT INTO timeseries.bybit_momentum_bars_1m (
        exchange, market_type, symbol, capture_version, bucket_start,
        universe_version, close_price,
        buy_total_notional_usd, sell_total_notional_usd,
        buy_hist_counts, buy_hist_notional, sell_hist_counts, sell_hist_notional,
        buy_max_10s_notional_usd, sell_max_10s_notional_usd,
        open_interest, open_interest_value,
        last_trade_received_at,
        ticker_complete, trades_complete, complete,
        price_complete, open_interest_complete, payload_hash
    ) VALUES (
        :exchange, 'linear', :symbol, :capture_version, :bucket_start,
        :universe_version, 1.0,
        :buy_notional, :sell_notional,
        '{}', '{}', '{}', '{}',
        5.0, 5.0,
        100000.0, 100000.0,
        :last_trade_received_at,
        :ticker_complete, :trades_complete, :complete,
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
    await connection.execute(
        text("DELETE FROM app.pump_events WHERE base LIKE 'OF%' OR base = 'AMBIG'"),
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


async def _seed_pump_event(connection: AsyncConnection, *, base: str, ts: datetime) -> int:
    result = await connection.execute(_INSERT_PUMP_EVENT, {"base": base, "ts": ts})
    return int(result.scalar_one())


async def _seed_decision_with_outcome(
    connection: AsyncConnection,
    *,
    exchange: str,
    base: str,
    decision_id: str,
    pump_event_id: int,
    ts: datetime,
    short_return_pct: float,
    action: str = "opened",
    status: str = "complete",
    anchor_exchange: str | None = None,
    source_exchange: str | None = None,
) -> None:
    await connection.execute(
        _INSERT_DECISION,
        {
            "ts": ts,
            "base": base,
            "exchange": exchange,
            "decision_id": decision_id,
            "pump_event_id": pump_event_id,
            "action": action,
        },
    )
    await connection.execute(
        _INSERT_OUTCOME,
        {
            "decision_id": decision_id,
            "short_return_pct": short_return_pct,
            "mfe_pct": 2.5,
            "mae_pct": -1.5,
            "status": status,
            "anchor_exchange": anchor_exchange if anchor_exchange is not None else exchange,
            "source_exchange": source_exchange if source_exchange is not None else exchange,
            "resolved_at": ts + timedelta(hours=2),
        },
    )


def _bar_row(
    *,
    exchange: str,
    symbol: str,
    universe_version: str,
    capture_version: str,
    bucket_start: datetime,
    buy_notional: float,
    sell_notional: float,
    trades_complete: bool = True,
    received_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "exchange": exchange,
        "symbol": symbol,
        "capture_version": capture_version,
        "bucket_start": bucket_start,
        "universe_version": universe_version,
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "ticker_complete": True,
        "trades_complete": trades_complete,
        "complete": trades_complete,
        # Default: received within the bar's own minute, so it is available for
        # any decision at or after the following minute boundary.
        "last_trade_received_at": (
            received_at if received_at is not None else bucket_start + timedelta(seconds=59)
        ),
    }


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
        _bar_row(
            exchange=exchange,
            symbol=symbol,
            universe_version=universe_version,
            capture_version=capture_version,
            bucket_start=_DECISION_BUCKET - timedelta(minutes=count - i),
            buy_notional=buy_notional,
            sell_notional=sell_notional,
        )
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
            pump_event_id = await _seed_pump_event(connection, base="OFTEST", ts=_DECISION_TS)
            await _seed_decision_with_outcome(
                connection,
                exchange=exchange,
                base="OFTEST",
                decision_id=decision_id,
                pump_event_id=pump_event_id,
                ts=_DECISION_TS,
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
            # Noise rows at DISTINCT primary keys that any missing predicate
            # would wrongly include. A wrong capture_version (distinct PK) and
            # an on-minute bar (bucket_start = decision minute, distinct PK)
            # must both be excluded.
            await connection.execute(
                _INSERT_BAR,
                _bar_row(
                    exchange=exchange,
                    symbol=native,
                    universe_version=universe_version,
                    capture_version="wrong_capture",
                    bucket_start=_DECISION_BUCKET - timedelta(minutes=1),
                    buy_notional=999999.0,
                    sell_notional=0.0,
                ),
            )
            await connection.execute(
                _INSERT_BAR,
                _bar_row(
                    exchange=exchange,
                    symbol=native,
                    universe_version=universe_version,
                    capture_version="v1",
                    bucket_start=_DECISION_BUCKET,
                    buy_notional=999999.0,
                    sell_notional=0.0,
                ),
            )

        repository = OrderflowMicrostructureRepository(engine)
        _, rows = await repository.fetch(cohort_start=_COHORT_START, cohort_end=_COHORT_END)
        mine = [r for r in rows if r.decision_id == decision_id]
        assert len(mine) == 1
        row = mine[0]
        assert row.outcome_qualified is True
        assert row.match_count == 1
        assert row.native_market_id == native
        assert row.market_type == "linear"
        assert row.bars_10m == 10
        assert row.bars_5m == 5
        assert row.bars_20m == 20
        # wrong-capture + on-minute rows excluded.
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


async def test_two_decisions_of_one_pump_are_a_single_episode() -> None:
    # Review finding 1: counting raw decisions double-counts an episode. The
    # representative (opened-first, then earliest ts) is the only row.
    engine = await _connect_or_skip()
    exchange = f"test_of_{uuid.uuid4().hex[:8]}"
    native = "OFDUPUSDT"
    universe_version = f"uni-{uuid.uuid4().hex[:8]}"
    catalog_version = f"cat-{uuid.uuid4().hex[:8]}"
    first_id = str(uuid.uuid4())
    second_id = str(uuid.uuid4())
    try:
        async with engine.begin() as connection:
            await _seed_snapshot_and_instruments(
                connection,
                exchange=exchange,
                universe_version=universe_version,
                catalog_version=catalog_version,
                native_market_ids=[native],
                base="OFDUP",
            )
            pump_event_id = await _seed_pump_event(connection, base="OFDUP", ts=_DECISION_TS)
            # Two decisions of the SAME pump. Earliest ts is the representative.
            await _seed_decision_with_outcome(
                connection,
                exchange=exchange,
                base="OFDUP",
                decision_id=first_id,
                pump_event_id=pump_event_id,
                ts=_DECISION_TS,
                short_return_pct=3.5,
            )
            await _seed_decision_with_outcome(
                connection,
                exchange=exchange,
                base="OFDUP",
                decision_id=second_id,
                pump_event_id=pump_event_id,
                ts=_DECISION_TS + timedelta(seconds=20),
                short_return_pct=9.9,
            )
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
        repository = OrderflowMicrostructureRepository(engine)
        _, rows = await repository.fetch(cohort_start=_COHORT_START, cohort_end=_COHORT_END)
        mine = [r for r in rows if r.decision_id in {first_id, second_id}]
        assert len(mine) == 1
        assert mine[0].decision_id == first_id
    finally:
        async with engine.begin() as connection:
            await _cleanup(connection, exchange=exchange)
        await engine.dispose()


async def test_outcome_straddling_the_held_out_boundary_is_excluded() -> None:
    # Review finding 2: a decision whose own 60m outcome window ends inside the
    # held-out period must not enter the discovery pass. ts + 60m > cohort_end.
    engine = await _connect_or_skip()
    exchange = f"test_of_{uuid.uuid4().hex[:8]}"
    native = "OFSTRUSDT"
    universe_version = f"uni-{uuid.uuid4().hex[:8]}"
    catalog_version = f"cat-{uuid.uuid4().hex[:8]}"
    decision_id = str(uuid.uuid4())
    straddle_ts = HELD_OUT_START - timedelta(minutes=30)
    try:
        async with engine.begin() as connection:
            await _seed_snapshot_and_instruments(
                connection,
                exchange=exchange,
                universe_version=universe_version,
                catalog_version=catalog_version,
                native_market_ids=[native],
                base="OFSTR",
            )
            pump_event_id = await _seed_pump_event(connection, base="OFSTR", ts=straddle_ts)
            await _seed_decision_with_outcome(
                connection,
                exchange=exchange,
                base="OFSTR",
                decision_id=decision_id,
                pump_event_id=pump_event_id,
                ts=straddle_ts,
                short_return_pct=3.5,
            )
        repository = OrderflowMicrostructureRepository(engine)
        _, rows = await repository.fetch(cohort_start=_COHORT_START, cohort_end=_COHORT_END)
        assert [r for r in rows if r.decision_id == decision_id] == []
    finally:
        async with engine.begin() as connection:
            await _cleanup(connection, exchange=exchange)
        await engine.dispose()


async def test_partial_outcome_does_not_qualify() -> None:
    # Review finding 3: a non-complete outcome (or a cross-exchange fallback)
    # must be coverage loss, not a measured episode.
    engine = await _connect_or_skip()
    exchange = f"test_of_{uuid.uuid4().hex[:8]}"
    native = "OFPARUSDT"
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
                base="OFPAR",
            )
            pump_event_id = await _seed_pump_event(connection, base="OFPAR", ts=_DECISION_TS)
            await _seed_decision_with_outcome(
                connection,
                exchange=exchange,
                base="OFPAR",
                decision_id=decision_id,
                pump_event_id=pump_event_id,
                ts=_DECISION_TS,
                short_return_pct=3.5,
                status="partial",
            )
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
        repository = OrderflowMicrostructureRepository(engine)
        _, rows = await repository.fetch(cohort_start=_COHORT_START, cohort_end=_COHORT_END)
        mine = [r for r in rows if r.decision_id == decision_id]
        assert len(mine) == 1
        assert mine[0].outcome_qualified is False
        assert mine[0].short_return_pct is None
        assert build_coverage(tuple(mine)).measured == ()
    finally:
        async with engine.begin() as connection:
            await _cleanup(connection, exchange=exchange)
        await engine.dispose()


async def test_bar_received_after_decision_is_not_available() -> None:
    # Review finding 4: a closed minute whose data arrived after the decision
    # is look-ahead and must be excluded, so its window is short of ten bars.
    engine = await _connect_or_skip()
    exchange = f"test_of_{uuid.uuid4().hex[:8]}"
    native = "OFLAGUSDT"
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
                base="OFLAG",
            )
            pump_event_id = await _seed_pump_event(connection, base="OFLAG", ts=_DECISION_TS)
            await _seed_decision_with_outcome(
                connection,
                exchange=exchange,
                base="OFLAG",
                decision_id=decision_id,
                pump_event_id=pump_event_id,
                ts=_DECISION_TS,
                short_return_pct=3.5,
            )
            # Ten in-window bars, but the most recent one's last trade was
            # received AFTER the decision (12:00:40 > 12:00:05).
            bars = [
                _bar_row(
                    exchange=exchange,
                    symbol=native,
                    universe_version=universe_version,
                    capture_version="v1",
                    bucket_start=_DECISION_BUCKET - timedelta(minutes=10 - i),
                    buy_notional=25.0,
                    sell_notional=75.0,
                    received_at=(
                        _DECISION_BUCKET + timedelta(seconds=40)
                        if i == 9
                        else _DECISION_BUCKET - timedelta(minutes=10 - i) + timedelta(seconds=59)
                    ),
                )
                for i in range(10)
            ]
            await connection.execute(_INSERT_BAR, bars)
        repository = OrderflowMicrostructureRepository(engine)
        _, rows = await repository.fetch(cohort_start=_COHORT_START, cohort_end=_COHORT_END)
        mine = [r for r in rows if r.decision_id == decision_id]
        assert len(mine) == 1
        assert mine[0].bars_10m == 9
        assert build_coverage(tuple(mine)).measured == ()
    finally:
        async with engine.begin() as connection:
            await _cleanup(connection, exchange=exchange)
        await engine.dispose()


async def test_incomplete_bar_is_excluded() -> None:
    # Review finding 6 (correctness): an incomplete bar (complete=false) inside
    # the window is dropped, so the window is short of ten bars. The fixture
    # must respect the migration CHECK: complete=false when trades_complete is
    # false.
    engine = await _connect_or_skip()
    exchange = f"test_of_{uuid.uuid4().hex[:8]}"
    native = "OFINCUSDT"
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
                base="OFINC",
            )
            pump_event_id = await _seed_pump_event(connection, base="OFINC", ts=_DECISION_TS)
            await _seed_decision_with_outcome(
                connection,
                exchange=exchange,
                base="OFINC",
                decision_id=decision_id,
                pump_event_id=pump_event_id,
                ts=_DECISION_TS,
                short_return_pct=3.5,
            )
            bars = [
                _bar_row(
                    exchange=exchange,
                    symbol=native,
                    universe_version=universe_version,
                    capture_version="v1",
                    bucket_start=_DECISION_BUCKET - timedelta(minutes=10 - i),
                    buy_notional=25.0,
                    sell_notional=75.0,
                    trades_complete=(i != 9),
                )
                for i in range(10)
            ]
            await connection.execute(_INSERT_BAR, bars)
        repository = OrderflowMicrostructureRepository(engine)
        _, rows = await repository.fetch(cohort_start=_COHORT_START, cohort_end=_COHORT_END)
        mine = [r for r in rows if r.decision_id == decision_id]
        assert len(mine) == 1
        assert mine[0].bars_10m == 9
        assert build_coverage(tuple(mine)).measured == ()
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
            pump_event_id = await _seed_pump_event(connection, base="AMBIG", ts=_DECISION_TS)
            await _seed_decision_with_outcome(
                connection,
                exchange=exchange,
                base="AMBIG",
                decision_id=decision_id,
                pump_event_id=pump_event_id,
                ts=_DECISION_TS,
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
