"""Offline replica of the CEX activity 5m/24h burst denominator
(research/cex-activity-offline-denominator-v1).

Why this exists: HYP-016 (`cex_activity_discovery_report.py`) needs
`fetch_candidate_extreme_minutes` -- a RANGE-window computation of each
minute's trailing-5m/trailing-24h taker-notional ratio -- over the full
instrument universe and a multi-day discovery window. The first production
attempt hit the 300s per-query `statement_timeout`
(`momentum_flow_bidirectional_burst_repository.REPORT_STATEMENT_TIMEOUT`); a
follow-up chunked it into one-day windows (`candidate_query_windows`, still
in that module, reused here) to stay under that per-query bound, but running
the FULL chunked sequence against live production for the full universe was
itself manually stopped after 12 minutes because it was degrading production
I/O and diagnostic-query latency for everything else sharing that database --
never a query-syntax problem, a sustained-load-on-a-live-OLTP-database
problem. See ROADMAP.md's "Near-term interleaving from 2026-08-31", item 3.

The fix is not a faster query. It is running the heavy part somewhere that
is not production at all:

1. `extract_bars_to_parquet` -- a PLAIN, unindexed-window, indexed-range
   SELECT (no window functions, no per-symbol computation) against
   `timeseries.bybit_momentum_bars_1m` for `[since - 24h, until)`, chunked by
   day via the SAME `candidate_query_windows` helper the live path already
   uses (so the two paths can never drift on what "one chunk" means), each
   chunk under its own short `_EXTRACT_STATEMENT_TIMEOUT`. This is the only
   piece that ever touches production, and it is cheap by construction: a
   sequential/index range scan the underlying table is already built to
   serve, not a per-symbol window aggregation across the full universe.
2. `fetch_candidate_extreme_minutes_offline` -- the SAME 5m/24h RANGE-window
   burst computation `_CANDIDATE_EXTREME_MINUTES_SQL` already performs, now
   run by DuckDB against the Parquet file `extract_bars_to_parquet` wrote,
   with zero further production load no matter how many times a discovery
   run needs to be repeated or re-parameterized while iterating.

This module produces `BurstMinute` tuples -- the exact same type
`fetch_candidate_extreme_minutes` (live Postgres path) returns -- so a
caller (the report layer, wired up in a later PR per ROADMAP item 4) can
select either path without touching anything downstream. See
`test_momentum_flow_bidirectional_burst_offline_repository_integration.py`'s
`test_offline_query_matches_live_query_on_identical_seeded_data` for the
actual proof the two paths agree bit-for-bit on the same input -- the whole
point of an offline replica is worthless without that.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import duckdb
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .momentum_flow_bidirectional_burst_repository import candidate_query_windows
from .momentum_flow_bidirectional_burst_study import BurstMinute
from .momentum_flow_capture_contract import BYBIT_MOMENTUM_MARKET_TYPE
from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

EXTRACT_QUERY_VERSION = "cex_activity_offline_bars_extract_v1"
_LOOKBACK = timedelta(hours=24)
OFFLINE_CANDIDATE_QUERY_VERSION = "cex_activity_offline_candidate_extreme_minutes_v1"

# Deliberately much shorter than the live path's REPORT_STATEMENT_TIMEOUT
# (300s): that timeout sizes a per-symbol RANGE-window aggregation across
# the full universe. This query is a plain indexed range scan with no
# per-symbol computation at all -- if one day's worth of that cannot finish
# in well under a minute, something is wrong with the index or the table,
# not with the amount of work requested, and failing fast surfaces that
# instead of masking it behind a timeout sized for a different query shape.
_EXTRACT_STATEMENT_TIMEOUT = "60s"

_RAW_BARS_SQL = text("""
    SELECT symbol, bucket_start, close_price, buy_total_notional_usd, sell_total_notional_usd
    FROM timeseries.bybit_momentum_bars_1m
    WHERE exchange = :exchange AND market_type = :market_type
      AND capture_version = :capture_version
      AND complete = true AND close_price > 0
      AND bucket_start >= :since AND bucket_start < :until
    ORDER BY symbol, bucket_start
