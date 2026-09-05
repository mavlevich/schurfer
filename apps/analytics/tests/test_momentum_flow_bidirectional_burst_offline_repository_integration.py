"""Real-Postgres coverage for OfflineBarsExtractRepository and the DuckDB
offline burst query (research/cex-activity-offline-denominator-v1).

The one thing worth proving here beyond "the SQL parses": the offline path
(Postgres extract -> Parquet -> DuckDB window query) computes EXACTLY the
same `BurstMinute` tuples as the existing live path
(`MomentumFlowBidirectionalBurstRepository.fetch_candidate_extreme_minutes`,
a single RANGE-window query against live Postgres) on identical seeded
input. An offline replica that quietly disagrees with the query it exists to
replace is worse than no replica at all -- it would make HYP-016 answerable
again while silently changing the answer. Colleague review, 2026-09-03:
the original version of this file only exercised one symbol, one output
minute, and one chunk -- multi-day boundary, a real gap, two exchange/
capture-version partitions, and the extreme-threshold boundary itself are
each their own test below, plus the provenance checks
`fetch_candidate_extreme_minutes_offline` gained in that same review round.

Matches infra/docker/docker-compose.dev.yml's local dev Postgres, same
convention as the other real-Postgres tests in this package. Skips (not
fails) when no Postgres is reachable.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from schurfer_analytics.momentum_flow_bidirectional_burst_offline_repository import (
    ExtractManifest,
    OfflineBarsExtractRepository,
    fetch_candidate_extreme_minutes_offline,
)
from schurfer_analytics.momentum_flow_bidirectional_burst_repository import (
    MomentumFlowBidirectionalBurstRepository,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

TEST_DATABASE_URL = "postgresql+psycopg://schurfer:schurfer_dev@localhost:5432/schurfer"

_TEST_EXCHANGE = "test_bidir_burst_offline"
_TEST_CAPTURE_VERSION = "test_capture_v1"
_TEST_MARKET_TYPE = "linear"
_START = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)

_INSERT_BAR_SQL = text("""
    INSERT INTO timeseries.bybit_momentum_bars_1m
        (exchange, market_type, symbol, capture_version, bucket_start,
         universe_version, close_price, buy_total_notional_usd, sell_total_notional_usd,
         buy_hist_counts, buy_hist_notional, sell_hist_counts, sell_hist_notional,
         ticker_complete, trades_complete, complete, payload_hash)
    VALUES
        (:exchange, :market_type, :symbol, :capture_version, :bucket_start,
         'universe-v1', :close_price, :buy_total_notional_usd, :sell_total_notional_usd,
         '{}', '{}', '{}', '{}',
         true, true, true, decode(repeat('ef', 32), 'hex'))
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
    buy_notional: float = 0.0,
    sell_notional: float = 0.0,
    exchange: str = _TEST_EXCHANGE,
    capture_version: str = _TEST_CAPTURE_VERSION,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            _INSERT_BAR_SQL,
            {
                "exchange": exchange,
                "market_type": _TEST_MARKET_TYPE,
                "symbol": symbol,
                "capture_version": capture_version,
                "bucket_start": bucket_start,
                "close_price": close_price,
                "buy_total_notional_usd": buy_notional,
                "sell_total_notional_usd": sell_notional,
            },
        )


async def _cleanup(engine: AsyncEngine, *, exchange: str = _TEST_EXCHANGE) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM timeseries.bybit_momentum_bars_1m WHERE exchange = :exchange"),
            {"exchange": exchange},
        )


async def _seed_gapless_burst_window(
    engine: AsyncEngine,
    *,
    symbol: str,
    target: datetime,
    burst_buy: float = 50_000.0,
    exchange: str = _TEST_EXCHANGE,
) -> None:
    """Same shape as the live-path positive test: 1435 flat baseline
    minutes, then a 5-minute one-sided buy burst ending at `target`."""
    baseline_minutes = 1435
    burst_minutes = 5
    for i in range(baseline_minutes):
        await _seed_bar(
            engine,
            symbol=symbol,
            bucket_start=target - timedelta(minutes=baseline_minutes + burst_minutes - 1 - i),
            close_price=1.0,
            buy_notional=100.0,
            sell_notional=100.0,
            exchange=exchange,
        )
    for i in range(burst_minutes):
        await _seed_bar(
            engine,
            symbol=symbol,
            bucket_start=target - timedelta(minutes=burst_minutes - 1 - i),
            close_price=2.0,
            buy_notional=burst_buy,
            sell_notional=0.0,
            exchange=exchange,
        )


