"""Real-Postgres, cross-venue end-to-end test for
feat/momentum-trade-price-source-v1.

Everything upstream of this (momentum.Engine's own AddTrade/
AddTickerObservation, momentumcapture's Writer) is already covered by
apps/collector's own Go tests (momentum_price_source_test.go,
writer_integration_test.go). What is NOT covered anywhere else: that a
row shaped exactly the way the Go writer now persists it -- trade-derived
canonical price fields for a Binance-style bar, ticker-mirrored canonical
price fields for a Bybit-style bar -- actually reads back through
MomentumFlowWatchRepository.load_bucket and clears
prepare_symbol_evaluation's own quality gate (no missing_price, no
stale_quote) on BOTH venues, using the SAME evaluator code path. That is
the actual bug this whole line of work exists to fix (see
docs/research/binance-watch-input-readiness-v1.md): Binance bars had
close_price permanently NULL, so no amount of Go-side or evaluator-unit
testing alone would have caught a wiring mistake connecting the two.

Matches infra/docker/docker-compose.dev.yml's local dev Postgres, same
convention as this session's other real-Postgres tests. Skips (not
fails) when no Postgres is reachable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.momentum_flow_watch_contract import WatchContract
from schurfer_analytics.momentum_flow_watch_evaluator import prepare_symbol_evaluation
from schurfer_analytics.momentum_flow_watch_repository import MomentumFlowWatchRepository
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"

_TEST_EXCHANGE = "test_trade_price_source"
_TEST_CAPTURE_VERSION = "test_capture_v1"
_TEST_MARKET_TYPE = "linear"
_START = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)
_LOOKBACK_MINUTES = 60

_INSERT_BAR_SQL = text("""
    INSERT INTO timeseries.bybit_momentum_bars_1m
        (exchange, market_type, symbol, capture_version, bucket_start,
         universe_version, close_price, buy_total_notional_usd,
         sell_total_notional_usd, buy_hist_counts, buy_hist_notional,
         sell_hist_counts, sell_hist_notional, open_interest,
         open_interest_event_at, open_interest_observed_at,
         ticker_complete, trades_complete, complete,
         price_source, first_price_event_at, last_price_event_at,
         first_price_received_at, last_price_received_at,
         price_observed_this_minute, open_interest_complete, price_complete,
         created_at, payload_hash)
    VALUES
        (:exchange, :market_type, :symbol, :capture_version, :bucket_start,
         'universe-v1', :close_price, :buy_total_notional_usd,
         :sell_total_notional_usd, '{}', '{}', '{}', '{}',
         :open_interest, :open_interest_event_at, :open_interest_observed_at,
         true, true, true,
         :price_source, :first_price_event_at, :last_price_event_at,
         :first_price_received_at, :last_price_received_at,
         true, true, true,
         :created_at, decode(repeat('cd', 32), 'hex'))
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


def _bar_rows(*, symbol: str, price_source: str) -> list[dict[str, object]]:
    """61 consecutive, complete, quality-clean bars -- shaped exactly as
    the Go writer persists them for the given price_source: 'aggregate_trade'
    (Binance -- price fields come from AddTrade) or 'ticker_last' (Bybit --
    price fields mirror AddTickerObservation's own Ticker* fields)."""
    rows: list[dict[str, object]] = []
    for index in range(_LOOKBACK_MINUTES + 1):
        bucket = _START + timedelta(minutes=index)
        rows.append(
            {
                "exchange": _TEST_EXCHANGE,
                "market_type": _TEST_MARKET_TYPE,
                "symbol": symbol,
                "capture_version": _TEST_CAPTURE_VERSION,
                "bucket_start": bucket,
                "close_price": 100.0 + index * 0.01,
                # Nonzero only in the flow window's own last 15 minutes,
                # same convention as the evaluator's own unit-test fixtures:
                # an all-zero flow_notional_15m_usd baseline is itself a
                # separate quality reason (insufficient_flow_baseline),
                # not the thing this test exists to exercise.
                "buy_total_notional_usd": 1_000.0 if index >= 46 else 100.0,
                "sell_total_notional_usd": 200.0 if index >= 46 else 100.0,
                "open_interest": 100.0 + index * 0.1,
                "open_interest_event_at": bucket + timedelta(seconds=30),
                "open_interest_observed_at": bucket + timedelta(seconds=31),
                "price_source": price_source,
                "first_price_event_at": bucket + timedelta(seconds=1),
                "last_price_event_at": bucket + timedelta(seconds=57),
                "first_price_received_at": bucket + timedelta(seconds=2),
                "last_price_received_at": bucket + timedelta(seconds=58),
                "created_at": bucket + timedelta(minutes=1, seconds=2),
            }
        )
    return rows


async def _seed_bars(engine: AsyncEngine, rows: list[dict[str, object]]) -> None:
    async with engine.begin() as connection:
        await connection.execute(_INSERT_BAR_SQL, rows)


async def _cleanup(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM timeseries.bybit_momentum_bars_1m WHERE exchange = :exchange"),
            {"exchange": _TEST_EXCHANGE},
        )


def _contract() -> WatchContract:
    return WatchContract(
        source_exchange=_TEST_EXCHANGE,
        market_type=_TEST_MARKET_TYPE,
        capture_version=_TEST_CAPTURE_VERSION,
        min_cross_section_size=1,
    )


@pytest.mark.parametrize(
    ("symbol", "price_source"),
    [
        ("BINANCESTYLEUSDT", "aggregate_trade"),
        ("BYBITSTYLEUSDT", "ticker_last"),
    ],
)
async def test_trade_and_ticker_price_source_both_clear_the_quality_gate(
    symbol: str, price_source: str
) -> None:
    engine = await _connect_or_skip()
    try:
        rows = _bar_rows(symbol=symbol, price_source=price_source)
        await _seed_bars(engine, rows)

        repository = MomentumFlowWatchRepository(engine)
        contract = _contract()
        last_bucket = rows[-1]["bucket_start"]
        assert isinstance(last_bucket, datetime)
        bucket_input = await repository.load_bucket(contract=contract, bucket_start=last_bucket)

        assert bucket_input is not None
        assert symbol in bucket_input.symbols
        bars = bucket_input.bars_by_symbol[symbol]
        assert len(bars) == _LOOKBACK_MINUTES + 1

        # Every bar's own canonical price fields round-tripped, not just
        # close_price: this is the exact mechanism stale_quote now reads.
        assert all(bar.price_source == price_source for bar in bars)
        assert all(bar.last_price_received_at is not None for bar in bars)
        assert all(bar.price_complete for bar in bars)

        evaluation = prepare_symbol_evaluation(
            symbol=symbol,
            bucket_start=last_bucket,
            bars=bars,
            evaluator_started_at=last_bucket + timedelta(seconds=70),
            contract=contract,
        )

        assert evaluation.quality_reasons == ()
        assert evaluation.quality_ready is True
        assert evaluation.features is not None
    finally:
        await _cleanup(engine)
        await engine.dispose()