""")

# Ported from momentum_flow_bidirectional_burst_repository._CANDIDATE_EXTREME_MINUTES_SQL.
# Any change to that query's window/threshold semantics must be mirrored
# here, or the differential test comparing the two paths on identical
# seeded data will (correctly) fail -- that test is the guard against the
# two silently drifting apart.
_OFFLINE_CANDIDATE_EXTREME_MINUTES_SQL = """
    WITH bars AS (
        SELECT symbol, bucket_start, close_price, buy_total_notional_usd, sell_total_notional_usd
        FROM read_parquet(?)
        WHERE bucket_start >= CAST(? AS TIMESTAMPTZ) - INTERVAL '24 hours'
          AND bucket_start < CAST(? AS TIMESTAMPTZ)
    ),
    windowed AS (
        SELECT symbol, bucket_start, close_price,
               SUM(buy_total_notional_usd) OVER w5 AS buy_notional_5m,
               SUM(sell_total_notional_usd) OVER w5 AS sell_notional_5m,
               SUM(buy_total_notional_usd + sell_total_notional_usd) OVER w24h AS total_volume_24h,
               count(*) OVER w5 AS observed_bars_5m,
               count(*) OVER w24h AS observed_bars_24h
        FROM bars
        WINDOW
            w5 AS (
                PARTITION BY symbol ORDER BY bucket_start
                RANGE BETWEEN INTERVAL '4 minutes' PRECEDING AND CURRENT ROW
            ),
            w24h AS (
                PARTITION BY symbol ORDER BY bucket_start
                RANGE BETWEEN INTERVAL '1439 minutes' PRECEDING AND CURRENT ROW
            )
    ),
    scored AS (
        SELECT symbol, bucket_start, close_price,
               100.0 * buy_notional_5m / NULLIF(total_volume_24h, 0) AS buy_burst_pct_5m,
               100.0 * sell_notional_5m / NULLIF(total_volume_24h, 0) AS sell_burst_pct_5m
        FROM windowed
        WHERE bucket_start >= CAST(? AS TIMESTAMPTZ)
          AND observed_bars_5m = 5
          AND observed_bars_24h = 1440
          AND total_volume_24h > ?
    )
    SELECT symbol, bucket_start, close_price, buy_burst_pct_5m, sell_burst_pct_5m
    FROM scored
    WHERE buy_burst_pct_5m >= ? OR sell_burst_pct_5m >= ?
    ORDER BY symbol, bucket_start
