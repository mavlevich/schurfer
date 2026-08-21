"""Real-Postgres coverage for LiquidationCascadeRepository's own LAG-based
window SQL. Only a real query proves the row-position LAG(15) math and the
lag-span gap detection actually hold against the real
`timeseries.bybit_momentum_bars_1m` table -- not just that the text()
statements parse.

Matches infra/docker/docker-compose.dev.yml's local dev Postgres, same
convention as this session's other real-Postgres tests. Skips (not fails)
when no Postgres is reachable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics.liquidation_cascade_repository import LiquidationCascadeRepository
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

if TYPE_CHECKING:
    from schurfer_analytics.liquidation_cascade_grid_search import MinuteObservation


async def _stream_all(
    repository: LiquidationCascadeRepository, **kwargs: object
) -> dict[str, tuple[MinuteObservation, ...]]:
    """Collects the whole `stream_minute_observations` generator into a
    dict -- test convenience only; production code consumes it lazily."""
    result: dict[str, tuple[MinuteObservation, ...]] = {}
    async for symbol, observations in repository.stream_minute_observations(**kwargs):  # type: ignore[arg-type]
        result[symbol] = observations
    return result


TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"

_TEST_EXCHANGE = "test_liq_cascade"
_TEST_CAPTURE_VERSION = "test_capture_v1"
_TEST_MARKET_TYPE = "linear"
_START = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)

_INSERT_BAR_SQL = text("""
    INSERT INTO timeseries.bybit_momentum_bars_1m
        (exchange, market_type, symbol, capture_version, bucket_start,
         universe_version, close_price, open_interest,
         last_bid_price, last_ask_price,
         buy_total_notional_usd, sell_total_notional_usd,
         buy_hist_counts, buy_hist_notional, sell_hist_counts, sell_hist_notional,
         price_complete, open_interest_complete, ticker_complete, trades_complete,
         complete, payload_hash)
    VALUES
        (:exchange, :market_type, :symbol, :capture_version, :bucket_start,
         'universe-v1', :close_price, :open_interest,
         :last_bid_price, :last_ask_price,
         0, 0, '{}', '{}', '{}', '{}',
         :price_complete, :open_interest_complete, true, true,
         :complete, decode(repeat('ef', 32), 'hex'))
    ON CONFLICT DO NOTHING
