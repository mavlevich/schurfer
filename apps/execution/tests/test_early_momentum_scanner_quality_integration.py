"""Real-Postgres coverage for early_momentum.py's _SQL_SCANNER -- only a
real database proves the rewritten window-evidence query actually produces
correct contiguity/completeness/freshness/version columns from real rows,
not just that the SQL text parses. Mirrors test_episodes_integration.py's
`_connect_or_skip` convention.

Skips when no Postgres is reachable locally, unless REQUIRE_INTEGRATION_DB=1
is set (CI sets this so a broken/unprovisioned Postgres service fails the
build loudly instead of these tests silently skipping and the run still
going green -- colleague review).
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.rows import dict_row
from schurfer_execution import early_momentum
from schurfer_market_quality import WindowQualityReason
from schurfer_market_quality import validate as validate_window_quality

TEST_DATABASE_URL = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"
_REQUIRED_BUCKETS = early_momentum.EARLY_MOMENTUM_V4_QUALITY_POLICY.required_bucket_count


async def _connect_or_skip() -> psycopg.AsyncConnection:
    try:
        conn = await psycopg.AsyncConnection.connect(TEST_DATABASE_URL, autocommit=True)
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1")
    except Exception as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but Postgres is unreachable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres reachable: {exc}")
    return conn


async def test_connect_or_skip_raises_when_require_integration_db_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CI-enforcement path itself: with REQUIRE_INTEGRATION_DB=1, an
    unreachable Postgres must fail the test, never silently skip it."""

    async def _failing_connect(*_args: object, **_kwargs: object) -> psycopg.AsyncConnection:
        raise OSError("connection refused")

    monkeypatch.setenv("REQUIRE_INTEGRATION_DB", "1")
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _failing_connect)
    with pytest.raises(RuntimeError, match="REQUIRE_INTEGRATION_DB"):
        await _connect_or_skip()


def _test_exchange() -> str:
    # Unique per test so parallel/rerun test sessions can never collide,
    # and cleanup can always target by exact exchange match alone.
    return f"test_v4_quality_{uuid.uuid4().hex[:10]}"


