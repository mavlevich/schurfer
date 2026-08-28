"""Bounded point-in-time WATCH reads for the radar outcome foundation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .cex_activity_discovery_repository import REPORT_STATEMENT_TIMEOUT
from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from datetime import datetime

WATCH_QUERY_VERSION = "exact_watch_decisions_by_native_contract_v1"


@dataclass(frozen=True)
class RadarWatchSignal:
    watch_id: str
    exchange: str
    market_type: str
    symbol: str
    capture_version: str
    watch_version: str
    bucket_start: datetime
    decision_at: datetime
    entry_at: datetime
    price_return_60m_pct: float | None
    oi_growth_60m_pct: float | None
    buy_imbalance_15m: float | None
    flow_acceleration_15m_vs_prior_45m: float | None


class RadarOutcomeDiscoveryRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, database_url: str) -> RadarOutcomeDiscoveryRepository:
        return cls(
            create_async_engine(
                async_database_url(database_url),
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
            )
        )

    async def fetch_watch_signals(
        self,
        *,
        exchange: str,
        market_type: str,
        capture_version: str,
        watch_version: str,
        since: datetime,
        until: datetime,
    ) -> tuple[RadarWatchSignal, ...]:
        if since >= until:
            raise ValueError("since must be earlier than until")
        async with self._engine.connect() as connection, connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            await connection.execute(
                text("SELECT set_config('statement_timeout', :timeout, true)"),
                {"timeout": REPORT_STATEMENT_TIMEOUT},
            )
            result = await connection.execute(
                text("""
                    SELECT watch_id::text AS watch_id,
                           exchange, market_type, symbol, capture_version, watch_version,
                           bucket_start, decision_at,
                           date_trunc('minute', decision_at) + INTERVAL '1 minute' AS entry_at,
                           price_return_60m_pct, oi_growth_60m_pct,
                           buy_imbalance_15m, flow_acceleration_15m_vs_prior_45m
                    FROM timeseries.momentum_flow_watch_evaluations_1m
                    WHERE exchange = :exchange
                      AND market_type = :market_type
                      AND capture_version = :capture_version
                      AND watch_version = :watch_version
                      AND bucket_start >= :since AND bucket_start < :until
                      AND decision_status = 'watch'
                      AND quality_ready IS true
                      AND raw_qualified IS true
                      AND watch_id IS NOT NULL
                    ORDER BY decision_at, symbol, watch_id
                """),
                {
                    "exchange": exchange,
                    "market_type": market_type,
                    "capture_version": capture_version,
                    "watch_version": watch_version,
                    "since": since,
                    "until": until,
                },
            )
            rows = result.all()
        return tuple(
            RadarWatchSignal(
                watch_id=str(row.watch_id),
                exchange=str(row.exchange),
                market_type=str(row.market_type),
                symbol=str(row.symbol),
                capture_version=str(row.capture_version),
                watch_version=str(row.watch_version),
                bucket_start=row.bucket_start,
                decision_at=row.decision_at,
                entry_at=row.entry_at,
                price_return_60m_pct=(
                    float(row.price_return_60m_pct)
                    if row.price_return_60m_pct is not None
                    else None
                ),
                oi_growth_60m_pct=(
                    float(row.oi_growth_60m_pct) if row.oi_growth_60m_pct is not None else None
                ),
                buy_imbalance_15m=(
                    float(row.buy_imbalance_15m) if row.buy_imbalance_15m is not None else None
                ),
                flow_acceleration_15m_vs_prior_45m=(
                    float(row.flow_acceleration_15m_vs_prior_45m)
                    if row.flow_acceleration_15m_vs_prior_45m is not None
                    else None
                ),
            )
            for row in rows
        )

    async def close(self) -> None:
        await self._engine.dispose()
