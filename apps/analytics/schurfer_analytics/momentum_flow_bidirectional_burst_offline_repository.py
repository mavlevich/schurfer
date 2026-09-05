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
   construction relative to the live path's own per-symbol window
   aggregation across the full universe: no window functions, no per-symbol
   computation. It is NOT a plain sequential/index range scan for any date
   HYP-016 will actually query, though -- this table compresses after 1 day
   (`add_compression_policy`, migration 0024), and every real HYP-016 run
   is retrospective by construction, so it always reads already-compressed
   chunks. See "2026-09-04" below for what that costs in practice (measured:
   not much -- Postgres itself is fast against compressed chunks, see that
   section for what the real bottleneck actually was).
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
is, at HYP-016's own scale, an estimated ~13M rows. Fixed here (row-
streaming mechanism superseded 2026-09-05, see that section below -- the
"never accumulate the whole extract in Python" PROPERTY these bullets
established still holds under the new mechanism, just via COPY-streamed
raw bytes instead of a server-side-streamed cursor's typed row batches):

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
  explicit `max_wall_seconds`. Separately, the per-batch DuckDB insert
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

Independent re-verification (colleague review, 2026-09-03 follow-up round
3) found the round-2 `max_wall_seconds` fix above still only checked its
budget once per chunk, before that chunk's own Postgres query started --
never during a chunk's own batch reads, never after the last chunk, and
never during the local COPY/hash phases that follow. A single slow/stuck
chunk (the only one, or the last one) could still run past the budget and
the function would still return successfully; the same was true of the
100%-local COPY/hash phases, which no per-chunk check could ever reach.
Fixed by computing one `time.monotonic()` deadline at the very start of
the call and checking it (`check_extract_deadline`) at every phase
against that SAME deadline, never a fresh per-chunk budget: before each
chunk, after every batch within a chunk, once more after the whole chunk
loop ends, and once more before each of the local COPY and hash steps.
Each chunk's own Postgres `statement_timeout` is now also capped at
whatever time is actually left in the overall budget (`min` against the
fixed `_EXTRACT_STATEMENT_TIMEOUT_MS`), not the fixed value regardless of
how little time remains -- Postgres applies `statement_timeout` to every
individual FETCH against a streamed/server-side cursor, not just the
initial query, so this bounds a hang mid-stream too, enforced server-side
rather than depending on the client staying responsive. The local
COPY/hash phases are deliberately NOT preemptible mid-flight (no
subprocess/hard-kill wrapping either one): both are 100% local work
against the already-fetched on-disk DuckDB file, carrying none of the
production-I/O risk `max_wall_seconds` primarily exists to bound, and the
real benchmark above already measures both, included, at low single-digit
seconds even at this benchmark's 2.59M-row scale -- the checks immediately
before each one exist to fail fast rather than waste time entering a phase
the budget is already spent for, not to interrupt one already running.
`DEFAULT_MAX_EXTRACT_WALL_SECONDS` also dropped from 6 hours to 15
minutes: 6 hours was sized as a generic "don't run forever" ceiling and
directly contradicted this module's own stated goal of never repeating the
12-minute production-I/O incident its own docstring opens with; 15 minutes
still gives roughly 7-10x margin over the ~1-2 minute HYP-016-scale
extrapolation above.

This module produces `BurstMinute` tuples -- the exact same type
`fetch_candidate_extreme_minutes` (live Postgres path) returns -- so a
caller (the report layer, wired up in a later PR per ROADMAP item 4) can
select either path without touching anything downstream. See
`test_momentum_flow_bidirectional_burst_offline_repository_integration.py`'s
`test_offline_query_matches_live_query_on_identical_seeded_data` (and its
multi-day/gap/two-partition/threshold-boundary siblings) for the actual
proof the two paths agree bit-for-bit on the same input -- the whole point
of an offline replica is worthless without that.

## 2026-09-04: the real production run did not match this docstring's own
## benchmark, and the actual cause was not the one first suspected