async def _insert_bar(
    conn: psycopg.AsyncConnection,
    *,
    exchange: str,
    symbol: str,
    bucket_start: datetime,
    market_type: str = "linear",
    capture_version: str = "v1",
    universe_version: str = "uv-test",
    close_price: float | None = 1.0,
    open_interest: float | None = 1000.0,
    open_interest_event_at: datetime | None = None,
    price_complete: bool | None = True,
    trades_complete: bool = True,
    open_interest_complete: bool | None = True,
    unbackfilled_gap_minutes: int = 0,
    buy_total_notional_usd: float = 100.0,
    sell_total_notional_usd: float = 10.0,
) -> None:
    if open_interest_event_at is None:
        open_interest_event_at = bucket_start
    payload_hash = hashlib.sha256(
        f"{exchange}{symbol}{capture_version}{bucket_start}".encode()
    ).digest()
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO timeseries.bybit_momentum_bars_1m (
                exchange, market_type, symbol, capture_version, bucket_start, universe_version,
                close_price, open_interest, open_interest_event_at, open_interest_observed_at,
                price_complete, trades_complete, open_interest_complete,
                ticker_complete, complete,
                unbackfilled_gap_minutes, buy_total_notional_usd,
                sell_total_notional_usd,
                buy_hist_counts, buy_hist_notional, sell_hist_counts, sell_hist_notional,
                payload_hash
            ) VALUES (
                %(exchange)s, %(market_type)s, %(symbol)s, %(capture_version)s, %(bucket_start)s,
                %(universe_version)s,
                %(close_price)s, %(open_interest)s, %(open_interest_event_at)s,
                %(open_interest_event_at)s,
                %(price_complete)s, %(trades_complete)s, %(open_interest_complete)s,
                false, false,
                %(unbackfilled_gap_minutes)s, %(buy_total_notional_usd)s,
                %(sell_total_notional_usd)s,
                '{}', '{}', '{}', '{}',
                %(payload_hash)s
            )
            ON CONFLICT (exchange, market_type, symbol, capture_version, bucket_start) DO UPDATE SET
                close_price = EXCLUDED.close_price,
                open_interest = EXCLUDED.open_interest,
                open_interest_event_at = EXCLUDED.open_interest_event_at
            """,
            {
                "exchange": exchange,
                "market_type": market_type,
                "symbol": symbol,
                "capture_version": capture_version,
                "bucket_start": bucket_start,
                "universe_version": universe_version,
                "close_price": close_price,
                "open_interest": open_interest,
                "open_interest_event_at": open_interest_event_at,
                "price_complete": price_complete,
                "trades_complete": trades_complete,
                "open_interest_complete": open_interest_complete,
                "unbackfilled_gap_minutes": unbackfilled_gap_minutes,
                "buy_total_notional_usd": buy_total_notional_usd,
                "sell_total_notional_usd": sell_total_notional_usd,
                "payload_hash": payload_hash,
            },
        )


async def _insert_clean_window(
    conn: psycopg.AsyncConnection,
    *,
    exchange: str,
    symbol: str,
    end_at: datetime,
    count: int = _REQUIRED_BUCKETS,
    oi_start: float = 1000.0,
    oi_end: float = 1000.0,
    **kwargs: object,
) -> None:
    for i in range(count):
        minutes_back = count - 1 - i
        bucket_start = end_at - timedelta(minutes=minutes_back)
        frac = (count - 1 - minutes_back) / (count - 1) if count > 1 else 1.0
        oi = oi_start + (oi_end - oi_start) * frac
        await _insert_bar(
            conn,
            exchange=exchange,
            symbol=symbol,
            bucket_start=bucket_start,
            open_interest=oi,
            open_interest_event_at=bucket_start,
            **kwargs,  # type: ignore[arg-type]
        )


async def _cleanup(conn: psycopg.AsyncConnection, *, exchange: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM timeseries.bybit_momentum_bars_1m WHERE exchange = %s", (exchange,)
        )


async def _scanner_rows() -> list[dict[str, object]]:
    async with (
        await psycopg.AsyncConnection.connect(TEST_DATABASE_URL, row_factory=dict_row) as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(
            early_momentum._SQL_SCANNER,
            early_momentum._scanner_sql_params(early_momentum.EARLY_MOMENTUM_V4_QUALITY_POLICY),
        )
        return await cur.fetchall()


async def _evaluate(*, exchange: str, symbol: str) -> tuple[dict[str, object], object] | None:
    rows = await _scanner_rows()
    matches = [r for r in rows if r["exchange"] == exchange and r["symbol"] == symbol]
    if not matches:
        return None
    row = matches[0]
    evidence = early_momentum._row_to_evidence(row)
    result = validate_window_quality(
        evidence, early_momentum.EARLY_MOMENTUM_V4_QUALITY_POLICY, evaluated_at=datetime.now(tz=UTC)
    )
    return row, result


async def test_clean_121_minute_window_qualifies() -> None:
    # A "should qualify" window must use a real exchange the policy has an
    # OI-freshness threshold configured for -- an unconfigured exchange is
    # deliberately fail-closed (STALE_OI), proven by
    # test_stale_oi_is_rejected_fail_closed_when_exchange_has_no_configured_threshold
    # in packages/market-quality's own unit tests.
    conn = await _connect_or_skip()
    symbol = f"CLEAN{uuid.uuid4().hex[:8]}"
    now = datetime.now(tz=UTC).replace(microsecond=0)
    try:
        await _insert_clean_window(conn, exchange="bybit", symbol=symbol, end_at=now)
        outcome = await _evaluate(exchange="bybit", symbol=symbol)
        assert outcome is not None
        row, result = outcome
        assert result.qualified is True, result.reasons
        assert row["raw_row_count"] == _REQUIRED_BUCKETS
        assert row["distinct_bucket_count"] == _REQUIRED_BUCKETS
    finally:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM timeseries.bybit_momentum_bars_1m "
                "WHERE exchange = 'bybit' AND symbol = %s",
                (symbol,),
            )
        await conn.close()


async def test_missing_minute_is_rejected_as_a_gap() -> None:
    conn = await _connect_or_skip()
    exchange = _test_exchange()
    now = datetime.now(tz=UTC).replace(microsecond=0)
    try:
        await _insert_clean_window(conn, exchange=exchange, symbol="GAPUSDT", end_at=now)
        # Delete a minute from the middle of the window -- 120 real rows
        # remain, one short of required_bucket_count.
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM timeseries.bybit_momentum_bars_1m "
                "WHERE exchange = %s AND symbol = %s AND bucket_start = %s",
                (exchange, "GAPUSDT", now - timedelta(minutes=60)),
            )
        outcome = await _evaluate(exchange=exchange, symbol="GAPUSDT")
        assert outcome is not None
        _row, result = outcome
        assert result.qualified is False
        assert WindowQualityReason.INSUFFICIENT_ROWS in result.reasons
    finally:
        await _cleanup(conn, exchange=exchange)
        await conn.close()


async def test_121_rows_stretched_over_130_minutes_is_rejected_as_a_gap() -> None:
    """A window can have exactly required_bucket_count raw rows and still
    be gappy if they're not contiguous -- raw_row_count alone would pass,
    max_gap_seconds must catch it."""
    conn = await _connect_or_skip()
    exchange = _test_exchange()
    now = datetime.now(tz=UTC).replace(microsecond=0)
    try:
        # 116 contiguous minutes [0..115] back from now, plus 5 more further
        # back [121..125], skipping [116..120] -- 121 raw rows total,
        # spanning 125 minutes instead of 120.
        for minutes_back in range(0, 116):
            await _insert_bar(
                conn,
                exchange=exchange,
                symbol="STRETCHUSDT",
                bucket_start=now - timedelta(minutes=minutes_back),
            )
        for minutes_back in range(121, 126):
            await _insert_bar(
                conn,
                exchange=exchange,
                symbol="STRETCHUSDT",
                bucket_start=now - timedelta(minutes=minutes_back),
            )
        outcome = await _evaluate(exchange=exchange, symbol="STRETCHUSDT")
        assert outcome is not None
        row, result = outcome
        assert row["raw_row_count"] == 121
        assert result.qualified is False
        assert WindowQualityReason.GAP in result.reasons
    finally:
        await _cleanup(conn, exchange=exchange)
        await conn.close()


async def test_duplicate_bucket_across_two_capture_versions_is_rejected() -> None:
    """capture_version is part of the primary key -- two rows can
    genuinely share one bucket_start if they differ by capture_version.
    That must surface as DUPLICATE_BUCKET (the seam also independently
    trips CAPTURE_VERSION_NOT_ALLOWED here since 'v1b' isn't in the
    allowlist, which is realistic and fine -- both are real incidents)."""
    conn = await _connect_or_skip()
    exchange = _test_exchange()
    now = datetime.now(tz=UTC).replace(microsecond=0)
    try:
        await _insert_clean_window(conn, exchange=exchange, symbol="DUPUSDT", end_at=now)
        await _insert_bar(
            conn, exchange=exchange, symbol="DUPUSDT", bucket_start=now, capture_version="v1b"
        )
        outcome = await _evaluate(exchange=exchange, symbol="DUPUSDT")
        assert outcome is not None
        _row, result = outcome
        assert result.qualified is False
        assert WindowQualityReason.DUPLICATE_BUCKET in result.reasons
    finally:
        await _cleanup(conn, exchange=exchange)
        await conn.close()


async def test_incomplete_price_is_rejected() -> None:
    conn = await _connect_or_skip()
    exchange = _test_exchange()
    now = datetime.now(tz=UTC).replace(microsecond=0)
    try:
        await _insert_clean_window(conn, exchange=exchange, symbol="PXUSDT", end_at=now)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE timeseries.bybit_momentum_bars_1m SET price_complete = false "
                "WHERE exchange = %s AND symbol = %s AND bucket_start = %s",
                (exchange, "PXUSDT", now),
            )
        outcome = await _evaluate(exchange=exchange, symbol="PXUSDT")
        assert outcome is not None
        _row, result = outcome
        assert result.qualified is False
        assert WindowQualityReason.INCOMPLETE_PRICE in result.reasons
    finally:
        await _cleanup(conn, exchange=exchange)
        await conn.close()


async def test_incomplete_trades_is_rejected() -> None:
    conn = await _connect_or_skip()
    exchange = _test_exchange()
    now = datetime.now(tz=UTC).replace(microsecond=0)
    try:
        await _insert_clean_window(conn, exchange=exchange, symbol="TRUSDT", end_at=now)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE timeseries.bybit_momentum_bars_1m SET trades_complete = false "
                "WHERE exchange = %s AND symbol = %s AND bucket_start = %s",
                (exchange, "TRUSDT", now),
            )
        outcome = await _evaluate(exchange=exchange, symbol="TRUSDT")
        assert outcome is not None
        _row, result = outcome
        assert result.qualified is False
        assert WindowQualityReason.INCOMPLETE_TRADES in result.reasons
    finally:
        await _cleanup(conn, exchange=exchange)
        await conn.close()


async def test_incomplete_oi_is_rejected() -> None:
    conn = await _connect_or_skip()
    exchange = _test_exchange()
    now = datetime.now(tz=UTC).replace(microsecond=0)
    try:
        await _insert_clean_window(conn, exchange=exchange, symbol="OIUSDT", end_at=now)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE timeseries.bybit_momentum_bars_1m SET open_interest_complete = false "
                "WHERE exchange = %s AND symbol = %s AND bucket_start = %s",
                (exchange, "OIUSDT", now),
            )
        outcome = await _evaluate(exchange=exchange, symbol="OIUSDT")
        assert outcome is not None
        _row, result = outcome
        assert result.qualified is False
        assert WindowQualityReason.INCOMPLETE_OI in result.reasons
    finally:
        await _cleanup(conn, exchange=exchange)
        await conn.close()


async def test_stale_latest_bucket_is_rejected() -> None:
    conn = await _connect_or_skip()
    exchange = _test_exchange()
    # The window ends 10 minutes ago -- well past max_bucket_lag_seconds (180s).
    end_at = datetime.now(tz=UTC).replace(microsecond=0) - timedelta(minutes=10)
    try:
        await _insert_clean_window(conn, exchange=exchange, symbol="STALEBUCKETUSDT", end_at=end_at)
        outcome = await _evaluate(exchange=exchange, symbol="STALEBUCKETUSDT")
        assert outcome is not None
        _row, result = outcome
        assert result.qualified is False
        assert WindowQualityReason.STALE_BUCKET in result.reasons
    finally:
        await _cleanup(conn, exchange=exchange)
        await conn.close()


async def test_stale_oi_is_rejected_while_bucket_stays_fresh() -> None:
    """The bucket itself (price/trades) is current, but open_interest_event_at
    never advances -- exactly the "OI feed silently stalled" case this
    policy exists to catch."""
    conn = await _connect_or_skip()
    exchange = _test_exchange()
    now = datetime.now(tz=UTC).replace(microsecond=0)
    stale_oi_event_at = now - timedelta(seconds=700)  # past bybit's 600s limit
    try:
        for minutes_back in range(_REQUIRED_BUCKETS):
            await _insert_bar(
                conn,
                exchange="bybit",  # use the real bybit name so its 600s limit applies
                symbol=f"{exchange[-8:]}STALEOI",
                bucket_start=now - timedelta(minutes=minutes_back),
                open_interest_event_at=stale_oi_event_at,
            )
        outcome = await _evaluate(exchange="bybit", symbol=f"{exchange[-8:]}STALEOI")
        assert outcome is not None
        _row, result = outcome
        assert result.qualified is False
        assert WindowQualityReason.STALE_OI in result.reasons
    finally:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM timeseries.bybit_momentum_bars_1m "
                "WHERE exchange = 'bybit' AND symbol = %s",
                (f"{exchange[-8:]}STALEOI",),
            )
        await conn.close()


async def test_unknown_capture_version_is_rejected_even_in_a_uniform_window() -> None:
    conn = await _connect_or_skip()
    exchange = _test_exchange()
    now = datetime.now(tz=UTC).replace(microsecond=0)
    try:
        await _insert_clean_window(
            conn, exchange=exchange, symbol="V2USDT", end_at=now, capture_version="v2"
        )
        outcome = await _evaluate(exchange=exchange, symbol="V2USDT")
        assert outcome is not None
        _row, result = outcome
        assert result.qualified is False
        assert WindowQualityReason.CAPTURE_VERSION_NOT_ALLOWED in result.reasons
        assert WindowQualityReason.MULTIPLE_CAPTURE_VERSIONS not in result.reasons
    finally:
        await _cleanup(conn, exchange=exchange)
        await conn.close()


async def test_universe_version_seam_is_rejected() -> None:
    conn = await _connect_or_skip()
    exchange = _test_exchange()
    now = datetime.now(tz=UTC).replace(microsecond=0)
    try:
        await _insert_clean_window(conn, exchange=exchange, symbol="UVUSDT", end_at=now)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE timeseries.bybit_momentum_bars_1m SET universe_version = 'uv-new' "
                "WHERE exchange = %s AND symbol = %s AND bucket_start = %s",
                (exchange, "UVUSDT", now),
            )
        outcome = await _evaluate(exchange=exchange, symbol="UVUSDT")
        assert outcome is not None
        _row, result = outcome
        assert result.qualified is False
        assert WindowQualityReason.MULTIPLE_UNIVERSE_VERSIONS in result.reasons
    finally:
        await _cleanup(conn, exchange=exchange)
        await conn.close()


async def test_wrong_market_type_is_excluded_from_results_entirely() -> None:
    """market_type is a WHERE-level scope filter (allowed per the plan --
    a population-selection choice, not a per-row defect to name a reason
    for), so an inverse-market bar for this symbol must never even appear
    in the scanner's evidence rows."""
    conn = await _connect_or_skip()
    exchange = _test_exchange()
    now = datetime.now(tz=UTC).replace(microsecond=0)
    try:
        await _insert_clean_window(
            conn, exchange=exchange, symbol="INVUSDT", end_at=now, market_type="inverse"
        )
        outcome = await _evaluate(exchange=exchange, symbol="INVUSDT")
        assert outcome is None
    finally:
        await _cleanup(conn, exchange=exchange)
        await conn.close()