async def test_extract_bars_to_parquet_writes_readable_rows(tmp_path: Path) -> None:
    engine = await _connect_or_skip()
    try:
        symbol = "EXTRACTUSDT"
        await _seed_bar(
            engine, symbol=symbol, bucket_start=_START, close_price=1.5, buy_notional=10.0
        )
        await _seed_bar(
            engine,
            symbol=symbol,
            bucket_start=_START + timedelta(minutes=1),
            close_price=1.6,
            buy_notional=20.0,
        )

        repository = OfflineBarsExtractRepository(engine)
        manifest = await repository.extract_bars_to_parquet(
            exchange=_TEST_EXCHANGE,
            capture_version=_TEST_CAPTURE_VERSION,
            market_type=_TEST_MARKET_TYPE,
            since=_START,
            until=_START + timedelta(minutes=2),
            output_path=tmp_path / "bars.parquet",
        )
        assert manifest.row_count == 2
        assert manifest.symbol_count == 1
        assert manifest.wall_seconds >= 0.0
        assert manifest.peak_rss_mb > 0.0
        assert Path(manifest.parquet_path).exists()
        # The temp on-disk DuckDB build file must not survive a successful run.
        assert not Path(manifest.parquet_path).with_suffix(".parquet.build.duckdb").exists()

        import duckdb

        connection = duckdb.connect(":memory:")
        try:
            rows = connection.execute(
                "SELECT symbol, bucket_start, close_price, buy_total_notional_usd "
                "FROM read_parquet(?) ORDER BY bucket_start",
                [manifest.parquet_path],
            ).fetchall()
        finally:
            connection.close()
        assert [row[0] for row in rows] == [symbol, symbol]
        assert rows[0][2] == pytest.approx(1.5)
        assert rows[1][3] == pytest.approx(20.0)
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_from_url_repository_can_be_closed() -> None:
    """Colleague review, 2026-09-04 (research/cex-activity-discovery-
    completion-v1 wiring in the offline denominator): from_url() creates
    its own engine, and until this fix there was no way to dispose it --
    every other repository in this codebase exposes close() for exactly
    this. SQLAlchemy engines connect lazily, so neither from_url() nor
    close() needs a real Postgres to be reachable -- this always runs,
    unlike the rest of this file's own _connect_or_skip-gated tests."""
    repository = OfflineBarsExtractRepository.from_url(TEST_DATABASE_URL)
    await repository.close()


async def test_extract_bars_to_parquet_rejects_since_after_until(tmp_path: Path) -> None:
    engine = await _connect_or_skip()
    try:
        repository = OfflineBarsExtractRepository(engine)
        now = datetime.now(UTC)
        with pytest.raises(ValueError, match="since must be earlier"):
            await repository.extract_bars_to_parquet(
                exchange=_TEST_EXCHANGE,
                capture_version=_TEST_CAPTURE_VERSION,
                since=now,
                until=now - timedelta(minutes=1),
                output_path=tmp_path / "bars.parquet",
            )
    finally:
        await engine.dispose()


