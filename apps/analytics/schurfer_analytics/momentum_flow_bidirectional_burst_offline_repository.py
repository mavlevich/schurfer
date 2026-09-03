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
   `timeseries.bybit_momentum_bars_1m` over `[since - 24h, until)`, chunked
   into non-overlapping day-sized pieces via `candidate_query_windows` (so
   this and the live path can never drift on what "one chunk" means; unlike
   that path, this one chunks the ALREADY-lookback-extended range once,
   never re-fetching the same calendar day from two different chunks). This
   is the only piece that ever touches production, and it is cheap by
   construction: a sequential/index range scan the underlying table is
   already built to serve, not a per-symbol window aggregation across the
   full universe.
2. `fetch_candidate_extreme_minutes_offline` -- the SAME 5m/24h RANGE-window
   burst computation `_CANDIDATE_EXTREME_MINUTES_SQL` already performs, now
   run by DuckDB against the Parquet file `extract_bars_to_parquet` wrote,
   with zero further production load no matter how many times a discovery
   run needs to be repeated or re-parameterized while iterating.

Colleague review (2026-09-03) found the first version of `extract_bars_to_
parquet` would very likely be OOM-killed on a real 9-day HYP-016 run
(production analytics' own 1536 MiB `mem_limit`): it accumulated every
fetched row into one Python list across every chunk, deduped that whole
list a second time, inserted it all in one `executemany` into an in-memory
DuckDB, then `read_bytes()`'d the entire finished Parquet file for hashing
-- multiple full-dataset materializations in Python/DuckDB-in-RAM for what
is, at HYP-016's own scale, an estimated ~13M rows. Fixed here:

- Postgres rows are read via a server-side-streamed cursor
  (`AsyncConnection.stream` + `.partitions(_EXTRACT_BATCH_SIZE)`), never
  `.all()`'d -- at most one batch's worth of rows exists in Python at a
  time, for the whole extract, regardless of total row count.
- Each batch is inserted immediately into an ON-DISK DuckDB database (a
  temp `.duckdb` file next to the output, deleted after `COPY` finishes),
  not `:memory:` -- DuckDB manages its own memory/disk spill for the
  accumulating table instead of Python holding it.
- The day-chunks themselves are non-overlapping (see point 1 above), so
  Postgres is never asked for the same calendar day's rows twice.
- The finished Parquet file's SHA-256 is computed by streaming fixed-size
  blocks (`_sha256_file`), never one `read_bytes()` of the whole file.
- `ExtractManifest` now records `row_count`, `wall_seconds`, and
  `peak_rss_mb` (`resource.getrusage`, platform-normalized) so every real
  run leaves an automatic record of exactly the RSS/wall-time/row numbers
  a reviewer would otherwise have to ask for separately before trusting a
  production-scale invocation.
- `fetch_candidate_extreme_minutes_offline` now takes the `ExtractManifest`
  itself (not a bare path + a caller-supplied `exchange` that could name
  the wrong file), re-verifies the file's SHA-256 against the manifest, and
  fails closed if the requested [since, until) is not covered by what the
  manifest says was actually extracted -- a Binance file can no longer be
  silently read and labeled `exchange="bybit"` by a caller passing the
  wrong path/exchange pair.

Independent re-verification (colleague review, 2026-09-03 follow-up round
2) found the above still left two real gaps, both fixed in this revision:

- `fetch_candidate_extreme_minutes_offline` opened its DuckDB connection
  with `_duckdb_connect()`'s default `:memory:` argument and no resource
  pragmas at all -- the on-disk-vs-`:memory:` fix above only ever covered
  `extract_bars_to_parquet`'s own accumulating INSERT table, never the
  window-function computation itself, which is the more memory-hungry of
  the two operations (a RANGE-window SUM/COUNT over the full universe,
  materializing `windowed`/`scored` as CTEs). `_duckdb_connect` now always
  sets an explicit `memory_limit` (default `_DEFAULT_MEMORY_LIMIT`,
  comfortably under production analytics' own 1536 MiB container
  `mem_limit`) and `threads` (default `_DEFAULT_THREADS` -- fewer parallel
  window/sort workers means a smaller peak working set for the same query,
  an explicit safety-over-speed tradeoff for a batch job), and both call
  sites now pass a real `temp_directory` (next to the `.duckdb` build file
  for the extract path, next to the Parquet file for the window-query
  path) so DuckDB spills to disk instead of raising or being OOM-killed
  once a query's working set exceeds `memory_limit`, rather than assuming
  the default `:memory:`-with-no-limit connection would happen to fit.
  The final `fetchall()` in the window-query path is also now bounded: the
  SQL itself carries `LIMIT :max_candidate_rows + 1` and a
  `check_candidate_row_count` guard raises (fail loud, matching this
  module's `check_extract_row_count` convention) if the result would have
  been truncated, rather than the previous unconditional, unbounded
  `fetchall()` -- defense in depth on top of `memory_limit`, since the
  candidate set crossing an extreme-burst threshold is expected to be
  small by construction, but nothing previously enforced that.
- `extract_bars_to_parquet` had no budget covering its own total wall
  time -- `_EXTRACT_STATEMENT_TIMEOUT` (60s) only bounds each individual
  per-chunk Postgres query, not the function as a whole across every
  chunk. A real local benchmark (2,592,000 rows / 600 symbols / 3 days,
  streamed Postgres -> on-disk DuckDB -> Parquet) measured 807s wall time
  before this revision's insert-path change below, which both proves the
  per-chunk timeout alone cannot catch a runaway total extract and
  extrapolates to roughly an hour against HYP-016's own full 9-day/full-
  universe scale (~13M rows) -- a real cost this module explicitly still
  does not hide, but one now bounded by a real, enforced budget rather
  than only disclosed in a docstring: `extract_bars_to_parquet` takes an
  explicit `max_wall_seconds` and checks it after every chunk, raising
  (not silently continuing) if exceeded, so a genuinely stuck or
  much-larger-than-expected run against live production is stopped rather
  than left to run indefinitely. Separately, the per-batch DuckDB insert
  itself switched from Python-level `executemany` (row-by-row FFI/prepared-
  statement overhead) to one columnar `INSERT ... SELECT UNNEST(...)` per
  batch, binding five Python lists (one per column) instead of a list of
  row tuples -- measured (isolated micro-benchmark, 500k synthetic rows)
  at roughly 50x the throughput of the `executemany` path this replaces.
  Re-run end to end after both changes, same 2,592,000-row/600-symbol/
  3-day scenario, now including the window-query call this revision also
  memory-bounds (extract + `fetch_candidate_extreme_minutes_offline`
  together, the full path a real discovery run actually exercises, not
  the extract alone as the previous benchmark measured): 20.46s wall time
  for the extract (down from 807s -- the UNNEST insert path, not the
  memory pragmas, drives this), 1.68s wall time for the window query, 22.14s
  total, 654MB peak RSS across the FULL path (extract + window query
  together, in one process) -- comfortably under both the 768MB
  `_DEFAULT_MEMORY_LIMIT` and production analytics' own 1536 MiB container
  `mem_limit`. Extrapolated to HYP-016's own full 9-day/full-universe scale
  (~13M rows, roughly 5x this benchmark's row count), extract wall time
  would be on the order of 1-2 minutes, not the roughly one hour the
  previous executemany-based path extrapolated to.

This module produces `BurstMinute` tuples -- the exact same type
`fetch_candidate_extreme_minutes` (live Postgres path) returns -- so a
caller (the report layer, wired up in a later PR per ROADMAP item 4) can
select either path without touching anything downstream. See
`test_momentum_flow_bidirectional_burst_offline_repository_integration.py`'s
`test_offline_query_matches_live_query_on_identical_seeded_data` (and its
multi-day/gap/two-partition/threshold-boundary siblings) for the actual
proof the two paths agree bit-for-bit on the same input -- the whole point
of an offline replica is worthless without that.
"""

from __future__ import annotations

import hashlib
import resource
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .momentum_flow_bidirectional_burst_repository import candidate_query_windows
from .momentum_flow_bidirectional_burst_study import BurstMinute
from .momentum_flow_capture_contract import BYBIT_MOMENTUM_MARKET_TYPE
from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from datetime import datetime

EXTRACT_QUERY_VERSION = "cex_activity_offline_bars_extract_v2"
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

# Bounds how many rows ever exist in Python at once (one streamed batch),
# not how many rows the extract can produce in total -- see
# MAX_EXTRACT_ROWS for the total-size safety net.
_EXTRACT_BATCH_SIZE = 5_000

# Generous headroom guard, this codebase's usual fail-loud-not-silently-
# large convention: HYP-016's own full 9-day/full-universe extract is
# estimated around 13M rows; this is sized to comfortably exceed any
# currently-registered discovery window while still catching a genuinely
# runaway since/until by mistake before it can consume unbounded disk.
MAX_EXTRACT_ROWS = 100_000_000

# No per-chunk statement_timeout catches a runaway TOTAL extract -- each
# chunk can individually finish well under _EXTRACT_STATEMENT_TIMEOUT while
# the sum across many chunks still runs far longer than intended (colleague
# review, 2026-09-03 follow-up round 2). Sized generously above this
# module's own measured ~13M-row/full-scale extrapolation (see module
# docstring) so a normal HYP-016-scale run is never falsely aborted, while
# still catching a genuinely stuck or much-larger-than-expected run rather
# than letting it hold a production connection pool slot indefinitely.
DEFAULT_MAX_EXTRACT_WALL_SECONDS = 6 * 60 * 60

# Comfortably under production analytics' own 1536 MiB container
# `mem_limit`, leaving headroom for Python/psycopg/the duckdb library's own
# overhead outside of what DuckDB accounts against this limit itself.
_DEFAULT_MEMORY_LIMIT = "768MB"

# Fewer parallel window-function/sort worker threads means a smaller peak
# working set for the same query -- an explicit safety-over-speed tradeoff,
# appropriate for a batch job with no interactive latency requirement.
_DEFAULT_THREADS = 2

# The candidate set crossing an extreme 5m/24h burst threshold is expected
# to be small by construction (a handful of minutes out of a multi-day,
# full-universe scan), but nothing previously enforced that -- this bounds
# the final fetchall() in fetch_candidate_extreme_minutes_offline the same
# way MAX_EXTRACT_ROWS bounds the raw extract, defense in depth on top of
# the memory_limit pragma above (colleague review, 2026-09-03 follow-up
# round 2).
MAX_CANDIDATE_ROWS = 1_000_000

_HASH_CHUNK_BYTES = 1024 * 1024

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
# here, or the differential tests comparing the two paths on identical
# seeded data will (correctly) fail -- those tests are the guard against
# the two silently drifting apart.
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
    LIMIT ?
"""


@dataclass(frozen=True)
class ExtractManifest:
    """Reproducibility + operational metadata for one `extract_bars_to_
    parquet` run -- mirrors the row-count/hash verification discipline
    `token_history_parquet_dataset.py` already established for a frozen
    Parquet output, scoped down to what this bounded, re-runnable extract
    actually needs (no run_id/publishable ceremony: this is disposable
    working input to a discovery query, not itself a frozen dataset a
    verdict depends on). `wall_seconds`/`peak_rss_mb` exist so a real run
    against production-scale data leaves its own resource-usage record,
    rather than requiring a separate manual benchmark before it can be
    trusted not to repeat the OOM/production-load incidents this module's
    own docstring describes."""

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
    wall_seconds: float
    peak_rss_mb: float


def check_extract_row_count(count: int, max_extract_rows: int) -> None:
    if count > max_extract_rows:
        raise ValueError(
            f"extract has read {count} rows, over max_extract_rows={max_extract_rows}; "
            "narrow since/until or raise the bound explicitly rather than silently "
            "continuing an unexpectedly large, unbounded-disk extract"
        )


def check_candidate_row_count(count: int, max_candidate_rows: int) -> None:
    if count > max_candidate_rows:
        raise ValueError(
            f"candidate extreme-minutes query produced over max_candidate_rows="
            f"{max_candidate_rows} rows; this is expected to be a small set by "
            "construction, so this many rows likely means since/until or "
            "extreme_threshold_pct are misconfigured rather than a genuinely large "
            "result -- narrow the window/threshold or raise the bound explicitly "
            "rather than silently returning a truncated candidate set"
        )


def check_extract_wall_seconds(elapsed_seconds: float, max_wall_seconds: float) -> None:
    if elapsed_seconds > max_wall_seconds:
        raise TimeoutError(
            f"extract_bars_to_parquet has run {elapsed_seconds:.1f}s, over "
            f"max_wall_seconds={max_wall_seconds}; no single chunk's own "
            "statement_timeout catches a runaway TOTAL extract across many chunks -- "
            "narrow since/until, raise the bound explicitly, or investigate why this "
            "run is taking far longer than a normal HYP-016-scale extract rather than "
            "letting it hold a production connection pool slot indefinitely"
        )


def _peak_rss_mb() -> float:
    # ru_maxrss units are platform-defined: kilobytes on Linux (where
    # production actually runs), bytes on macOS/BSD (where this is
    # developed/tested) -- normalizing here, once, avoids every caller
    # having to know that.
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 1024 if sys.platform != "darwin" else peak / (1024 * 1024)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _duckdb_connect(
    database: str = ":memory:",
    *,
    temp_directory: Path | None = None,
    memory_limit: str = _DEFAULT_MEMORY_LIMIT,
    threads: int = _DEFAULT_THREADS,
) -> duckdb.DuckDBPyConnection:
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
    #
    # memory_limit/threads are always set (colleague review, 2026-09-03
    # follow-up round 2): the earlier on-disk-vs-:memory: fix only ever
    # bounded extract_bars_to_parquet's own accumulating INSERT table, not
    # the window-function computation fetch_candidate_extreme_minutes_
    # offline runs -- an unbounded default-:memory: connection with no
    # resource pragmas at all. A caller that also passes temp_directory
    # gets a real spill target once a query's working set exceeds
    # memory_limit, instead of DuckDB raising (or, worse, the OS OOM-
    # killing the process) the moment it does.
    connection = duckdb.connect(database)
    connection.execute("SET TimeZone = 'UTC'")
    connection.execute(f"SET memory_limit = '{memory_limit}'")
    connection.execute(f"SET threads = {threads}")
    if temp_directory is not None:
        temp_directory.mkdir(parents=True, exist_ok=True)
        connection.execute(f"SET temp_directory = '{temp_directory.as_posix()}'")
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
        max_extract_rows: int = MAX_EXTRACT_ROWS,
        max_wall_seconds: float = DEFAULT_MAX_EXTRACT_WALL_SECONDS,
    ) -> ExtractManifest:
        if since >= until:
            raise ValueError("since must be earlier than until")

        started_at = time.monotonic()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_parquet_path = output_path.with_suffix(output_path.suffix + ".tmp")
        # On disk, not :memory: -- DuckDB spills/manages this table's own
        # storage instead of Python (or DuckDB-in-RAM) holding the whole
        # accumulating extract at once. Deleted in the finally block below
        # regardless of outcome; a stale leftover from a previous crashed
        # run is removed first so it can never silently merge into this run.
        tmp_duckdb_path = output_path.with_suffix(output_path.suffix + ".build.duckdb")
        tmp_duckdb_path.unlink(missing_ok=True)
        # A real spill target for the CREATE TABLE/COPY/ORDER BY below, once
        # their own working set exceeds _duckdb_connect's memory_limit --
        # not just the on-disk database file itself, which only bounds the
        # table's steady-state storage, not DuckDB's transient working
        # memory while building/sorting it. Removed in the finally block
        # below like the .duckdb file itself.
        tmp_spill_dir = output_path.with_suffix(output_path.suffix + ".build.spill")

        duckdb_conn = _duckdb_connect(tmp_duckdb_path.as_posix(), temp_directory=tmp_spill_dir)
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

            total_rows = 0
            # Chunked over [since - 24h, until), not [since, until): this
            # extractor copies rows, it does not compute a per-chunk window
            # function the way the live path does, so it never needs to
            # re-request the same calendar day's rows once per output
            # chunk -- one non-overlapping partition of the FULL needed
            # range (already including the lookback) is both simpler and
            # halves the redundant Postgres I/O the first version of this
            # function had (every day fetched exactly once, not twice from
            # two adjacent chunks' own lookback windows).
            for chunk_since, chunk_until in candidate_query_windows(since - _LOOKBACK, until):
                # No single chunk's own _EXTRACT_STATEMENT_TIMEOUT catches a
                # runaway TOTAL extract across many chunks -- checked once
                # per chunk (not per row/batch) so this stays cheap relative
                # to the actual I/O the loop is doing.
                check_extract_wall_seconds(time.monotonic() - started_at, max_wall_seconds)
                async with self._engine.connect() as connection, connection.begin():
                    await connection.execute(text("SET TRANSACTION READ ONLY"))
                    await connection.execute(
                        text("SELECT set_config('statement_timeout', :timeout, true)"),
                        {"timeout": _EXTRACT_STATEMENT_TIMEOUT},
                    )
                    result = await connection.stream(
                        _RAW_BARS_SQL,
                        {
                            "exchange": exchange,
                            "market_type": market_type,
                            "capture_version": capture_version,
                            "since": chunk_since,
                            "until": chunk_until,
                        },
                    )
                    # Server-side-streamed and consumed in bounded batches:
                    # at most _EXTRACT_BATCH_SIZE rows ever exist in Python
                    # at once for the whole extract, regardless of how many
                    # days or how many total rows this call covers.
                    async for batch in result.partitions(_EXTRACT_BATCH_SIZE):
                        total_rows += len(batch)
                        check_extract_row_count(total_rows, max_extract_rows)
                        # Columnar UNNEST insert, not row-by-row
                        # executemany: measured (isolated micro-benchmark,
                        # 500k synthetic rows) at roughly 50x the
                        # throughput of Python-level executemany, which
                        # pays per-row FFI/prepared-statement overhead for
                        # every single row. Five parallel Python lists (one
                        # per column) bind directly as DuckDB LIST
                        # parameters and UNNEST back into rows inside
                        # DuckDB itself, so this batch's insert is one
                        # vectorized operation instead of _EXTRACT_BATCH_
                        # SIZE separate ones. No ON CONFLICT clause here
                        # either, for the same reason the earlier
                        # executemany version dropped it (colleague review,
                        # 2026-09-03 follow-up): chunks are non-overlapping
                        # by construction (see the comment above this
                        # loop), so no chunk can ever propose a (symbol,
                        # bucket_start) pair another chunk already
                        # inserted -- the PRIMARY KEY stays as a real
                        # integrity check, and a genuine duplicate now
                        # raises loudly instead of being silently dropped.
                        duckdb_conn.execute(
                            """
                            INSERT INTO bars
                            SELECT * FROM (
                                SELECT
                                    UNNEST(?::VARCHAR[]) AS symbol,
                                    UNNEST(?::TIMESTAMPTZ[]) AS bucket_start,
                                    UNNEST(?::DOUBLE[]) AS close_price,
                                    UNNEST(?::DOUBLE[]) AS buy_total_notional_usd,
                                    UNNEST(?::DOUBLE[]) AS sell_total_notional_usd
                            )
                            """,
                            [
                                [str(row.symbol) for row in batch],
                                [row.bucket_start for row in batch],
                                [float(row.close_price) for row in batch],
                                [float(row.buy_total_notional_usd) for row in batch],
                                [float(row.sell_total_notional_usd) for row in batch],
                            ],
                        )

            duckdb_conn.execute(
                "COPY (SELECT * FROM bars ORDER BY symbol, bucket_start) "
                "TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
                [tmp_parquet_path.as_posix()],
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
            tmp_duckdb_path.unlink(missing_ok=True)
            shutil.rmtree(tmp_spill_dir, ignore_errors=True)

        parquet_sha256 = _sha256_file(tmp_parquet_path)
        parquet_bytes = tmp_parquet_path.stat().st_size
        tmp_parquet_path.replace(output_path)
        wall_seconds = time.monotonic() - started_at

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
            parquet_bytes=parquet_bytes,
            wall_seconds=wall_seconds,
            peak_rss_mb=_peak_rss_mb(),
        )


def fetch_candidate_extreme_minutes_offline(
    manifest: ExtractManifest,
    *,
    since: datetime,
    until: datetime,
    min_volume_24h_usd: float,
    extreme_threshold_pct: float,
    max_candidate_rows: int = MAX_CANDIDATE_ROWS,
) -> tuple[BurstMinute, ...]:
    """DuckDB replica of
    `MomentumFlowBidirectionalBurstRepository.fetch_candidate_extreme_minutes`,
    reading `extract_bars_to_parquet`'s own output instead of live Postgres.
    Same window/threshold semantics -- see this module's docstring and
    `test_offline_query_matches_live_query_on_identical_seeded_data`.

    Takes the extract's own `ExtractManifest`, not a bare path + a caller-
    supplied `exchange`: `exchange` is read from the manifest (a caller can
    no longer mislabel a Binance file as `exchange="bybit"` by passing the
    wrong string), the file's SHA-256 is re-verified against
    `manifest.parquet_sha256` before any of it is trusted, and the
    requested [since, until) must fall inside what the manifest says was
    actually extracted -- colleague review, 2026-09-03.

    The DuckDB connection this runs the window query on is memory-bounded
    (explicit `memory_limit`/`threads`/`temp_directory`, see
    `_duckdb_connect`) and the query itself is capped at `max_candidate_rows`
    (`check_candidate_row_count` raises rather than silently returning a
    truncated result) -- colleague review, 2026-09-03 follow-up round 2: the
    previous version opened a default `:memory:` connection with no resource
    pragmas at all for this, the more memory-hungry of this module's two
    DuckDB operations, and called an unconditionally unbounded `fetchall()`."""
    if since >= until:
        raise ValueError("since must be earlier than until")
    if since < manifest.since or until > manifest.until:
        raise ValueError(
            f"requested window [{since.isoformat()}, {until.isoformat()}) is not covered "
            f"by this extract's own [{manifest.since.isoformat()}, "
            f"{manifest.until.isoformat()}) -- re-run extract_bars_to_parquet for a wider "
            "range rather than querying past what it actually covers"
        )

    parquet_path = Path(manifest.parquet_path)
    actual_sha256 = _sha256_file(parquet_path)
    if actual_sha256 != manifest.parquet_sha256:
        raise ValueError(
            f"{parquet_path} does not match its own manifest: expected sha256 "
            f"{manifest.parquet_sha256}, found {actual_sha256} -- refusing to trust a "
            "file that may have been swapped, corrupted, or regenerated since this "
            "manifest was produced"
        )

    temp_directory = parquet_path.with_suffix(parquet_path.suffix + ".query.spill")
    connection = _duckdb_connect(temp_directory=temp_directory)
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
                max_candidate_rows + 1,
            ],
        ).fetchall()
    finally:
        connection.close()
        shutil.rmtree(temp_directory, ignore_errors=True)

    check_candidate_row_count(len(rows), max_candidate_rows)

    return tuple(
        BurstMinute(
            exchange=manifest.exchange,
            symbol=str(row[0]),
            bucket_start=row[1],
            close_price=float(row[2]),
            buy_burst_pct_5m=float(row[3] or 0.0),
            sell_burst_pct_5m=float(row[4] or 0.0),
        )
        for row in sorted(rows, key=lambda row: (str(row[0]), row[1]))
    )