async def test_future_timestamp_is_rejected() -> None:
    conn = await _connect_or_skip()
    exchange = _test_exchange()
    now = datetime.now(tz=UTC).replace(microsecond=0)
    try:
        await _insert_clean_window(conn, exchange=exchange, symbol="FUTUSDT", end_at=now)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE timeseries.bybit_momentum_bars_1m SET bucket_start = %s "
                "WHERE exchange = %s AND symbol = %s AND bucket_start = %s",
                (now + timedelta(minutes=30), exchange, "FUTUSDT", now),
            )
        outcome = await _evaluate(exchange=exchange, symbol="FUTUSDT")
        assert outcome is not None
        _row, result = outcome
        assert result.qualified is False
        assert WindowQualityReason.FUTURE_TIMESTAMP in result.reasons
    finally:
        await _cleanup(conn, exchange=exchange)
        await conn.close()


async def test_unbackfilled_gap_minutes_alone_is_rejected() -> None:
    conn = await _connect_or_skip()
    exchange = _test_exchange()
    now = datetime.now(tz=UTC).replace(microsecond=0)
    try:
        await _insert_clean_window(conn, exchange=exchange, symbol="UGUSDT", end_at=now)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE timeseries.bybit_momentum_bars_1m SET unbackfilled_gap_minutes = 3 "
                "WHERE exchange = %s AND symbol = %s AND bucket_start = %s",
                (exchange, "UGUSDT", now),
            )
        outcome = await _evaluate(exchange=exchange, symbol="UGUSDT")
        assert outcome is not None
        _row, result = outcome
        assert result.qualified is False
        assert WindowQualityReason.GAP in result.reasons
    finally:
        await _cleanup(conn, exchange=exchange)
        await conn.close()