async def test_extract_bars_to_parquet_rejects_over_row_cap(tmp_path: Path) -> None:
    engine = await _connect_or_skip()
    try:
        symbol = "OVERCAPUSDT"
        await _seed_bar(engine, symbol=symbol, bucket_start=_START, close_price=1.0)
        await _seed_bar(
            engine, symbol=symbol, bucket_start=_START + timedelta(minutes=1), close_price=1.0
        )
        repository = OfflineBarsExtractRepository(engine)
        with pytest.raises(ValueError, match="over max_extract_rows"):
            await repository.extract_bars_to_parquet(
                exchange=_TEST_EXCHANGE,
                capture_version=_TEST_CAPTURE_VERSION,
                since=_START,
                until=_START + timedelta(minutes=2),
                output_path=tmp_path / "bars.parquet",
                max_extract_rows=1,
            )
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_extract_bars_to_parquet_cleans_up_build_artifacts_after_a_mid_copy_failure(
    tmp_path: Path,
) -> None:
    """Colleague review, 2026-09-05 follow-up round 2: the row-cap/deadline
    check that raises DURING a chunk's own PostgreSQL COPY stream (as
    `test_extract_bars_to_parquet_rejects_over_row_cap` above already
    exercises) happens before that chunk's own COPY-phase code ever
    reaches the point where the DuckDB phase -- and its own cleanup --
    would start. An earlier version of this function only cleaned up
    `tmp_csv_dir`/`tmp_spill_dir`/`tmp_duckdb_path` from the DuckDB
    phase's own `finally` block, which a COPY-phase failure never reaches
    -- leaving the already-created, already partly-written CSV build
    directory on disk indefinitely. The fix wraps the WHOLE function body
    in one outer `finally` instead; this test proves it by triggering the
    exact same over-row-cap failure mid-COPY and then asserting none of
    the `.build.*` paths (or a dangling `.tmp` Parquet file) survive it."""
    engine = await _connect_or_skip()
    try:
        symbol = "MIDCOPYCLEANUPUSDT"
        await _seed_bar(engine, symbol=symbol, bucket_start=_START, close_price=1.0)
        await _seed_bar(
            engine, symbol=symbol, bucket_start=_START + timedelta(minutes=1), close_price=1.0
        )
        repository = OfflineBarsExtractRepository(engine)
        output_path = tmp_path / "bars.parquet"
        with pytest.raises(ValueError, match="over max_extract_rows"):
            await repository.extract_bars_to_parquet(
                exchange=_TEST_EXCHANGE,
                capture_version=_TEST_CAPTURE_VERSION,
                since=_START,
                until=_START + timedelta(minutes=2),
                output_path=output_path,
                max_extract_rows=1,
            )
        assert not output_path.exists()
        assert not output_path.with_suffix(".parquet.tmp").exists()
        assert not output_path.with_suffix(".parquet.build.duckdb").exists()
        assert not output_path.with_suffix(".parquet.build.spill").exists()
        assert not output_path.with_suffix(".parquet.build.csv").exists()
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_extract_bars_to_parquet_rejects_over_wall_time_budget(tmp_path: Path) -> None:
    """No single chunk's own statement_timeout alone catches a runaway
    TOTAL extract across many chunks (colleague review, 2026-09-03) --
    max_wall_seconds=0.0 means the deadline is already in the past by the
    time the very first check runs, so this must raise immediately."""
    engine = await _connect_or_skip()
    try:
        symbol = "WALLBUDGETUSDT"
        await _seed_bar(engine, symbol=symbol, bucket_start=_START, close_price=1.0)
        repository = OfflineBarsExtractRepository(engine)
        with pytest.raises(TimeoutError, match="max_wall_seconds"):
            await repository.extract_bars_to_parquet(
                exchange=_TEST_EXCHANGE,
                capture_version=_TEST_CAPTURE_VERSION,
                since=_START,
                until=_START + timedelta(minutes=1),
                output_path=tmp_path / "bars.parquet",
                max_wall_seconds=0.0,
            )
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_extract_bars_to_parquet_enforces_the_deadline_after_chunk_work_finishes(
    tmp_path: Path,
) -> None:
    """The wall-time budget must be enforced beyond just the pre-chunk
    check -- also periodically during a chunk's own row stream, after each
    chunk finishes, and before the local bulk-load/COPY/hash phases
    (colleague review, 2026-09-03 follow-up round 3, preserved through the
    2026-09-05 COPY/CSV redesign: a per-chunk-only check misses a single
    slow/stuck chunk or a slow local phase, since those would only ever be
    checked again at the START of the NEXT chunk, which may never come).

    Uses a fake `_now` clock (the function's own private testing seam) that
    returns small, real-looking values for its first few calls -- enough to
    let real chunk/row processing actually happen (the one seeded row's own
    COPY stream, at minimum) -- then jumps far past the deadline on every
    call after that. If the deadline were only ever checked before a chunk
    starts, a scenario this small (one row, two chunks -- the extract
    always adds one chunk for the 24h lookback ahead of the requested
    since/until) could complete successfully before the jump is ever
    observed; the assertion below only requires that a TimeoutError is
    eventually raised and that more than a few real clock calls preceded
    it, not which exact checkpoint catches it -- pinning the exact call
    count would make this test brittle against any future change to how
    many times a single chunk's processing calls `_now`."""
    engine = await _connect_or_skip()
    try:
        symbol = "DEADLINEAFTERUSDT"
        await _seed_bar(engine, symbol=symbol, bucket_start=_START, close_price=1.0)
        repository = OfflineBarsExtractRepository(engine)

        real_start = time.monotonic()
        call_count = 0

        def fake_now() -> float:
            nonlocal call_count
            call_count += 1
            # Generous slack beyond the two pre-chunk checks + the one
            # post-batch check this scenario makes (3 calls): if a future
            # refactor adds a couple more checkpoints before the chunk loop
            # ends, this still exercises the same "checked after chunk work
            # finishes" property rather than becoming flaky.
            if call_count <= 5:
                return real_start + call_count * 0.001
            return real_start + 10_000.0

        with pytest.raises(TimeoutError, match="max_wall_seconds"):
            await repository.extract_bars_to_parquet(
                exchange=_TEST_EXCHANGE,
                capture_version=_TEST_CAPTURE_VERSION,
                since=_START,
                until=_START + timedelta(minutes=1),
                output_path=tmp_path / "bars.parquet",
                max_wall_seconds=5.0,
                _now=fake_now,
            )
        # Sanity: the fake clock's early, real-looking values must actually
        # have been consumed (proving this test exercised real chunk/batch
        # processing before the jump, not an immediate pre-first-chunk
        # failure the way test_extract_bars_to_parquet_rejects_over_wall_
        # time_budget above already covers).
        assert call_count > 3
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_extract_bars_to_parquet_spans_multiple_day_chunks(tmp_path: Path) -> None:
    """since/until spans 3 calendar days -> candidate_query_windows splits
    the fetch into multiple non-overlapping chunks; every row from every
    chunk must land in the one finished Parquet file."""
    engine = await _connect_or_skip()
    try:
        symbol = "MULTIDAYUSDT"
        since = _START
        until = _START + timedelta(days=2, hours=3)
        # One bar per day, spread across the 3-day span (well inside since/until).
        seeded_at = [since, since + timedelta(days=1), since + timedelta(days=1, hours=12)]
        for bucket_start in seeded_at:
            await _seed_bar(engine, symbol=symbol, bucket_start=bucket_start, close_price=1.0)

        repository = OfflineBarsExtractRepository(engine)
        manifest = await repository.extract_bars_to_parquet(
            exchange=_TEST_EXCHANGE,
            capture_version=_TEST_CAPTURE_VERSION,
            since=since,
            until=until,
            output_path=tmp_path / "multiday.parquet",
        )
        assert manifest.row_count == len(seeded_at)

        import duckdb

        connection = duckdb.connect(":memory:")
        try:
            timestamps = connection.execute(
                "SELECT bucket_start FROM read_parquet(?) ORDER BY bucket_start",
                [manifest.parquet_path],
            ).fetchall()
        finally:
            connection.close()
        assert len(timestamps) == len(seeded_at)
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_offline_query_matches_live_query_on_identical_seeded_data(tmp_path: Path) -> None:
    engine = await _connect_or_skip()
    try:
        symbol = "PARITYUSDT"
        target = _START
        await _seed_gapless_burst_window(engine, symbol=symbol, target=target)

        since = target
        until = target + timedelta(minutes=1)
        min_volume_24h_usd = 1.0
        extreme_threshold_pct = 10.0

        live_repository = MomentumFlowBidirectionalBurstRepository(engine)
        live_minutes = await live_repository.fetch_candidate_extreme_minutes(
            exchange=_TEST_EXCHANGE,
            capture_version=_TEST_CAPTURE_VERSION,
            market_type=_TEST_MARKET_TYPE,
            since=since,
            until=until,
            min_volume_24h_usd=min_volume_24h_usd,
            extreme_threshold_pct=extreme_threshold_pct,
        )
        assert len(live_minutes) == 1  # sanity: the seeded burst is real

        extract_repository = OfflineBarsExtractRepository(engine)
        manifest = await extract_repository.extract_bars_to_parquet(
            exchange=_TEST_EXCHANGE,
            capture_version=_TEST_CAPTURE_VERSION,
            market_type=_TEST_MARKET_TYPE,
            since=since,
            until=until,
            output_path=tmp_path / "parity.parquet",
        )
        offline_minutes = fetch_candidate_extreme_minutes_offline(
            manifest,
            since=since,
            until=until,
            min_volume_24h_usd=min_volume_24h_usd,
            extreme_threshold_pct=extreme_threshold_pct,
        )

        assert offline_minutes == live_minutes
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_offline_extract_matches_live_query_against_a_compressed_chunk(
    tmp_path: Path,
) -> None:
    """Colleague review, 2026-09-04, real-production incident: this
    module's own docstring benchmark, and every OTHER test in this file,
    seed fresh rows and extract them in the same test -- by construction,
    TimescaleDB's own `add_compression_policy(..., INTERVAL '1 day')`
    (migration 0024) never has a chance to apply, so none of them ever
    exercise the one condition every real HYP-016 production run will
    always hit: querying an ALREADY-COMPRESSED chunk. Confirmed against
    real production data the same day this test was added: a raw
    `SELECT count(*)` against a compressed day answered in 0.27s and a
    real `EXPLAIN (ANALYZE, ...)` of the actual extract SELECT against
    that same compressed day measured 1.36s -- compression itself was
    NOT the production bottleneck found that day (the batch-insert
    transaction pattern in `extract_bars_to_parquet` was), but nothing
    before this test ever actually proved that by running the offline
    path against genuinely compressed storage. This seeds a day of data,
    explicitly compresses that chunk via `compress_chunk()` (not relying
    on the background compression job's own schedule, which would make
    this test non-deterministic), and proves the offline extract still
    agrees bit-for-bit with the live path -- not just that it runs
    without error."""
    engine = await _connect_or_skip()
    exchange = "test_bidir_offline_compressed"
    try:
        await _cleanup(engine, exchange=exchange)
        symbol = "COMPRESSEDPARITYUSDT"
        target = _START
        await _seed_gapless_burst_window(engine, symbol=symbol, target=target, exchange=exchange)

        # Explicitly compress the chunk covering target's own calendar day.
        # Chunks partition purely by time (not by exchange), so this also
        # compresses any other exchange's rows sharing that day -- safe,
        # since TimescaleDB compression is transparent to query results,
        # only to physical storage.
        async with engine.begin() as connection:
            await connection.execute(
                text("""
                    SELECT compress_chunk(chunk, if_not_compressed => true)
                    FROM show_chunks(
                        'timeseries.bybit_momentum_bars_1m',
                        older_than => :chunk_end,
                        newer_than => :chunk_start
                    ) AS chunk
                """),
                {"chunk_start": target.date(), "chunk_end": target.date() + timedelta(days=1)},
            )

        since = target
        until = target + timedelta(minutes=1)
        min_volume_24h_usd = 1.0
        extreme_threshold_pct = 10.0

        live_repository = MomentumFlowBidirectionalBurstRepository(engine)
        live_minutes = await live_repository.fetch_candidate_extreme_minutes(
            exchange=exchange,
            capture_version=_TEST_CAPTURE_VERSION,
            market_type=_TEST_MARKET_TYPE,
            since=since,
            until=until,
            min_volume_24h_usd=min_volume_24h_usd,
            extreme_threshold_pct=extreme_threshold_pct,
        )
        # Sanity: the seeded burst is still real even read back from
        # compressed storage.
        assert len(live_minutes) == 1

        extract_repository = OfflineBarsExtractRepository(engine)
        manifest = await extract_repository.extract_bars_to_parquet(
            exchange=exchange,
            capture_version=_TEST_CAPTURE_VERSION,
            market_type=_TEST_MARKET_TYPE,
            since=since,
            until=until,
            output_path=tmp_path / "compressed_parity.parquet",
        )
        offline_minutes = fetch_candidate_extreme_minutes_offline(
            manifest,
            since=since,
            until=until,
            min_volume_24h_usd=min_volume_24h_usd,
            extreme_threshold_pct=extreme_threshold_pct,
        )

        assert offline_minutes == live_minutes
    finally:
        await _cleanup(engine, exchange=exchange)
        await engine.dispose()


