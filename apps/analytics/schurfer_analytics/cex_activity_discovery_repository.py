"""Bounded PostgreSQL reads for the CEX activity discovery report."""

from __future__ import annotations

import json
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
    from collections.abc import Sequence

PATH_BATCH_SIZE = 200
PATH_QUERY_VERSION = "cex_activity_exact_native_path_v1"
REPORT_STATEMENT_TIMEOUT = "300s"


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
    ) -> dict[str, ExactPricePath]:
        if batch_size <= 0:
            raise ValueError("path batch size must be positive")
        if len({item.request_id for item in requests}) != len(requests):
            raise ValueError("path request ids must be unique")
        paths: dict[str, ExactPricePath] = {}
        for start in range(0, len(requests), batch_size):
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
            async with self._engine.connect() as connection, connection.begin():
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                await connection.execute(
                    text("SELECT set_config('statement_timeout', :timeout, true)"),
                    {"timeout": REPORT_STATEMENT_TIMEOUT},
                )
                result = await connection.execute(_PATH_STATEMENT, parameters)
                rows = result.all()
            for row in rows:
                path = ExactPricePath(
                    request_id=str(row.request_id),
                    symbol=str(row.symbol),
                    trigger_at=row.trigger_at,
                    entry_at=row.entry_at,
                    entry_price=float(row.entry_price) if row.entry_price is not None else None,
                    observed_minutes=int(row.observed_minutes or 0),
                    max_high=float(row.max_high) if row.max_high is not None else None,
                    min_low=float(row.min_low) if row.min_low is not None else None,
                    first_up_25_at=row.first_up_25_at,
                    first_down_25_at=row.first_down_25_at,
                )
                paths[path.request_id] = path
        return paths

    async def close(self) -> None:
        await self._engine.dispose()


def report_maturity_at(until: datetime) -> datetime:
    return until + timedelta(minutes=OUTCOME_HORIZON_MINUTES + 1)