async def test_a_genuinely_sharp_oi_move_in_a_clean_window_still_qualifies() -> None:
    """No magnitude-based sanity filter anywhere: a real, large OI jump in
    an otherwise fully clean/contiguous/fresh window must qualify."""
    conn = await _connect_or_skip()
    symbol = f"SHARP{uuid.uuid4().hex[:8]}"
    now = datetime.now(tz=UTC).replace(microsecond=0)
    try:
        await _insert_clean_window(
            conn,
            exchange="bybit",
            symbol=symbol,
            end_at=now,
            oi_start=1000.0,
            oi_end=2000.0,  # +100%
        )
        outcome = await _evaluate(exchange="bybit", symbol=symbol)
        assert outcome is not None
        row, result = outcome
        assert result.qualified is True, result.reasons
        signal = early_momentum._compute_signal(row)
        assert signal.qualified is True
    finally:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM timeseries.bybit_momentum_bars_1m "
                "WHERE exchange = 'bybit' AND symbol = %s",
                (symbol,),
            )
        await conn.close()


async def test_an_oi_move_that_only_looks_contiguous_across_a_gap_is_rejected() -> None:
    conn = await _connect_or_skip()
    exchange = _test_exchange()
    now = datetime.now(tz=UTC).replace(microsecond=0)
    try:
        await _insert_clean_window(
            conn,
            exchange=exchange,
            symbol="GAPJUMPUSDT",
            end_at=now,
            oi_start=1000.0,
            oi_end=2000.0,
        )
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM timeseries.bybit_momentum_bars_1m "
                "WHERE exchange = %s AND symbol = %s AND bucket_start = %s",
                (exchange, "GAPJUMPUSDT", now - timedelta(minutes=60)),
            )
        outcome = await _evaluate(exchange=exchange, symbol="GAPJUMPUSDT")
        assert outcome is not None
        _row, result = outcome
        assert result.qualified is False
        assert WindowQualityReason.INSUFFICIENT_ROWS in result.reasons
    finally:
        await _cleanup(conn, exchange=exchange)
        await conn.close()