""")


async def _connect_or_skip() -> AsyncEngine:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"no local postgres reachable: {exc}")
    return engine


async def _seed_bar(
    engine: AsyncEngine,
    *,
    symbol: str,
    bucket_start: datetime,
    close_price: float,
    open_interest: float,
    last_bid_price: float | None = None,
    last_ask_price: float | None = None,
    price_complete: bool = True,
    open_interest_complete: bool = True,
    complete: bool = True,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            _INSERT_BAR_SQL,
            {
                "exchange": _TEST_EXCHANGE,
                "market_type": _TEST_MARKET_TYPE,
                "symbol": symbol,
                "capture_version": _TEST_CAPTURE_VERSION,
                "bucket_start": bucket_start,
                "close_price": close_price,
                "open_interest": open_interest,
                "last_bid_price": last_bid_price,
                "last_ask_price": last_ask_price,
                "price_complete": price_complete,
                "open_interest_complete": open_interest_complete,
                "complete": complete,
            },
        )


async def _cleanup(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM timeseries.bybit_momentum_bars_1m WHERE exchange = :exchange"),
            {"exchange": _TEST_EXCHANGE},
        )


async def test_fetch_minute_observations_computes_the_real_15_row_lag_drop() -> None:
    engine = await _connect_or_skip()
    try:
        symbol = "LAGDROPUSDT"
        # Minute 0: baseline. Minutes 1..14: flat filler so the LAG(15) row
        # for minute 15 is genuinely minute 0. Minute 15: a real cascade
        # candidate -- price down 6%, OI down 20% versus minute 0.
        await _seed_bar(
            engine, symbol=symbol, bucket_start=_START, close_price=100.0, open_interest=1000.0
        )
        for i in range(1, 15):
            await _seed_bar(
                engine,
                symbol=symbol,
                bucket_start=_START + timedelta(minutes=i),
                close_price=100.0,
                open_interest=1000.0,
            )
        await _seed_bar(
            engine,
            symbol=symbol,
            bucket_start=_START + timedelta(minutes=15),
            close_price=94.0,
            open_interest=800.0,
        )

        repository = LiquidationCascadeRepository(engine)
        streamed = await _stream_all(
            repository,
            exchange=_TEST_EXCHANGE,
            since=_START,
            until=_START + timedelta(minutes=20),
            capture_version=_TEST_CAPTURE_VERSION,
        )
        observations = streamed[symbol]
        by_bucket = {obs.bucket_start: obs for obs in observations}
        target = by_bucket[_START + timedelta(minutes=15)]
        assert target.price_drop_pct == pytest.approx(-0.06)
        assert target.oi_drop_pct == pytest.approx(-0.20)
        assert target.price_complete is True
        assert target.open_interest_complete is True
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_fetch_minute_observations_flags_a_gap_widened_lag_as_incomplete() -> None:
    engine = await _connect_or_skip()
    try:
        symbol = "GAPLAGUSDT"
        # Minute 0, then a 5-minute gap (minutes 1..5 never written), then
        # 15 consecutive bars (minutes 6..20). Minute 20 is the 15th ROW
        # after minute 0 in this partition (0, 6, 7, ..., 20 = 16 rows,
        # minute 20 at row-index 15), so LAG(15) resolves to minute 0 --
        # correct row position, but 20 real minutes back, not 15.
        await _seed_bar(
            engine, symbol=symbol, bucket_start=_START, close_price=100.0, open_interest=1000.0
        )
        for i in range(6, 21):
            await _seed_bar(
                engine,
                symbol=symbol,
                bucket_start=_START + timedelta(minutes=i),
                close_price=100.0 if i < 20 else 94.0,
                open_interest=1000.0 if i < 20 else 800.0,
            )

        repository = LiquidationCascadeRepository(engine)
        streamed = await _stream_all(
            repository,
            exchange=_TEST_EXCHANGE,
            since=_START,
            until=_START + timedelta(minutes=30),
            capture_version=_TEST_CAPTURE_VERSION,
        )
        observations = streamed[symbol]
        by_bucket = {obs.bucket_start: obs for obs in observations}
        target = by_bucket[_START + timedelta(minutes=20)]
        # The row-position LAG(15) still resolves (there are 15 real rows
        # before it), so the row is NOT dropped -- but the gap makes its
        # real span 20 minutes, not 15, and both completeness flags must
        # fold that in rather than reporting the raw bar-level flags
        # unchanged.
        assert target.price_drop_pct == pytest.approx(-0.06)
        assert target.oi_drop_pct == pytest.approx(-0.20)
        assert target.price_complete is False
        assert target.open_interest_complete is False
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_stream_minute_observations_groups_each_symbol_complete_and_in_order() -> None:
    # Regression for the streaming mechanism itself (colleague review,
    # 2026-08-21): the whole point of ordering by (exchange, symbol,
    # bucket_start) and yielding on symbol change is that each symbol's
    # tuple is complete and correctly ordered by the time it is yielded --
    # not split across two yields, and never mixed with another symbol's
    # rows.
    engine = await _connect_or_skip()
    try:
        # 15 minutes of flat lookback history per symbol so LAG(15)
        # resolves for the in-window rows below.
        for symbol, base_price, base_oi in (("AUSDT", 100.0, 1000.0), ("BUSDT", 200.0, 2000.0)):
            for i in range(15):
                await _seed_bar(
                    engine,
                    symbol=symbol,
                    bucket_start=_START - timedelta(minutes=15 - i),
                    close_price=base_price,
                    open_interest=base_oi,
                )
        for i in range(3):
            await _seed_bar(
                engine,
                symbol="AUSDT",
                bucket_start=_START + timedelta(minutes=i),
                close_price=100.0 + i,
                open_interest=1000.0,
            )
        for i in range(2):
            await _seed_bar(
                engine,
                symbol="BUSDT",
                bucket_start=_START + timedelta(minutes=i),
                close_price=200.0 + i,
                open_interest=2000.0,
            )

        repository = LiquidationCascadeRepository(engine)
        yielded: list[tuple[str, int]] = []
        streamed: dict[str, tuple[MinuteObservation, ...]] = {}
        async for symbol, observations in repository.stream_minute_observations(
            exchange=_TEST_EXCHANGE,
            since=_START,
            until=_START + timedelta(minutes=5),
            capture_version=_TEST_CAPTURE_VERSION,
        ):
            yielded.append((symbol, len(observations)))
            streamed[symbol] = observations

        assert yielded == [("AUSDT", 3), ("BUSDT", 2)]
        assert [obs.bucket_start for obs in streamed["AUSDT"]] == [
            _START,
            _START + timedelta(minutes=1),
            _START + timedelta(minutes=2),
        ]
        assert [obs.price_drop_pct for obs in streamed["BUSDT"]] == [
            pytest.approx(0.0),
            pytest.approx(0.005),
        ]
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_fetch_outcome_path_is_exact_symbol_and_timestamp() -> None:
    engine = await _connect_or_skip()
    try:
        symbol = "OUTCOMEUSDT"
        await _seed_bar(
            engine, symbol=symbol, bucket_start=_START, close_price=100.0, open_interest=1000.0
        )
        await _seed_bar(
            engine,
            symbol=symbol,
            bucket_start=_START + timedelta(minutes=1),
            close_price=101.0,
            open_interest=1000.0,
        )
        # A different symbol at the same timestamps must never leak in.
        await _seed_bar(
            engine,
            symbol="OTHERUSDT",
            bucket_start=_START + timedelta(minutes=1),
            close_price=9999.0,
            open_interest=1000.0,
        )

        repository = LiquidationCascadeRepository(engine)
        bars = await repository.fetch_outcome_path(
            exchange=_TEST_EXCHANGE,
            symbol=symbol,
            since=_START,
            until=_START + timedelta(minutes=5),
            capture_version=_TEST_CAPTURE_VERSION,
        )
        assert [bar.bucket_start for bar in bars] == [_START, _START + timedelta(minutes=1)]
        assert bars[1].close_price == pytest.approx(101.0)
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_fetch_bars_and_quotes_for_symbols_are_bulk_and_exact() -> None:
    engine = await _connect_or_skip()
    try:
        await _seed_bar(
            engine,
            symbol="AUSDT",
            bucket_start=_START,
            close_price=100.0,
            open_interest=1000.0,
            last_bid_price=99.9,
            last_ask_price=100.1,
        )
        await _seed_bar(
            engine,
            symbol="BUSDT",
            bucket_start=_START,
            close_price=200.0,
            open_interest=2000.0,
            last_bid_price=199.5,
            last_ask_price=200.5,
        )
        # A symbol not in the requested list must never leak into either
        # bulk result.
        await _seed_bar(
            engine, symbol="CUSDT", bucket_start=_START, close_price=9999.0, open_interest=1.0
        )

        repository = LiquidationCascadeRepository(engine)
        bars = await repository.fetch_bars_for_symbols(
            exchange=_TEST_EXCHANGE,
            symbols=["AUSDT", "BUSDT"],
            since=_START,
            until=_START + timedelta(minutes=1),
            capture_version=_TEST_CAPTURE_VERSION,
        )
        assert set(bars) == {"AUSDT", "BUSDT"}
        assert bars["AUSDT"][0].close_price == pytest.approx(100.0)
        assert bars["BUSDT"][0].close_price == pytest.approx(200.0)

        quotes = await repository.fetch_quotes_for_symbols(
            exchange=_TEST_EXCHANGE,
            symbols=["AUSDT", "BUSDT"],
            since=_START,
            until=_START + timedelta(minutes=1),
            capture_version=_TEST_CAPTURE_VERSION,
        )
        assert quotes[("AUSDT", _START)].last_bid_price == pytest.approx(99.9)
        assert quotes[("BUSDT", _START)].last_ask_price == pytest.approx(200.5)
        assert ("CUSDT", _START) not in quotes
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_capture_version_is_pinned_and_never_interleaves_rows() -> None:
    # Regression (colleague review, 2026-08-21): the table's own primary
    # key includes capture_version, so a bare query without pinning it
    # could see more than one row per (exchange, symbol, bucket_start) once
    # a capture-version bump has happened, corrupting LAG(15) partitioning
    # and quote lookups non-deterministically.
    engine = await _connect_or_skip()
    try:
        symbol = "DUALVERSIONUSDT"
        await _seed_bar(
            engine,
            symbol=symbol,
            bucket_start=_START,
            close_price=100.0,
            open_interest=1000.0,
            last_bid_price=99.0,
            last_ask_price=101.0,
        )
        async with engine.begin() as connection:
            await connection.execute(
                _INSERT_BAR_SQL,
                {
                    "exchange": _TEST_EXCHANGE,
                    "market_type": _TEST_MARKET_TYPE,
                    "symbol": symbol,
                    "capture_version": "test_capture_v2",
                    "bucket_start": _START,
                    "close_price": 500.0,
                    "open_interest": 5000.0,
                    "last_bid_price": 499.0,
                    "last_ask_price": 501.0,
                    "price_complete": True,
                    "open_interest_complete": True,
                    "complete": True,
                },
            )

        repository = LiquidationCascadeRepository(engine)
        bars = await repository.fetch_bars_for_symbols(
            exchange=_TEST_EXCHANGE,
            symbols=[symbol],
            since=_START,
            until=_START + timedelta(minutes=1),
            capture_version=_TEST_CAPTURE_VERSION,
        )
        assert len(bars[symbol]) == 1
        assert bars[symbol][0].close_price == pytest.approx(100.0)

        quotes = await repository.fetch_quotes_for_symbols(
            exchange=_TEST_EXCHANGE,
            symbols=[symbol],
            since=_START,
            until=_START + timedelta(minutes=1),
            capture_version=_TEST_CAPTURE_VERSION,
        )
        assert len(quotes) == 1
        assert quotes[(symbol, _START)].last_bid_price == pytest.approx(99.0)
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_fetch_symbols_in_window_lists_distinct_symbols_only() -> None:
    engine = await _connect_or_skip()
    try:
        await _seed_bar(
            engine, symbol="AUSDT", bucket_start=_START, close_price=100.0, open_interest=1000.0
        )
        await _seed_bar(
            engine,
            symbol="AUSDT",
            bucket_start=_START + timedelta(minutes=1),
            close_price=100.0,
            open_interest=1000.0,
        )
        await _seed_bar(
            engine, symbol="BUSDT", bucket_start=_START, close_price=200.0, open_interest=2000.0
        )

        repository = LiquidationCascadeRepository(engine)
        symbols = await repository.fetch_symbols_in_window(
            exchange=_TEST_EXCHANGE,
            since=_START,
            until=_START + timedelta(minutes=5),
            capture_version=_TEST_CAPTURE_VERSION,
        )
        assert symbols == ("AUSDT", "BUSDT")
    finally:
        await _cleanup(engine)
        await engine.dispose()


_INSERT_SNAPSHOT_SQL = text("""
    INSERT INTO app.momentum_universe_snapshots
        (exchange, universe_version, catalog_version, capture_version, schema_version,
         captured_at, instrument_count, payload_hash)
    VALUES
        (:exchange, :universe_version, :catalog_version, 'capture-v1', 'schema-v1',
         :captured_at, :instrument_count, decode(repeat('ab', 32), 'hex'))
    ON CONFLICT DO NOTHING
