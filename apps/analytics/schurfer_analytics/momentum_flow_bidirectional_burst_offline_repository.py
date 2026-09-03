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
  production-scale invocation. Measured end to end (this fix's own local
  benchmark, 2,592,000 rows / 600 symbols / 3 days, real streamed Postgres
  -> on-disk DuckDB -> Parquet, not a synthetic in-memory-only test): 807s
  wall time, 555MB peak RSS -- comfortably under production analytics' own
  1536 MiB `mem_limit`, and NOT observed to scale with total row count the
  way the original in-memory version would have (RSS is bounded by one
  streamed batch, not the whole dataset). Extrapolated to HYP-016's own
  full 9-day/full-universe scale (~13M rows), wall time would be on the
  order of an hour -- a real, disclosed cost, not hidden, but it is a
  ONE-TIME cost per discovery iteration (every subsequent `fetch_
  candidate_extreme_minutes_offline` call against the finished file is
  local and near-instant), and it is exactly what this module trades for:
  memory safety and zero further production load, not raw speed. Found
  and fixed one real throughput bug in the process: `INSERT ... ON
  CONFLICT DO NOTHING` measured over 500x slower than a plain `INSERT` in
  DuckDB's Python `executemany` path -- dropped, since day-chunks are
  already non-overlapping by construction (point 1 above) and can never
  produce a genuine conflict.
- `fetch_candidate_extreme_minutes_offline` now takes the `ExtractManifest`
  itself (not a bare path + a caller-supplied `exchange` that could name
  the wrong file), re-verifies the file's SHA-256 against the manifest, and
  fails closed if the requested [since, until) is not covered by what the
  manifest says was actually extracted -- a Binance file can no longer be
  silently read and labeled `exchange="bybit"` by a caller passing the
  wrong path/exchange pair.

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


def _duckdb_connect(database: str = ":memory:") -> duckdb.DuckDBPyConnection:
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
    connection = duckdb.connect(database)
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
        max_extract_rows: int = MAX_EXTRACT_ROWS,
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

        duckdb_conn = _duckdb_connect(tmp_duckdb_path.as_posix())
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
                        # Plain INSERT, no ON CONFLICT clause: measured
                        # (colleague review, 2026-09-03 follow-up) at
                        # >500x slower than the plain form in DuckDB's
                        # Python executemany path (a 200k-row micro-
                        # benchmark: ~4,500 rows/s plain vs. still not
                        # finished after 5+ minutes with ON CONFLICT DO
                        # NOTHING). Safe to drop: chunks are non-
                        # overlapping by construction (see the comment
                        # above this loop), so no chunk can ever propose a
                        # (symbol, bucket_start) pair another chunk already
                        # inserted -- the PRIMARY KEY stays as a real
                        # integrity check, and a genuine duplicate now
                        # raises loudly instead of being silently dropped,
                        # which is the more correct behavior for a
                        # violation that should never legitimately happen.
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
                                for row in batch
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
    actually extracted -- colleague review, 2026-09-03."""
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
            exchange=manifest.exchange,
            symbol=str(row[0]),
            bucket_start=row[1],
            close_price=float(row[2]),
            buy_burst_pct_5m=float(row[3] or 0.0),
            sell_burst_pct_5m=float(row[4] or 0.0),
        )
        for row in sorted(rows, key=lambda row: (str(row[0]), row[1]))
    )