async def test_bybit_and_binance_are_never_blended_for_the_same_symbol() -> None:
    """Direct regression test for the root-cause finding: BTCUSDT-style
    same-symbol rows from two exchanges on wildly different OI scales must
    always evaluate as two entirely independent series, never merged into
    one evidence row."""
    conn = await _connect_or_skip()
    suffix = uuid.uuid4().hex[:8]
    symbol = f"MIXTEST{suffix}"
    now = datetime.now(tz=UTC).replace(microsecond=0)
    try:
        await _insert_clean_window(
            conn, exchange="bybit", symbol=symbol, end_at=now, oi_start=50000.0, oi_end=55000.0
        )
        await _insert_clean_window(
            conn, exchange="binance", symbol=symbol, end_at=now, oi_start=110000.0, oi_end=112000.0
        )
        rows = await _scanner_rows()
        matches = [r for r in rows if r["symbol"] == symbol]
        assert len(matches) == 2
        by_exchange = {r["exchange"]: r for r in matches}
        assert by_exchange["bybit"]["oi_start"] == pytest.approx(50000.0)
        assert by_exchange["binance"]["oi_start"] == pytest.approx(110000.0)
    finally:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM timeseries.bybit_momentum_bars_1m WHERE symbol = %s", (symbol,)
            )
        await conn.close()
