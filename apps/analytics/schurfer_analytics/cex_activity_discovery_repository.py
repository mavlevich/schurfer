"""Bounded PostgreSQL reads for the CEX activity discovery report."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .cex_activity_discovery import (
    OUTCOME_HORIZON_MINUTES,
    PRIMARY_MOVE_PCT,
    ExactPricePath,
    PathRequest,
)
from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

PATH_BATCH_SIZE = 200
PATH_QUERY_VERSION = "cex_activity_exact_native_path_v1"
# radar_outcome_discovery_repository.py also imports this exact string
# constant (its own SET statement_timeout call passes it as-is) -- kept
# unchanged so that module's own unrelated behavior stays untouched.
# REPORT_STATEMENT_TIMEOUT_MS below is the same 300s expressed as an int,
# used only by this module's own deadline-capping arithmetic (see
# fetch_exact_paths), which needs a number, not a Postgres interval string.
REPORT_STATEMENT_TIMEOUT = "300s"
REPORT_STATEMENT_TIMEOUT_MS = 300_000

# Colleague review, 2026-09-03 (research/cex-activity-discovery-completion-v1
# planning): fetch_exact_paths previously had no budget covering its own
# TOTAL runtime across every batch -- REPORT_STATEMENT_TIMEOUT_MS only
# bounded each individual batch's own query. Mirrors the exact deadline
# pattern momentum_flow_bidirectional_burst_offline_repository.py's own
# extract_bars_to_parquet already established (colleague review, same day,
# earlier round on that module): one deadline computed once, checked
# before every batch, each batch's own statement_timeout capped to
# whatever time is actually left rather than the fixed value regardless of
# how little time remains. 15 minutes is generous for even several
# thousand path requests at PATH_BATCH_SIZE -- HYP-016's own real request
# count (bounded by MAX_PATH_REQUESTS at the report layer) is far smaller
# than the scale that would ever approach this budget.
DEFAULT_PATH_MAX_WALL_SECONDS = 15 * 60


_PATH_STATEMENT = text("""
        WITH requested AS (
            SELECT request_id, symbol, trigger_at, entry_at
            FROM jsonb_to_recordset(CAST(:requests_json AS jsonb)) AS request_rows(
                request_id text,
                symbol text,
                trigger_at timestamptz,
                entry_at timestamptz
            )
        ),
        entries AS (
            SELECT r.request_id, r.symbol, r.trigger_at,
                   r.entry_at,
                   e.open_price AS entry_price
            FROM requested r
            LEFT JOIN timeseries.bybit_momentum_bars_1m e
              ON e.exchange = :exchange
             AND e.market_type = :market_type
             AND e.capture_version = :capture_version
             AND e.symbol = r.symbol
             AND e.bucket_start = r.entry_at
             AND e.price_complete IS true
             AND e.open_price > 0
        )
        SELECT e.request_id, e.symbol, e.trigger_at, e.entry_at, e.entry_price,
               count(p.bucket_start) FILTER (
                   WHERE p.price_complete IS true
                     AND p.open_price > 0 AND p.high_price > 0
                     AND p.low_price > 0 AND p.close_price > 0
               ) AS observed_minutes,
               max(p.high_price) FILTER (WHERE p.price_complete IS true) AS max_high,
               min(p.low_price) FILTER (WHERE p.price_complete IS true) AS min_low,
               min(p.bucket_start) FILTER (
                   WHERE p.price_complete IS true
                     AND e.entry_price IS NOT NULL
                     AND p.high_price >= e.entry_price * :up_multiplier
               ) AS first_up_25_at,
               min(p.bucket_start) FILTER (
                   WHERE p.price_complete IS true
                     AND e.entry_price IS NOT NULL
                     AND p.low_price <= e.entry_price * :down_multiplier
               ) AS first_down_25_at
        FROM entries e
        LEFT JOIN timeseries.bybit_momentum_bars_1m p
          ON p.exchange = :exchange
         AND p.market_type = :market_type
         AND p.capture_version = :capture_version
         AND p.symbol = e.symbol
         AND p.bucket_start >= e.entry_at
         AND p.bucket_start < e.entry_at + make_interval(mins => :outcome_minutes)
        GROUP BY e.request_id, e.symbol, e.trigger_at, e.entry_at, e.entry_price
        ORDER BY e.request_id
    """)


def check_path_deadline(now: float, deadline: float, *, max_wall_seconds: float) -> None:
    """`now`/`deadline` are `time.monotonic()`-comparable floats. Checked
    once per batch, against the SAME deadline computed once at
    `fetch_exact_paths`'s own start, not a fresh per-batch budget."""
    if now >= deadline:
        raise TimeoutError(
            f"fetch_exact_paths exceeded its own max_wall_seconds={max_wall_seconds} "
            "budget -- no single batch's own statement_timeout alone catches a runaway "
            "TOTAL fetch across many batches; narrow the request count, raise the bound "
            "explicitly, or investigate why this run is taking far longer than normal "
            "rather than letting it hold a production connection pool slot indefinitely"
        )


class CexActivityDiscoveryRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, database_url: str) -> CexActivityDiscoveryRepository:
        return cls(
            create_async_engine(
                async_database_url(database_url),
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
            )
        )

    async def database_now(self) -> datetime:
        async with self._engine.connect() as connection:
            value = (await connection.execute(text("SELECT now()"))).scalar_one()
        if not isinstance(value, datetime):
            raise TypeError("database now() did not return a datetime")
        return value

    async def fetch_exact_paths(
        self,
        *,
        exchange: str,
        market_type: str,
        capture_version: str,
        requests: Sequence[PathRequest],
        batch_size: int = PATH_BATCH_SIZE,
        max_wall_seconds: float = DEFAULT_PATH_MAX_WALL_SECONDS,
        _now: Callable[[], float] = time.monotonic,
        _after_batch: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, ExactPricePath]:
        """`_now` is a private testing seam (defaults to `time.monotonic`):
        lets a test substitute a controlled fake clock to prove the
        deadline survives past the first pre-batch check, deterministically,
        without racing real wall-clock time against an artificially slow
        batch -- same pattern established in
        momentum_flow_bidirectional_burst_offline_repository.py's own
        extract_bars_to_parquet. `_after_batch` is a second private testing
        seam (default `None`, a no-op): awaited once after each batch's own
        rows are consumed, letting a test commit a concurrent write via a
        SEPARATE connection between two batches of the SAME call, to prove
        a later batch's own query still cannot see it (see
        `test_fetch_exact_paths_holds_one_snapshot_across_batches`).

        ALL batches run inside ONE `REPEATABLE READ`, read-only transaction
        (colleague review, 2026-09-04 follow-up round): the previous
        version opened a brand-new connection/transaction PER BATCH, so a
        request's own signal path and its matched control path -- landing
        in different batches once `len(requests) > batch_size`, which is
        already true for any real-scale run -- could observe genuinely
        different data if a backfill/correction landed mid-fetch, making
        the earlier "one call closes this" comment factually wrong: one
        Python-level call was never the same thing as one Postgres
        snapshot. `REPEATABLE READ`'s own snapshot is captured at the
        FIRST query inside a transaction and held for every later query in

        ALL batches run inside ONE `REPEATABLE READ`, read-only transaction
        (colleague review, 2026-09-04 follow-up round): the previous
        version opened a brand-new connection/transaction PER BATCH, so a
        request's own signal path and its matched control path -- landing
        in different batches once `len(requests) > batch_size`, which is
        already true for any real-scale run -- could observe genuinely
        different data if a backfill/correction landed mid-fetch, making
        the earlier "one call closes this" comment factually wrong: one
        Python-level call was never the same thing as one Postgres
        snapshot. `REPEATABLE READ`'s own snapshot is captured at the
        FIRST query inside a transaction and held for every later query in
        that SAME transaction, regardless of batch boundaries or request
        order -- so every batch this call issues, whatever order they run
        in, now genuinely reads the same consistent view of the data."""
        if batch_size <= 0:
            raise ValueError("path batch size must be positive")
        if len({item.request_id for item in requests}) != len(requests):
            raise ValueError("path request ids must be unique")
        if not requests:
            return {}
        started_at = _now()
        deadline = started_at + max_wall_seconds
        paths: dict[str, ExactPricePath] = {}
        async with self._engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="REPEATABLE READ")
            async with connection.begin():
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                for start in range(0, len(requests), batch_size):
                    now = _now()
                    check_path_deadline(now, deadline, max_wall_seconds=max_wall_seconds)
                    # This batch's own statement_timeout is capped at
                    # whatever time is ACTUALLY left in the overall budget,
                    # not the fixed REPORT_STATEMENT_TIMEOUT_MS regardless
                    # of how little time remains -- max(1, ...) since
                    # Postgres treats a statement_timeout of exactly 0 as
                    # DISABLED, the opposite of what a near-zero remaining
                    # budget should mean here. set_config's own third
                    # argument (`true` = LOCAL) still works mid-transaction
                    # here -- re-issuing it before each batch updates the
                    # timeout for that batch's own statement without
                    # needing a fresh transaction.
                    remaining_ms = max(
                        1, min(REPORT_STATEMENT_TIMEOUT_MS, int((deadline - now) * 1000))
                    )
                    batch = tuple(requests[start : start + batch_size])
                    parameters: dict[str, object] = {
                        "exchange": exchange,
                        "market_type": market_type,
                        "capture_version": capture_version,
                        "up_multiplier": 1 + PRIMARY_MOVE_PCT / 100,
                        "down_multiplier": 1 - PRIMARY_MOVE_PCT / 100,
                        "outcome_minutes": OUTCOME_HORIZON_MINUTES,
                        "requests_json": json.dumps(
                            [
                                {
                                    "request_id": request.request_id,
                                    "symbol": request.symbol,
                                    "trigger_at": request.trigger_at.isoformat(),
                                    "entry_at": request.entry_at.isoformat(),
                                }
                                for request in batch
                            ],
                            separators=(",", ":"),
                        ),
                    }
                    await connection.execute(
                        text("SELECT set_config('statement_timeout', :timeout, true)"),
                        {"timeout": f"{remaining_ms}ms"},
                    )
                    result = await connection.execute(_PATH_STATEMENT, parameters)
                    rows = result.all()
                    for row in rows:
                        path = ExactPricePath(
                            request_id=str(row.request_id),
                            symbol=str(row.symbol),
                            trigger_at=row.trigger_at,
                            entry_at=row.entry_at,
                            entry_price=(
                                float(row.entry_price) if row.entry_price is not None else None
                            ),
                            observed_minutes=int(row.observed_minutes or 0),
                            max_high=float(row.max_high) if row.max_high is not None else None,
                            min_low=float(row.min_low) if row.min_low is not None else None,
                            first_up_25_at=row.first_up_25_at,
                            first_down_25_at=row.first_down_25_at,
                        )
                        paths[path.request_id] = path
                    if _after_batch is not None:
                        await _after_batch()
        return paths

    async def close(self) -> None:
        await self._engine.dispose()


def report_maturity_at(latest_request_entry_at: datetime) -> datetime:
    """Colleague review, 2026-09-03 (research/cex-activity-discovery-
    completion-v1 planning): this previously took `until` (the discovery
    window's own exclusive end) directly, silently assuming every
    request's own entry_at falls at or before it. Two things break that
    assumption -- an episode's own entry_at is `trigger_at + 1 minute`,
    which for a burst triggered in the LAST minute before `until` can land
    strictly past it, and control requests can be offset up to
    CONTROL_SEARCH_DAYS forward of their own episode. The caller must pass
    the actual maximum entry_at across EVERY signal and control request
    this run will fetch (`max(request.entry_at for request in ...)`), not
    `until` -- maturity is "the latest real outcome window this run
    depends on has fully closed", not "the discovery window's own
    boundary plus the standard horizon"."""
    return latest_request_entry_at + timedelta(minutes=OUTCOME_HORIZON_MINUTES + 1)