async def test_offline_query_matches_live_query_with_a_real_gap(tmp_path: Path) -> None:
    """A missing minute inside the trailing-24h window must reject the
    candidate on BOTH paths identically (observed_bars_24h != 1440) -- not
    just the positive, gapless case."""
    engine = await _connect_or_skip()
    try:
        symbol = "GAPPARITYUSDT"
        target = _START
        await _seed_gapless_burst_window(engine, symbol=symbol, target=target)
        # Delete one baseline minute deep inside the 24h window -> a real gap.
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM timeseries.bybit_momentum_bars_1m "
                    "WHERE exchange = :exchange AND symbol = :symbol AND bucket_start = :ts"
                ),
                {
                    "exchange": _TEST_EXCHANGE,
                    "symbol": symbol,
                    "ts": target - timedelta(minutes=700),
                },
            )

        since = target
        until = target + timedelta(minutes=1)
        min_volume_24h_usd = 1.0
        extreme_threshold_pct = 10.0

        live_repository = MomentumFlowBidirectionalBurstRepository(engine)
        live_minutes = await live_repository.fetch_candidate_extreme_minutes(
            exchange=_TEST_EXCHANGE,
            capture_version=_TEST_CAPTURE_VERSION,
            market_type=_TEST_MARKET_TYPE,
            since=since,
            until=until,
            min_volume_24h_usd=min_volume_24h_usd,
            extreme_threshold_pct=extreme_threshold_pct,
        )
        assert live_minutes == ()  # sanity: the gap really does reject it live

        extract_repository = OfflineBarsExtractRepository(engine)
        manifest = await extract_repository.extract_bars_to_parquet(
            exchange=_TEST_EXCHANGE,
            capture_version=_TEST_CAPTURE_VERSION,
            market_type=_TEST_MARKET_TYPE,
            since=since,
            until=until,
            output_path=tmp_path / "gap-parity.parquet",
        )
        offline_minutes = fetch_candidate_extreme_minutes_offline(
            manifest,
            since=since,
            until=until,
            min_volume_24h_usd=min_volume_24h_usd,
            extreme_threshold_pct=extreme_threshold_pct,
        )
        assert offline_minutes == live_minutes == ()
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_offline_query_matches_live_query_at_the_exact_threshold_boundary(
    tmp_path: Path,
) -> None:
    """buy_burst_pct_5m landing EXACTLY on extreme_threshold_pct must be
    included on both paths (>=, not >) -- proves the boundary condition
    itself, not just a comfortably-over-threshold burst."""
    engine = await _connect_or_skip()
    try:
        symbol = "THRESHOLDUSDT"
        target = _START
        baseline_minutes = 1435
        burst_minutes = 5
        baseline_buy = 100.0
        baseline_sell = 100.0
        # Solve algebraically for the burst notional that makes
        # buy_burst_pct_5m land exactly on the extreme threshold: the
        # burst share of total 24h volume, isolated for burst_buy.
        extreme_threshold_pct = 10.0
        baseline_total = baseline_minutes * (baseline_buy + baseline_sell)
        burst_buy = (extreme_threshold_pct * baseline_total) / (
            burst_minutes * (100.0 - extreme_threshold_pct)
        )
        for i in range(baseline_minutes):
            await _seed_bar(
                engine,
                symbol=symbol,
                bucket_start=target - timedelta(minutes=baseline_minutes + burst_minutes - 1 - i),
                close_price=1.0,
                buy_notional=baseline_buy,
                sell_notional=baseline_sell,
            )
        for i in range(burst_minutes):
            await _seed_bar(
                engine,
                symbol=symbol,
                bucket_start=target - timedelta(minutes=burst_minutes - 1 - i),
                close_price=2.0,
                buy_notional=burst_buy,
                sell_notional=0.0,
            )

        since = target
        until = target + timedelta(minutes=1)
        min_volume_24h_usd = 1.0

        live_repository = MomentumFlowBidirectionalBurstRepository(engine)
        live_minutes = await live_repository.fetch_candidate_extreme_minutes(
            exchange=_TEST_EXCHANGE,
            capture_version=_TEST_CAPTURE_VERSION,
            market_type=_TEST_MARKET_TYPE,
            since=since,
            until=until,
            min_volume_24h_usd=min_volume_24h_usd,
            extreme_threshold_pct=extreme_threshold_pct,
        )
        assert len(live_minutes) == 1
        assert live_minutes[0].buy_burst_pct_5m == pytest.approx(extreme_threshold_pct)

        extract_repository = OfflineBarsExtractRepository(engine)
        manifest = await extract_repository.extract_bars_to_parquet(
            exchange=_TEST_EXCHANGE,
            capture_version=_TEST_CAPTURE_VERSION,
            market_type=_TEST_MARKET_TYPE,
            since=since,
            until=until,
            output_path=tmp_path / "threshold-parity.parquet",
        )
        offline_minutes = fetch_candidate_extreme_minutes_offline(
            manifest,
            since=since,
            until=until,
            min_volume_24h_usd=min_volume_24h_usd,
            extreme_threshold_pct=extreme_threshold_pct,
        )
        # Postgres and DuckDB compute the same division in slightly
        # different float arithmetic right at an exact boundary (observed:
        # 10.0 vs 10.000000000000004) -- not a real divergence, so this one
        # comparison tolerates float noise instead of requiring bit-
        # identical output. Every other differential test in this file
        # compares comfortably-over-threshold data with exact `==`; this is
        # the one deliberately razor's-edge case.
        assert len(offline_minutes) == len(live_minutes) == 1
        offline_minute, live_minute = offline_minutes[0], live_minutes[0]
        assert offline_minute.symbol == live_minute.symbol
        assert offline_minute.bucket_start == live_minute.bucket_start
        assert offline_minute.close_price == pytest.approx(live_minute.close_price)
        assert offline_minute.buy_burst_pct_5m == pytest.approx(live_minute.buy_burst_pct_5m)
        assert offline_minute.sell_burst_pct_5m == pytest.approx(live_minute.sell_burst_pct_5m)
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_extract_bars_to_parquet_does_not_leak_across_exchange_partitions(
    tmp_path: Path,
) -> None:
    """Two different exchange/capture_version partitions seeded with the
    SAME symbol/timestamp but different data -- an extract for exchange A
    must contain exactly A's rows, never B's, and vice versa."""
    engine = await _connect_or_skip()
    exchange_a = _TEST_EXCHANGE
    exchange_b = f"{_TEST_EXCHANGE}_b"
    try:
        symbol = "PARTITIONUSDT"
        await _seed_bar(
            engine,
            symbol=symbol,
            bucket_start=_START,
            close_price=1.0,
            buy_notional=111.0,
            exchange=exchange_a,
        )
        await _seed_bar(
            engine,
            symbol=symbol,
            bucket_start=_START,
            close_price=2.0,
            buy_notional=222.0,
            exchange=exchange_b,
        )

        repository = OfflineBarsExtractRepository(engine)
        manifest_a = await repository.extract_bars_to_parquet(
            exchange=exchange_a,
            capture_version=_TEST_CAPTURE_VERSION,
            since=_START,
            until=_START + timedelta(minutes=1),
            output_path=tmp_path / "partition-a.parquet",
        )
        manifest_b = await repository.extract_bars_to_parquet(
            exchange=exchange_b,
            capture_version=_TEST_CAPTURE_VERSION,
            since=_START,
            until=_START + timedelta(minutes=1),
            output_path=tmp_path / "partition-b.parquet",
        )

        import duckdb

        connection = duckdb.connect(":memory:")
        try:
            rows_a = connection.execute(
                "SELECT close_price, buy_total_notional_usd FROM read_parquet(?)",
                [manifest_a.parquet_path],
            ).fetchall()
            rows_b = connection.execute(
                "SELECT close_price, buy_total_notional_usd FROM read_parquet(?)",
                [manifest_b.parquet_path],
            ).fetchall()
        finally:
            connection.close()
        assert rows_a == [(1.0, 111.0)]
        assert rows_b == [(2.0, 222.0)]
    finally:
        await _cleanup(engine, exchange=exchange_a)
        await _cleanup(engine, exchange=exchange_b)
        await engine.dispose()