The first real HYP-016 freeze attempt (`research/cex-activity-discovery-
completion-v1`, after production deploy) exceeded `DEFAULT_MAX_EXTRACT_
WALL_SECONDS` even for a SINGLE day of the 10-day range -- both over an
SSH tunnel and, after redeploying to rule out tunnel latency, running
directly on the production host with zero network hop to Postgres.
Diagnosis (full writeup: `docs/research/cex-activity-discovery-
completion-v1.md`'s own incident note) went through three real,
evidence-gathering steps before changing anything, not a guess:

1. `timescaledb_information.chunks` confirmed the relevant day's chunk
   was already TimescaleDB-compressed (`add_compression_policy(...,
   INTERVAL '1 day')`, migration 0024) -- this table's own `compress_
   segmentby = 'exchange, market_type, symbol, capture_version'` means a
   query that does not filter by `symbol` (this one never does) touches
   roughly one compressed segment per symbol, not the "sequential/index
   range scan" this module's own opening paragraph above describes.
   That framing is accurate for same-day (uncompressed) data only --
   which is ALL the benchmark below and every integration test in this
   file ever exercises, and NONE of what any real HYP-016 run, being
   retrospective by construction, will ever see.
2. `EXPLAIN (VERBOSE, COSTS, SETTINGS)` (plan only) against the real
   extract SELECT confirmed a `Custom Scan (ColumnarScan)` (TimescaleDB's
   own decompression scan) and, importantly, NO separate `Sort` node
   with or without the (at the time still-present) `ORDER BY symbol,
   bucket_start` clause -- `compress_orderby` already gives the scan a
   stable order, so that clause was never the cost driver, though it was
   still removed below as a correct, independent simplification (the
   final Parquet file is already deterministically sorted by the `COPY`
   step's own `ORDER BY`).
3. `EXPLAIN (ANALYZE, VERBOSE, BUFFERS, SETTINGS)` against that SAME real
   compressed day, run in two steps (one hour under a 30s
   `statement_timeout`, then the full day under 60s, per this project's
   own "measure the narrowest slice first" discipline) measured
   **1.36 seconds** execution time for the whole day, 736,703 rows.
   Decisive: Postgres/decompression was never the bottleneck at all.

That redirected the investigation to this function's own Python/DuckDB
loop, where reading the code (not another benchmark) found a real,
genuine cause: `duckdb_conn`'s batch INSERTs ran in DuckDB's own default
autocommit mode, on an ON-DISK database, against a table with a
`PRIMARY KEY (symbol, bucket_start)` -- meaning every one of the ~148
per-day batches (`_EXTRACT_BATCH_SIZE=5_000`) paid its own commit and
unique-index-maintenance cost, roughly 1,480 individual commits for
HYP-016's own 10-day range, on a production container capped to `cpus:
1.0`. Fixed (PR #335, merged and deployed) by wrapping the whole
batch-insert sequence in one explicit DuckDB transaction -- the standard
DuckDB bulk-load pattern, and a real, worthwhile fix in its own right.

**This was NOT, however, the actual fix -- deploying it and re-running
the SAME one-day production diagnostic that originally surfaced this
incident still timed out.** See "2026-09-05" below for the follow-up
diagnosis that found what the autocommit fix missed, and the redesign
that actually resolved it. The compression finding above is real and
worth the `test_offline_extract_matches_live_query_against_a_compressed_
chunk` regression test it earned, but was independently confirmed (both
here and again during the 2026-09-05 follow-up) not to be the bottleneck
either.

**The benchmark quoted throughout this docstring's "2026-09-03" sections
(22.14s / 2.59M rows / 654MB peak RSS) was measured against
freshly-seeded, always-uncompressed test data, like every integration
test in this file -- it structurally cannot represent what any real,
retrospective HYP-016 run against already-compressed historical chunks
will experience**, and should not be read as a production-scale
performance guarantee on its own. Treat it as a correctness/memory-bound
benchmark, not a timing one.

## 2026-09-05: the autocommit fix was real but insufficient -- the actual
## bottleneck was the per-batch UNNEST-bind call itself, and the fix is a
## pipeline redesign, not another tuning pass

Re-running the SAME one-day production diagnostic that surfaced the
2026-09-04 incident, after PR #335 (the autocommit/transaction fix above)
was deployed, still did not finish inside a 180s or a 450s budget.
Diagnosis this time isolated the cost with a synthetic, fully offline
DuckDB micro-benchmark (no Postgres, no real table) run directly on the
production host via `docker compose run --rm --no-deps --entrypoint
python3 analytics ...` (inheriting the exact same `cpus: 1.0`/`mem_limit:
1536m` cgroup limits `extract_bars_to_parquet` itself runs under): the
SAME per-batch `INSERT ... SELECT UNNEST(...)` call this module already
used, with autocommit eliminated (one transaction, matching PR #335),
still cost roughly 2.4-2.8s per 5,000-row/5-column batch on the
production host -- and, decisively, the SAME benchmark on a quiet local
machine (same `duckdb==1.5.5`) cost roughly 0.048s per batch, a ~50x gap
for an identical, purely CPU-bound operation. Cost also scaled
measurably with UNNEST column count on the production host. Both
observations are consistent with either genuine CPU throttling/
contention on the production host (`cpus: 1.0` plus real host
overcommit -- `uptime` load averages up to 4.88 on a 4-CPU host were
observed live during this diagnosis) or an inherent per-call binding
inefficiency in DuckDB's own UNNEST/parameter-marshaling path; this
module does not take a side on which explanation is correct, because the
fix does not depend on the answer -- see the next paragraph.

The fix (this revision) replaces the per-batch UNNEST-insert loop
entirely: `extract_bars_to_parquet` now issues one PostgreSQL `COPY
(SELECT ...) TO STDOUT WITH (FORMAT CSV)` per day-chunk (via a raw
`psycopg.AsyncConnection`, not routed through SQLAlchemy Core -- see
`_psycopg_dsn` and the class docstring for why), streams the raw CSV
bytes straight to a local per-chunk temp file with zero Python-side row
parsing, then bulk-loads every chunk's file into DuckDB with ONE
`INSERT ... SELECT ... FROM read_csv(...)` call across the whole extract
-- not per batch, not per chunk, once. Whether the production slowdown
was host contention or DuckDB's own per-call overhead, the fix is the
same: go from ~148 small, typed, Python-marshaled DuckDB calls to one
large one. `bucket_start` round-trips through the CSV as whole epoch
seconds (a plain `BIGINT`, reconstructed via DuckDB's `to_timestamp` on
the other side), not a textual timestamp, deliberately avoiding a second
possible source of the exact UTC-round-tripping ambiguity `_duckdb_
connect`'s own comment already documents this module hit once before --
1-minute bars have no sub-second component, so epoch seconds is both
simpler and exact. `ExtractManifest`'s phase-timing fields were renamed
to match the new phases (`time_to_first_bytes_seconds`, `postgres_copy_
seconds`, `duckdb_bulk_load_seconds`; `parquet_copy_seconds`/`hash_
seconds` unchanged) -- see `ExtractManifest`'s own docstring for why this
rename is a fully contained change.

Every existing correctness/differential test in this file (offline vs.
live path agreement, multi-day/gap/two-partition/threshold-boundary
scenarios, the compressed-chunk regression test, row-cap and wall-
deadline enforcement, exchange-partition isolation) passed against this
redesign unchanged, run against real local Postgres, before this
revision was committed -- the redesign changes HOW rows move from
Postgres to the Parquet file, never what the file ends up containing.
**A fresh one-day production diagnostic, and then the real HYP-016
freeze, are still required before this can be trusted at production
scale -- the same discipline this section's own predecessor should have
followed more strictly before its "the actual fix" claim turned out to
be wrong.**
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
import psycopg
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .momentum_flow_bidirectional_burst_repository import candidate_query_windows
from .momentum_flow_bidirectional_burst_study import BurstMinute
from .momentum_flow_capture_contract import BYBIT_MOMENTUM_MARKET_TYPE
from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

EXTRACT_QUERY_VERSION = "cex_activity_offline_bars_extract_v3"
_LOOKBACK = timedelta(hours=24)
OFFLINE_CANDIDATE_QUERY_VERSION = "cex_activity_offline_candidate_extreme_minutes_v1"

# Deliberately much shorter than the live path's REPORT_STATEMENT_TIMEOUT
# (300s): that timeout sizes a per-symbol RANGE-window aggregation across
# the full universe. This query is a plain indexed range scan with no
# per-symbol computation at all -- if one day's worth of that cannot finish
# in well under a minute, something is wrong with the index or the table,
# not with the amount of work requested, and failing fast surfaces that
# instead of masking it behind a timeout sized for a different query shape.
# Each chunk's own statement_timeout is actually set to min(this, the time
# actually left in the overall max_wall_seconds budget) -- see
# extract_bars_to_parquet -- so this is a ceiling on top of the real
# per-chunk budget, not the only bound in play.
_EXTRACT_STATEMENT_TIMEOUT_MS = 60_000

# Since the 2026-09-05 COPY/CSV redesign (see the module docstring's own
# "2026-09-05" section), this no longer bounds how many rows accumulate in
# Python at once -- the COPY loop below writes each row's raw bytes
# straight to disk and never accumulates more than one row in Python
# regardless of this value. It now sets the cadence of the wall-deadline
# check (`check_extract_deadline`, which calls `_now`) during a chunk's own
# row stream: checked every _EXTRACT_BATCH_SIZE rows, not every single row,
# so a multi-million-row chunk does not pay a monotonic-clock call per row.
# Row-count enforcement (MAX_EXTRACT_ROWS, the total-size safety net) is
# unrelated to this constant -- that check is a plain int comparison, cheap
# enough to run on every row regardless of this cadence.
_EXTRACT_BATCH_SIZE = 5_000

# Generous headroom guard, this codebase's usual fail-loud-not-silently-
# large convention: HYP-016's own full 9-day/full-universe extract is
# estimated around 13M rows; this is sized to comfortably exceed any
# currently-registered discovery window while still catching a genuinely
# runaway since/until by mistake before it can consume unbounded disk.
MAX_EXTRACT_ROWS = 100_000_000

# A single deadline (started_at + this) is computed once and enforced at
# every phase of extract_bars_to_parquet -- before each chunk, after every
# batch within a chunk, after the chunk loop ends, and before both the
# local COPY and hash steps (colleague review, 2026-09-03 follow-up round 3:
# a per-chunk-only check misses a runaway single/last chunk, and this
# module's own stated goal is specifically avoiding a repeat of the
# 12-minute production I/O incident its docstring describes, not giving a
# runaway extract hours of unbounded runway). Sized against this module's
# own measured post-UNNEST-fix scale (see module docstring: ~22s for
# 2.59M rows, extrapolating to roughly 1-2 minutes for HYP-016's own full
# ~13M-row scale) with generous margin for real production variance, not
# against the old, much-slower executemany-based numbers.
DEFAULT_MAX_EXTRACT_WALL_SECONDS = 15 * 60

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

# No ORDER BY here (colleague review, 2026-09-04, real-production
# diagnosis of the extract's own slowness): the final Parquet output is
# already deterministically ordered by the COPY step below (`COPY (SELECT
# * FROM bars ORDER BY symbol, bucket_start) TO ...`), and chunk
# non-overlap already guarantees no duplicate (symbol, bucket_start) pair
# across chunks -- row arrival order from this SELECT has no effect on the
# final file. Verified via `EXPLAIN (VERBOSE, COSTS, SETTINGS)` against
# real production data (compressed chunk) that the plan is IDENTICAL with
# or without this ORDER BY -- TimescaleDB's own compress_orderby already
# gives the ColumnarScan a stable order, so this clause was not even
# costing anything measurable there, but it is a fragile thing to depend
# on (a different chunk/compression state could force a real Sort), and
# removing a clause with zero purpose is a correct simplification either
# way. See docs/research/cex-activity-discovery-completion-v1.md's own
# "compressed-chunk extract" note for the fuller incident writeup -- the
# ORDER BY was NOT the actual bottleneck found there (the batch-insert
# transaction pattern below was); this is a real but secondary fix.
# bucket_start is cast to whole epoch seconds, not left as a TIMESTAMPTZ
# literal in the CSV: bars are 1-minute buckets with no sub-second
# component, so epoch seconds round-trips exactly with zero ambiguity,
# unlike a textual timestamp whose CSV rendering depends on the Postgres
# session's DateStyle and whose parsing back into DuckDB's TIMESTAMPTZ
# depends on DuckDB's own format auto-detection agreeing with it -- this
# module already hit one real UTC round-tripping bug from exactly that
# class of implicit-format dependency (see _duckdb_connect's own comment).
# `to_timestamp(bigint)` on the DuckDB side (see the bulk INSERT below)
# is the exact, tested inverse. `%(name)s`-style placeholders: psycopg3's
# client-side parameter binding, used verbatim inside a COPY (subquery)
# TO STDOUT statement exactly like any other parameterized query --
# verified directly against a real Postgres instance before this SQL was
# written (colleague review, 2026-09-05, COPY/CSV redesign).
_RAW_BARS_COPY_SQL = """
    COPY (
        SELECT symbol,
               EXTRACT(EPOCH FROM bucket_start)::BIGINT AS bucket_start_epoch_s,
               close_price, buy_total_notional_usd, sell_total_notional_usd
        FROM timeseries.bybit_momentum_bars_1m
        WHERE exchange = %(exchange)s AND market_type = %(market_type)s
          AND capture_version = %(capture_version)s
          AND complete = true AND close_price > 0
          AND bucket_start >= %(since)s AND bucket_start < %(until)s
    ) TO STDOUT WITH (FORMAT CSV)
"""

# Column order here IS the CSV field order DuckDB's read_csv maps
# positionally against (header=False, so there is no header row to match
# by name) -- must stay in exact sync with _RAW_BARS_COPY_SQL's own SELECT
# list above, or values silently land in the wrong column instead of
# raising. Covered by test_offline_extract_matches_live_query_on_
# identical_seeded_data and siblings, which would fail loudly (wrong
# values, not a crash) on this class of drift.
_RAW_BARS_CSV_COLUMNS = {
    "symbol": "VARCHAR",
    "bucket_start_epoch_s": "BIGINT",
    "close_price": "DOUBLE",
    "buy_total_notional_usd": "DOUBLE",
    "sell_total_notional_usd": "DOUBLE",
}

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
    # Phase-level timing breakdown. Originally added 2026-09-04 (real-
    # production diagnosis: raw `SELECT count(*)` for one day of real,
    # TimescaleDB-compressed production data completed in 0.27s, but this
    # function's own full pipeline for the SAME day did not finish inside
    # a 180s budget -- neither the Postgres query nor decompression turned
    # out to be the cause; a real `EXPLAIN (ANALYZE, ...)` of the actual
    # extract SELECT against that same compressed day measured 1.36s), and
    # renamed 2026-09-05 to match the COPY/CSV pipeline redesign (see the
    # module docstring's "2026-09-05" section): the old field names
    # (`postgres_stream_seconds`, `python_conversion_seconds`,
    # `duckdb_insert_seconds`) named phases of the per-batch UNNEST-insert
    # loop that no longer exists. No downstream code reads these specific
    # field names (verified before renaming: only this module and its own
    # tests construct/read them; the report layer only reads `row_count`/
    # `symbol_count`/`parquet_sha256`/`wall_seconds`/`extract_query_
    # version`), so the rename is a fully contained change. These fields
    # exist so any future slow run leaves its own breakdown on the
    # manifest instead of requiring a fresh round of ad-hoc production
    # diagnosis to find out which phase regressed. Default to 0.0 only for
    # synthetic manifests built directly in tests that never call
    # extract_bars_to_parquet itself -- the real constructor below always
    # sets every one of these explicitly.
    time_to_first_bytes_seconds: float = 0.0
    postgres_copy_seconds: float = 0.0
    duckdb_bulk_load_seconds: float = 0.0
    parquet_copy_seconds: float = 0.0
    hash_seconds: float = 0.0


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


def check_extract_deadline(now: float, deadline: float, *, max_wall_seconds: float) -> None:
    """`now`/`deadline` are `time.monotonic()`-comparable floats. Called at
    every phase of `extract_bars_to_parquet` -- before each chunk, after
    every batch within a chunk, after the chunk loop, and before the local
    COPY/hash steps -- against the SAME deadline computed once at the
    function's own start, not a fresh per-call budget (colleague review,
    2026-09-03 follow-up round 3: a check that only ran once per chunk
    could never catch a single slow/stuck chunk, or the local COPY/hash
    phases that follow the last chunk, exceeding the budget)."""
    if now >= deadline:
        raise TimeoutError(
            f"extract_bars_to_parquet exceeded its own max_wall_seconds="
            f"{max_wall_seconds} budget; no single chunk's own statement_timeout "
            "alone catches a runaway TOTAL extract across many chunks, a single slow "
            "chunk, or the local Parquet COPY/hash phases that follow -- narrow "
            "since/until, raise the bound explicitly, or investigate why this run is "
            "taking far longer than a normal HYP-016-scale extract rather than "
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


def _psycopg_dsn(engine: AsyncEngine) -> str:
    """A plain `postgresql://...` DSN psycopg.AsyncConnection.connect can
    use directly, derived from the SAME URL object the SQLAlchemy engine
    itself was already built from -- never a second, independently-
    supplied connection string, so the COPY path below and whatever else
    this engine is used for can never drift on host/port/user/password/
    dbname/query params. `postgresql+psycopg` (SQLAlchemy's own dialect
    notation, set by outcome_repository.async_database_url) is not a
    scheme psycopg's own connect() understands -- swapped back to plain
    `postgresql` here, once."""
    return engine.url.set(drivername="postgresql").render_as_string(hide_password=False)


class OfflineBarsExtractRepository:
    """Postgres -> Parquet extraction. The only piece of this module that
    ever touches production; deliberately as small and cheap a query shape
    as the source table can serve, per this module's own docstring.

    Still constructed from a SQLAlchemy `AsyncEngine`, even though (since
    the 2026-09-05 COPY/CSV redesign) `extract_bars_to_parquet` no longer
    runs any query through it: `_psycopg_dsn` reads the engine's own `url`
    to open a raw `psycopg.AsyncConnection` instead, so the extract's COPY
    can never drift from whatever connection parameters this engine was
    built with. Keeping the `AsyncEngine`-based constructor (rather than
    taking a plain DSN string directly) is deliberate, not an oversight:
    every existing caller and every integration test in this file already
    constructs this class from an engine, and `create_async_engine` never
    opens a real connection until something actually calls `.connect()`
    on it -- which nothing here does anymore -- so keeping it costs
    nothing at runtime beyond one unused pool object. `close()` still
    disposes it for the same reason every other repository in this
    codebase exposes that method: a caller's `finally` block releases
    resources deterministically regardless of which of a repository's
    methods actually used them."""

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

    async def close(self) -> None:
        # Colleague review, 2026-09-04 (research/cex-activity-discovery-
        # completion-v1 wiring in the offline denominator): this class had
        # no way to dispose the engine `from_url` creates -- every other
        # repository in this codebase (e.g. CexActivityDiscoveryRepository)
        # exposes this exact one-liner so a caller's own finally block can
        # release the connection pool deterministically instead of relying
        # on GC.
        await self._engine.dispose()

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
        _now: Callable[[], float] = time.monotonic,
    ) -> ExtractManifest:
        """`_now` is a private testing seam (defaults to `time.monotonic`):
        letting a test substitute a controlled fake clock is what makes it
        possible to prove the SAME deadline is enforced at every phase below
        deterministically, without racing real wall-clock time against a
        genuinely slow chunk."""
        if since >= until:
            raise ValueError("since must be earlier than until")

        started_at = _now()
        deadline = started_at + max_wall_seconds
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_parquet_path = output_path.with_suffix(output_path.suffix + ".tmp")
        # On disk, not :memory: -- DuckDB spills/manages this table's own
        # storage instead of Python (or DuckDB-in-RAM) holding the whole
        # accumulating extract at once. Deleted in the finally block below
        # regardless of outcome; a stale leftover from a previous crashed
        # run is removed first so it can never silently merge into this run.
        tmp_duckdb_path = output_path.with_suffix(output_path.suffix + ".build.duckdb")
        tmp_duckdb_path.unlink(missing_ok=True)
        # A real spill target for the CREATE TABLE/bulk INSERT/COPY/ORDER BY
        # below, once their own working set exceeds _duckdb_connect's
        # memory_limit -- not just the on-disk database file itself, which
        # only bounds the table's steady-state storage, not DuckDB's
        # transient working memory while building/sorting it. Removed in
        # the finally block below like the .duckdb file itself.
        tmp_spill_dir = output_path.with_suffix(output_path.suffix + ".build.spill")
        # One raw CSV file per day-chunk (colleague review, 2026-09-05,
        # COPY/CSV redesign) -- never one giant CSV, so a mid-run failure
        # or a future partial-resume only ever concerns whole chunks, the
        # same unit this module already reasons about everywhere else
        # (candidate_query_windows, the deadline checks below). Cleared and
        # recreated here so a stale leftover from a previous crashed run
        # can never silently merge into this run's own bulk load, same
        # discipline as tmp_duckdb_path.unlink above.
        tmp_csv_dir = output_path.with_suffix(output_path.suffix + ".build.csv")
        shutil.rmtree(tmp_csv_dir, ignore_errors=True)
        tmp_csv_dir.mkdir(parents=True, exist_ok=True)

        total_rows = 0
        time_to_first_bytes_seconds = 0.0
        postgres_copy_seconds = 0.0
        first_bytes_seen = False
        chunk_csv_paths: list[Path] = []

        # This is the ONLY connection this function opens to Postgres, a
        # plain psycopg connection (not routed through self._engine, see
        # _psycopg_dsn and the class docstring), held open across every
        # chunk -- COPY (subquery) TO STDOUT is not exposed by SQLAlchemy
        # Core, and psycopg's own native async COPY API is what streams raw
        # CSV bytes straight to a local file with zero Python-side row
        # parsing/type conversion, replacing the per-batch UNNEST-insert
        # path this redesign removes (colleague review, 2026-09-05: a real
        # production incident traced the old path's per-batch DuckDB
        # UNNEST-bind call itself -- not Postgres, not TimescaleDB
        # decompression, both independently ruled out by real EXPLAIN
        # ANALYZE evidence -- to roughly 50x its expected cost on the
        # production host relative to an identical benchmark on a quiet
        # machine; this redesign replaces ~148 per-day-chunk batches of
        # typed Python-list-to-DuckDB-LIST binding with ONE bulk
        # `read_csv` call per extract, regardless of which theory of the
        # production slowdown is correct -- fewer, larger DuckDB calls with
        # far less per-call Python/FFI marshaling either way).
        # ONE outer try/finally spans everything from here through the final
        # rename below (colleague review, 2026-09-05 follow-up: the previous
        # version's cleanup was split across the COPY-phase's own finally
        # and the DuckDB-phase's own finally, with nothing covering either
        # the COPY phase's own exception path -- tmp_csv_dir was created
        # and populated BEFORE that phase's try/finally, which only closed
        # the psycopg connection, never removed it -- or the gap between
        # the DuckDB phase finishing and the final hash/rename. An
        # exception raised ANYWHERE in this function, including a row-cap/
        # deadline error mid-COPY or the deadline check right before
        # hashing, must still leave no `.build.*` artifact behind).
        # `output_path` itself is never touched here -- only ever removed
        # via `tmp_parquet_path.unlink`, and by the time a successful run
        # reaches `tmp_parquet_path.replace(output_path)` below,
        # `tmp_parquet_path` no longer exists to unlink (missing_ok=True
        # makes that a no-op), so a finished result is never at risk.
        try:
            psycopg_conn = await psycopg.AsyncConnection.connect(_psycopg_dsn(self._engine))
            try:
                # Chunked over [since - 24h, until), not [since, until):
                # this extractor copies rows, it does not compute a
                # per-chunk window function the way the live path does, so
                # it never needs to re-request the same calendar day's rows
                # once per output chunk -- one non-overlapping partition of
                # the FULL needed range (already including the lookback) is
                # both simpler and halves the redundant Postgres I/O the
                # first version of this function had (every day fetched
                # exactly once, not twice from two adjacent chunks' own
                # lookback windows).
                for chunk_index, (chunk_since, chunk_until) in enumerate(
                    candidate_query_windows(since - _LOOKBACK, until)
                ):
                    # Checked against the SAME deadline computed once above,
                    # not a fresh per-chunk budget -- raises immediately if
                    # the PREVIOUS chunk(s) already exhausted it (colleague
                    # review, 2026-09-03 follow-up round 3).
                    now = _now()
                    check_extract_deadline(now, deadline, max_wall_seconds=max_wall_seconds)
                    # This chunk's own statement_timeout is capped at
                    # whatever time is ACTUALLY left in the overall budget,
                    # not just the fixed _EXTRACT_STATEMENT_TIMEOUT_MS
                    # regardless of how little time remains -- so a single
                    # slow/stuck chunk (the only one, or the last one) is
                    # cut off by Postgres itself once the real budget runs
                    # out, rather than being allowed its own full 60s (or
                    # more) on top of an already-exhausted deadline.
                    # Postgres applies statement_timeout to every individual
                    # data-transfer step of an in-progress COPY, not just
                    # the initial query, so this also bounds a hang
                    # mid-stream, not only a slow query plan.
                    # max(1, ...): Postgres treats a statement_timeout of
                    # exactly 0 as DISABLED (no timeout at all), the
                    # opposite of what a near-zero remaining budget should
                    # mean here -- the check_extract_deadline call above
                    # already guarantees deadline - now > 0 at this point,
                    # but that could still round down to 0ms once truncated
                    # to whole milliseconds.
                    remaining_ms = max(
                        1, min(_EXTRACT_STATEMENT_TIMEOUT_MS, int((deadline - now) * 1000))
                    )
                    chunk_csv_path = tmp_csv_dir / f"{chunk_index:05d}.csv"
                    chunk_csv_paths.append(chunk_csv_path)
                    copy_started_at = _now()
                    chunk_row_count = 0
                    # Threshold this chunk's own deadline-check cadence
                    # crosses next, not a `% _EXTRACT_BATCH_SIZE == 0` test
                    # (colleague review, 2026-09-05 follow-up round 2): a
                    # monotonically-advancing `>=` threshold, stepped past
                    # in a `while` loop if a single iteration's row count
                    # ever crosses more than one boundary at once, is
                    # correct regardless of how many rows arrive per
                    # iteration of the COPY loop below -- whereas an exact-
                    # multiple check would silently skip every remaining
                    # boundary for the rest of the chunk the first time a
                    # single iteration's count overshoots one. psycopg's
                    # AsyncCopy is documented and, verified directly against
                    # a real Postgres instance at 200,000-row/2KB-row scale,
                    # observed to yield exactly one CSV row per iteration
                    # (each call wraps libpq's PQgetCopyData, itself
                    # documented to return exactly one COPY row per call) --
                    # so this can only ever advance by 1 in practice today,
                    # but the threshold form costs nothing extra and does
                    # not depend on that guarantee holding forever.
                    next_deadline_check_at = _EXTRACT_BATCH_SIZE
                    async with psycopg_conn.transaction(), psycopg_conn.cursor() as cursor:
                        await cursor.execute("SET TRANSACTION READ ONLY")
                        # set_config(..., true) scopes to this transaction
                        # only (the same as SET LOCAL), matching the
                        # previous SQLAlchemy-routed version's own choice: a
                        # real function call with a genuine bind parameter,
                        # not a SET statement's less consistently
                        # parameterizable value position.
                        await cursor.execute(
                            "SELECT set_config('statement_timeout', %s, true)",
                            (f"{remaining_ms}ms",),
                        )
                        with chunk_csv_path.open("wb") as csv_file:
                            async with cursor.copy(
                                _RAW_BARS_COPY_SQL,
                                {
                                    "exchange": exchange,
                                    "market_type": market_type,
                                    "capture_version": capture_version,
                                    "since": chunk_since,
                                    "until": chunk_until,
                                },
                            ) as copy:
                                # Raw, undecoded CSV bytes per iteration, no
                                # Python-side field parsing/type conversion
                                # at all here, unlike the per-batch Python-
                                # list construction the UNNEST-insert path
                                # this replaces required. Newline-counting
                                # each iteration's bytes directly off the
                                # wire, instead of a second pass over the
                                # finished file, is what lets total_rows and
                                # max_extract_rows stay enforced
                                # incrementally without ever materializing a
                                # row in Python -- and, unlike the deadline-
                                # check cadence above, is correct no matter
                                # how many rows one iteration's bytes
                                # contain, since it counts actual `\n` bytes
                                # rather than assuming one iteration is one
                                # row.
                                async for data in copy:
                                    if not first_bytes_seen:
                                        first_bytes_seen = True
                                        time_to_first_bytes_seconds = _now() - started_at
                                    raw = bytes(data)
                                    csv_file.write(raw)
                                    row_delta = raw.count(b"\n")
                                    chunk_row_count += row_delta
                                    total_rows += row_delta
                                    # The row-count check is a plain int
                                    # comparison, cheap enough to run every
                                    # iteration; the wall-deadline check
                                    # below calls _now() and is throttled to
                                    # roughly
                                    # every _EXTRACT_BATCH_SIZE rows instead,
                                    # so a multi-million-row chunk does not
                                    # pay a monotonic-clock call per row.
                                    check_extract_row_count(total_rows, max_extract_rows)
                                    if chunk_row_count >= next_deadline_check_at:
                                        check_extract_deadline(
                                            _now(), deadline, max_wall_seconds=max_wall_seconds
                                        )
                                        while next_deadline_check_at <= chunk_row_count:
                                            next_deadline_check_at += _EXTRACT_BATCH_SIZE
                    postgres_copy_seconds += _now() - copy_started_at
                    # Checked once more after the whole chunk finishes, not
                    # just at the _EXTRACT_BATCH_SIZE-row cadence above -- a
                    # chunk whose row count never reaches that cadence's
                    # first threshold (or a chunk with very few rows) must
                    # still be caught before the next chunk starts
                    # (colleague review, 2026-09-03 follow-up round 3,
                    # preserved through the 2026-09-05 redesign).
                    check_extract_deadline(_now(), deadline, max_wall_seconds=max_wall_seconds)
            finally:
                await psycopg_conn.close()

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
                # ONE bulk INSERT reading every non-empty chunk CSV at once
                # (colleague review, 2026-09-05, COPY/CSV redesign):
                # replaces the ~148 per-batch UNNEST-bind calls a real
                # HYP-016-scale extract previously made with a single
                # DuckDB `read_csv` call over the whole file list -- the
                # standard DuckDB bulk-load pattern, and a strict superset
                # of the 2026-09-04 single-transaction fix this replaces
                # (one DuckDB statement total, not merely one transaction
                # wrapping many statements). Empty chunk files (a day with
                # zero matching rows -- routine, not an error) are filtered
                # out before the call: DuckDB's read_csv on a zero-byte file
                # with an explicit schema and no header has nothing to
                # infer from and is a needless edge case to feed it when
                # the empty list itself is already meaningful (an extract
                # whose every chunk is empty simply performs no INSERT at
                # all, correctly leaving `bars` empty rather than raising).
                # The PRIMARY KEY above still applies to a bulk INSERT
                # exactly like it did to the old per-batch inserts --
                # chunks are non-overlapping by construction (see the
                # comment above the chunk loop), so no chunk's file can
                # ever contain a (symbol, bucket_start) pair another
                # chunk's file already does; a genuine duplicate still
                # raises loudly instead of being silently dropped.
                non_empty_csv_paths = [
                    path.as_posix() for path in chunk_csv_paths if path.stat().st_size > 0
                ]
                bulk_load_started_at = _now()
                if non_empty_csv_paths:
                    duckdb_conn.execute(
                        """
                        INSERT INTO bars
                        SELECT
                            symbol,
                            to_timestamp(bucket_start_epoch_s) AS bucket_start,
                            close_price,
                            buy_total_notional_usd,
                            sell_total_notional_usd
                        FROM read_csv(?, columns=?, header=False)
                        """,
                        [non_empty_csv_paths, _RAW_BARS_CSV_COLUMNS],
                    )
                duckdb_bulk_load_seconds = _now() - bulk_load_started_at
                # The deadline is enforced once more here, after the whole
                # bulk load, before starting the local COPY/hash phases
                # below -- those phases never touch production (they are
                # 100% local Parquet-write/hashing work against the on-disk
                # DuckDB file already built above), so they carry none of
                # the production-I/O risk max_wall_seconds primarily exists
                # to bound; this check's job is only to fail fast rather
                # than spend time on COPY/hashing when the budget is
                # already spent, not to preempt COPY/hashing mid-flight
                # once they've started (that would need running DuckDB's
                # synchronous COPY in a killable subprocess -- a materially
                # bigger redesign not justified by what these phases
                # actually cost, measured at low single-digit seconds even
                # at multi-million-row scale).
                check_extract_deadline(_now(), deadline, max_wall_seconds=max_wall_seconds)
                copy_started_at = _now()
                duckdb_conn.execute(
                    "COPY (SELECT * FROM bars ORDER BY symbol, bucket_start) "
                    "TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
                    [tmp_parquet_path.as_posix()],
                )
                parquet_copy_seconds = _now() - copy_started_at
                check_extract_deadline(_now(), deadline, max_wall_seconds=max_wall_seconds)
                count_row = duckdb_conn.execute(
                    "SELECT count(*), count(DISTINCT symbol) FROM bars"
                ).fetchone()
                # An unqualified aggregate with no GROUP BY always returns
                # exactly one row; this is a type-narrowing guard, not a
                # real runtime possibility.
                assert count_row is not None
                row_count, symbol_count = count_row
            finally:
                duckdb_conn.close()

            # Checked once more before the (also 100% local, no production
            # I/O) streamed-hash pass over the finished Parquet file, for
            # the same fail-fast-rather-than-preempt-mid-flight reasoning as
            # the COPY checkpoint above.
            check_extract_deadline(_now(), deadline, max_wall_seconds=max_wall_seconds)
            hash_started_at = _now()
            parquet_sha256 = _sha256_file(tmp_parquet_path)
            hash_seconds = _now() - hash_started_at
            parquet_bytes = tmp_parquet_path.stat().st_size
            tmp_parquet_path.replace(output_path)
            wall_seconds = _now() - started_at
        finally:
            # Unconditional, regardless of where (or whether) an exception
            # was raised above -- see this block's own opening comment.
            # unlink(missing_ok=True) on tmp_parquet_path is a no-op after
            # a successful run (already renamed to output_path above).
            tmp_duckdb_path.unlink(missing_ok=True)
            shutil.rmtree(tmp_spill_dir, ignore_errors=True)
            shutil.rmtree(tmp_csv_dir, ignore_errors=True)
            tmp_parquet_path.unlink(missing_ok=True)

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
            time_to_first_bytes_seconds=time_to_first_bytes_seconds,
            postgres_copy_seconds=postgres_copy_seconds,
            duckdb_bulk_load_seconds=duckdb_bulk_load_seconds,
            parquet_copy_seconds=parquet_copy_seconds,
            hash_seconds=hash_seconds,
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