""")

_INSERT_INSTRUMENT_SQL = text("""
    INSERT INTO app.momentum_universe_instruments
        (exchange, universe_version, catalog_version, native_market_id, base, quote, settle,
         native_market_type, canonical_market_type, onboarded_at, identity_status, identity_key,
         metadata_hash)
    VALUES
        (:exchange, :universe_version, :catalog_version, :native_market_id, :base, 'USDT', 'USDT',
         'LinearPerpetual', 'linear', :onboarded_at, :identity_status, :identity_key,
         decode(repeat('cd', 32), 'hex'))
    ON CONFLICT DO NOTHING
""")

_IDENTITY_TEST_EXCHANGE = "test_liq_cascade_identity"


async def _seed_snapshot(
    engine: AsyncEngine, *, catalog_version: str, captured_at: datetime, instrument_count: int = 1
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            _INSERT_SNAPSHOT_SQL,
            {
                "exchange": _IDENTITY_TEST_EXCHANGE,
                "universe_version": "universe-v1",
                "catalog_version": catalog_version,
                "captured_at": captured_at,
                "instrument_count": instrument_count,
            },
        )


async def _seed_instrument(
    engine: AsyncEngine,
    *,
    catalog_version: str,
    native_market_id: str,
    onboarded_at: datetime | None,
    identity_status: str = "ready",
    identity_key: str | None = "key-a",
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            _INSERT_INSTRUMENT_SQL,
            {
                "exchange": _IDENTITY_TEST_EXCHANGE,
                "universe_version": "universe-v1",
                "catalog_version": catalog_version,
                "native_market_id": native_market_id,
                "base": native_market_id.removesuffix("USDT"),
                "onboarded_at": onboarded_at,
                "identity_status": identity_status,
                "identity_key": identity_key,
            },
        )


async def _cleanup_identity(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM app.momentum_universe_snapshots WHERE exchange = :exchange"),
            {"exchange": _IDENTITY_TEST_EXCHANGE},
        )


async def test_fetch_identity_lookup_includes_a_baseline_snapshot_before_since() -> None:
    # Regression (colleague review, 2026-08-21): the specific failure a real
    # smoke run hit -- a snapshot captured well before `since`, with no
    # snapshot at all landing inside [since, until), must still come back
    # as the baseline. A bare `captured_at < until` scan technically
    # includes it too, but so would it include ANY older snapshot; the
    # real assertion here is that `relevant_snapshot_timestamps` is
    # EXACTLY the one baseline, not an unbounded history.
    engine = await _connect_or_skip()
    try:
        since = datetime(2026, 8, 20, tzinfo=UTC)
        until = datetime(2026, 8, 27, tzinfo=UTC)
        baseline_at = datetime(2026, 8, 15, tzinfo=UTC)
        # An unrelated, much older snapshot that must NOT be pulled in.
        ancient_at = datetime(2026, 1, 1, tzinfo=UTC)
        await _seed_snapshot(engine, catalog_version="ancient", captured_at=ancient_at)
        await _seed_snapshot(engine, catalog_version="baseline", captured_at=baseline_at)
        await _seed_instrument(
            engine,
            catalog_version="baseline",
            native_market_id="AUSDT",
            onboarded_at=baseline_at,
        )

        repository = LiquidationCascadeRepository(engine)
        lookup = await repository.fetch_identity_lookup(
            exchange=_IDENTITY_TEST_EXCHANGE, symbols=["AUSDT"], since=since, until=until
        )
        assert lookup.relevant_snapshot_timestamps == (baseline_at,)
        assert len(lookup.observations) == 1
        assert lookup.observations[0].native_market_id == "AUSDT"
        assert lookup.observations[0].captured_at == baseline_at
    finally:
        await _cleanup_identity(engine)
        await engine.dispose()


async def test_fetch_identity_lookup_includes_baseline_plus_in_window_changes() -> None:
    engine = await _connect_or_skip()
    try:
        since = datetime(2026, 8, 20, tzinfo=UTC)
        until = datetime(2026, 8, 27, tzinfo=UTC)
        baseline_at = datetime(2026, 8, 15, tzinfo=UTC)
        in_window_at = datetime(2026, 8, 22, tzinfo=UTC)
        # After `until` -- must never be pulled in.
        after_until_at = datetime(2026, 8, 28, tzinfo=UTC)
        await _seed_snapshot(engine, catalog_version="baseline", captured_at=baseline_at)
        await _seed_snapshot(engine, catalog_version="in-window", captured_at=in_window_at)
        await _seed_snapshot(engine, catalog_version="after-until", captured_at=after_until_at)
        await _seed_instrument(
            engine, catalog_version="baseline", native_market_id="AUSDT", onboarded_at=baseline_at
        )
        await _seed_instrument(
            engine, catalog_version="in-window", native_market_id="AUSDT", onboarded_at=baseline_at
        )
        await _seed_instrument(
            engine,
            catalog_version="after-until",
            native_market_id="AUSDT",
            onboarded_at=baseline_at,
        )

        repository = LiquidationCascadeRepository(engine)
        lookup = await repository.fetch_identity_lookup(
            exchange=_IDENTITY_TEST_EXCHANGE, symbols=["AUSDT"], since=since, until=until
        )
        assert lookup.relevant_snapshot_timestamps == (baseline_at, in_window_at)
        assert {obs.captured_at for obs in lookup.observations} == {baseline_at, in_window_at}
    finally:
        await _cleanup_identity(engine)
        await engine.dispose()