async def test_offline_query_rejects_since_after_until(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    manifest = ExtractManifest(
        extract_query_version="test",
        exchange=_TEST_EXCHANGE,
        market_type=_TEST_MARKET_TYPE,
        capture_version=_TEST_CAPTURE_VERSION,
        since=now - timedelta(days=1),
        until=now + timedelta(days=1),
        row_count=0,
        symbol_count=0,
        parquet_path=str(tmp_path / "does-not-need-to-exist.parquet"),
        parquet_sha256="0" * 64,
        parquet_bytes=0,
        wall_seconds=0.0,
        peak_rss_mb=0.0,
    )
    with pytest.raises(ValueError, match="since must be earlier"):
        fetch_candidate_extreme_minutes_offline(
            manifest,
            since=now,
            until=now - timedelta(minutes=1),
            min_volume_24h_usd=1.0,
            extreme_threshold_pct=1.0,
        )


async def test_offline_query_rejects_a_window_the_manifest_does_not_cover(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    manifest = ExtractManifest(
        extract_query_version="test",
        exchange=_TEST_EXCHANGE,
        market_type=_TEST_MARKET_TYPE,
        capture_version=_TEST_CAPTURE_VERSION,
        since=now,
        until=now + timedelta(hours=1),
        row_count=0,
        symbol_count=0,
        parquet_path=str(tmp_path / "does-not-need-to-exist.parquet"),
        parquet_sha256="0" * 64,
        parquet_bytes=0,
        wall_seconds=0.0,
        peak_rss_mb=0.0,
    )
    with pytest.raises(ValueError, match="is not covered"):
        fetch_candidate_extreme_minutes_offline(
            manifest,
            since=now - timedelta(hours=1),  # before manifest.since
            until=now + timedelta(minutes=30),
            min_volume_24h_usd=1.0,
            extreme_threshold_pct=1.0,
        )


async def test_offline_query_rejects_a_parquet_file_that_does_not_match_its_manifest_hash(
    tmp_path: Path,
) -> None:
    engine = await _connect_or_skip()
    try:
        symbol = "TAMPEREDUSDT"
        await _seed_bar(engine, symbol=symbol, bucket_start=_START, close_price=1.0)

        repository = OfflineBarsExtractRepository(engine)
        manifest = await repository.extract_bars_to_parquet(
            exchange=_TEST_EXCHANGE,
            capture_version=_TEST_CAPTURE_VERSION,
            since=_START,
            until=_START + timedelta(minutes=1),
            output_path=tmp_path / "tampered.parquet",
        )
        # Simulate the file having been swapped/corrupted/regenerated since
        # the manifest was produced.
        Path(manifest.parquet_path).write_bytes(b"not a real parquet file")

        with pytest.raises(ValueError, match="does not match its own manifest"):
            fetch_candidate_extreme_minutes_offline(
                manifest,
                since=_START,
                until=_START + timedelta(minutes=1),
                min_volume_24h_usd=1.0,
                extreme_threshold_pct=1.0,
            )
    finally:
        await _cleanup(engine)
        await engine.dispose()


async def test_offline_query_rejects_over_candidate_row_cap(tmp_path: Path) -> None:
    """The candidate set crossing an extreme-burst threshold is expected to
    be small by construction, but nothing previously enforced that -- this
    seeds two independent symbols each producing one genuine candidate
    minute, then caps max_candidate_rows at 1 and expects a loud failure
    rather than a silently truncated result (colleague review, 2026-09-03
    follow-up round 2)."""
    engine = await _connect_or_skip()
    try:
        target = _START
        await _seed_gapless_burst_window(engine, symbol="CAPAUSDT", target=target)
        await _seed_gapless_burst_window(engine, symbol="CAPBUSDT", target=target)

        since = target
        until = target + timedelta(minutes=1)
        extract_repository = OfflineBarsExtractRepository(engine)
        manifest = await extract_repository.extract_bars_to_parquet(
            exchange=_TEST_EXCHANGE,
            capture_version=_TEST_CAPTURE_VERSION,
            market_type=_TEST_MARKET_TYPE,
            since=since,
            until=until,
            output_path=tmp_path / "candidate_cap.parquet",
        )

        # Sanity: with no cap tightened, both seeded symbols' candidate
        # minutes come back.
        uncapped = fetch_candidate_extreme_minutes_offline(
            manifest,
            since=since,
            until=until,
            min_volume_24h_usd=1.0,
            extreme_threshold_pct=10.0,
        )
        assert len(uncapped) == 2

        with pytest.raises(ValueError, match="over max_candidate_rows"):
            fetch_candidate_extreme_minutes_offline(
                manifest,
                since=since,
                until=until,
                min_volume_24h_usd=1.0,
                extreme_threshold_pct=10.0,
                max_candidate_rows=1,
            )
    finally:
        await _cleanup(engine)
        await engine.dispose()