"""


@dataclass(frozen=True)
class ExtractManifest:
    """Reproducibility metadata for one `extract_bars_to_parquet` run --
    mirrors the row-count/hash verification discipline
    `token_history_parquet_dataset.py` already established for a frozen
    Parquet output, scoped down to what this bounded, re-runnable extract
    actually needs (no run_id/publishable ceremony: this is disposable
    working input to a discovery query, not itself a frozen dataset a
    verdict depends on)."""

    extract_query_version: str
    exchange: str
    market_type: str
    capture_version: str
    since: datetime
    until: datetime
    row_count: int
    symbol_count: int
    parquet_path: str
    parquet_sha256: str
    parquet_bytes: int


def _duckdb_connect() -> duckdb.DuckDBPyConnection:
    # Found via test_offline_query_matches_live_query_on_identical_seeded_data
    # actually disagreeing with the live path by exactly this host's own
    # UTC offset: DuckDB silently converts a tz-aware Python datetime to
    # its session-local TimeZone (host-dependent, NOT necessarily UTC) when
    # binding/storing it as a plain TIMESTAMP. Pinning the session
    # TimeZone to UTC here, and using TIMESTAMPTZ (never bare TIMESTAMP)
    # for every bucket_start column/cast in this module, makes every
    # instant round-trip exactly regardless of the machine this runs on.
    # TIMESTAMPTZ round-tripping through DuckDB's Python client also
    # requires `pytz` importable (pinned in pyproject.toml, not imported
    # directly by this module) -- without it, any fetch of a TIMESTAMPTZ
    # column raises InvalidInputException at query time, not at import
    # time.
    connection = duckdb.connect(":memory:")
    connection.execute("SET TimeZone = 'UTC'")
    return connection


class OfflineBarsExtractRepository:
    """Postgres -> Parquet extraction. The only piece of this module that
    ever touches production; deliberately as small and cheap a query shape
    as the source table can serve, per this module's own docstring."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, database_url: str) -> OfflineBarsExtractRepository:
        return cls(
            create_async_engine(
                async_database_url(database_url),
                pool_pre_ping=True,
                pool_size=2,
                max_overflow=0,
            )
        )

    async def extract_bars_to_parquet(
        self,
        *,
        exchange: str,
        capture_version: str,
        since: datetime,
        until: datetime,
        output_path: Path,
        market_type: str = BYBIT_MOMENTUM_MARKET_TYPE,
    ) -> ExtractManifest:
        if since >= until:
            raise ValueError("since must be earlier than until")

        rows: list[Any] = []
        # Each chunk still requests its own [chunk_since - 24h, chunk_until)
        # coverage -- candidate_query_windows only splits the OUTPUT range,
        # exactly like its docstring says, and every chunk here re-fetches
        # its own 24h lookback the same way the live path's caller does.
        # That means the last ~24h of one chunk and the first ~24h of the
        # next chunk are fetched twice; harmless (the query below dedupes on
        # write via a distinct symbol+bucket_start key set, see the
        # CREATE TABLE below), and far cheaper than teaching this extractor
        # to track cross-chunk overlap for a query this simple.
        for chunk_since, chunk_until in candidate_query_windows(since, until):
            fetch_since = chunk_since - _LOOKBACK
            async with self._engine.connect() as connection, connection.begin():
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                await connection.execute(
                    text("SELECT set_config('statement_timeout', :timeout, true)"),
                    {"timeout": _EXTRACT_STATEMENT_TIMEOUT},
                )
                result = await connection.execute(
                    _RAW_BARS_SQL,
                    {
                        "exchange": exchange,
                        "market_type": market_type,
                        "capture_version": capture_version,
                        "since": fetch_since,
                        "until": chunk_until,
                    },
                )
                rows.extend(result.all())

        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")

        duckdb_conn = _duckdb_connect()
        try:
            duckdb_conn.execute(
                """
                CREATE TABLE bars (
                    symbol VARCHAR NOT NULL,
                    bucket_start TIMESTAMPTZ NOT NULL,
                    close_price DOUBLE NOT NULL,
                    buy_total_notional_usd DOUBLE NOT NULL,
                    sell_total_notional_usd DOUBLE NOT NULL,
                    PRIMARY KEY (symbol, bucket_start)
                )
                """
            )
            deduped = {(str(row.symbol), row.bucket_start): row for row in rows}
            duckdb_conn.executemany(
                "INSERT INTO bars VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        str(row.symbol),
                        row.bucket_start,
                        float(row.close_price),
                        float(row.buy_total_notional_usd),
                        float(row.sell_total_notional_usd),
                    )
                    for row in deduped.values()
                ],
            )
            duckdb_conn.execute(
                "COPY (SELECT * FROM bars ORDER BY symbol, bucket_start) "
                "TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
                [tmp_path.as_posix()],
            )
            count_row = duckdb_conn.execute(
                "SELECT count(*), count(DISTINCT symbol) FROM bars"
            ).fetchone()
            # An unqualified aggregate with no GROUP BY always returns
            # exactly one row; this is a type-narrowing guard, not a real
            # runtime possibility.
            assert count_row is not None
            row_count, symbol_count = count_row
        finally:
            duckdb_conn.close()

        tmp_bytes = tmp_path.read_bytes()
        parquet_sha256 = hashlib.sha256(tmp_bytes).hexdigest()
        tmp_path.replace(output_path)

        return ExtractManifest(
            extract_query_version=EXTRACT_QUERY_VERSION,
            exchange=exchange,
            market_type=market_type,
            capture_version=capture_version,
            since=since,
            until=until,
            row_count=int(row_count),
            symbol_count=int(symbol_count),
            parquet_path=output_path.as_posix(),
            parquet_sha256=parquet_sha256,
            parquet_bytes=len(tmp_bytes),
        )


def fetch_candidate_extreme_minutes_offline(
    parquet_path: Path,
    *,
    exchange: str,
    since: datetime,
    until: datetime,
    min_volume_24h_usd: float,
    extreme_threshold_pct: float,
) -> tuple[BurstMinute, ...]:
    """DuckDB replica of
    `MomentumFlowBidirectionalBurstRepository.fetch_candidate_extreme_minutes`,
    reading `extract_bars_to_parquet`'s own output instead of live Postgres.
    Same signature shape, same return type, same window/threshold semantics
    -- see this module's docstring and
    `test_offline_query_matches_live_query_on_identical_seeded_data`."""
    if since >= until:
        raise ValueError("since must be earlier than until")

    connection = _duckdb_connect()
    try:
        rows = connection.execute(
            _OFFLINE_CANDIDATE_EXTREME_MINUTES_SQL,
            [
                parquet_path.as_posix(),
                since,
                until,
                since,
                min_volume_24h_usd,
                extreme_threshold_pct,
                extreme_threshold_pct,
            ],
        ).fetchall()
    finally:
        connection.close()

    return tuple(
        BurstMinute(
            exchange=exchange,
            symbol=str(row[0]),
            bucket_start=row[1],
            close_price=float(row[2]),
            buy_burst_pct_5m=float(row[3] or 0.0),
            sell_burst_pct_5m=float(row[4] or 0.0),
        )
        for row in sorted(rows, key=lambda row: (str(row[0]), row[1]))
    )
