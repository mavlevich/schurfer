"""Repeatable-read repository for the early_momentum flow-feature discovery."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .early_momentum_net_evidence import (
    ACCOUNTING_VERSION,
    EXPECTED_CONTRACT_SHA256_HEX,
    EXPECTED_SETUP_CONTEXT_STRATEGY,
    STRATEGY_NAME,
    STRATEGY_VERSION,
)
from .early_momentum_unused_flow_features import RawFeatureRow
from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.engine import RowMapping
    from sqlalchemy.ext.asyncio import AsyncConnection


FEATURE_ROWS_SQL = text("""
WITH cohort AS (
    SELECT
        t.id AS trade_id,
        e.episode_id::text AS episode_id,
        e.cluster_key,
        e.source_exchange,
        e.source_native_id,
        NULLIF(e.features->>'bucket_start', '')::timestamptz AS decision_bucket,
        e.features->>'market_type' AS market_type,
        e.features->>'capture_version' AS capture_version,
        e.features->>'universe_version' AS universe_version,
        t.entry_at,
        t.exit_at,
        t.net_pnl_pct::double precision AS net_pnl_pct,
        t.net_pnl_usd::double precision AS net_pnl_usd
    FROM app.early_momentum_episodes AS e
    JOIN app.strategies AS s ON s.id = e.strategy_id
    JOIN app.trades AS t ON t.episode_id = e.episode_id
    WHERE s.name = :strategy_name
      AND s.version = :strategy_version
      AND e.contract_sha256 = decode(:contract_sha256_hex, 'hex')
      AND e.armed_at >= :cohort_start
      AND e.armed_at < :cohort_end
      AND t.status = 'closed'
      AND t.accounting_status = 'complete'
      AND t.accounting_version = :accounting_version
      AND t.side = 'long'
      AND t.setup_context->>'paper' = 'true'
      AND t.setup_context->>'strategy' = :setup_context_strategy
      AND t.exit_at IS NOT NULL
      AND t.net_pnl_pct IS NOT NULL
      AND t.net_pnl_usd IS NOT NULL
),
scoped_bars AS (
    SELECT *
    FROM timeseries.bybit_momentum_bars_1m
    WHERE bucket_start >= :bars_start
      AND bucket_start < :cohort_end
),
joined AS (
    SELECT
        c.*,
        b.bucket_start,
        b.price_complete,
        b.trades_complete,
        b.open_interest_complete,
        b.buy_total_notional_usd,
        b.sell_total_notional_usd,
        b.buy_max_10s_notional_usd,
        b.sell_max_10s_notional_usd,
        b.open_interest_value,
        lag(b.bucket_start) OVER (
            PARTITION BY c.episode_id ORDER BY b.bucket_start
        ) AS previous_bucket
    FROM cohort AS c
    LEFT JOIN scoped_bars AS b
      ON b.exchange = c.source_exchange
     AND b.market_type = c.market_type
     AND b.symbol = c.source_native_id
     AND b.capture_version = c.capture_version
     AND b.universe_version = c.universe_version
     AND b.bucket_start BETWEEN c.decision_bucket - interval '120 minutes'
                            AND c.decision_bucket
)
SELECT
    trade_id,
    episode_id,
    cluster_key,
    source_exchange,
    source_native_id,
    decision_bucket,
    entry_at,
    exit_at,
    net_pnl_pct,
    net_pnl_usd,
    count(bucket_start)::integer AS bars_observed,
    count(DISTINCT bucket_start)::integer AS distinct_buckets,
    min(bucket_start) AS first_bucket,
    max(bucket_start) AS last_bucket,
    max(extract(epoch FROM bucket_start - previous_bucket))::double precision
        AS max_gap_seconds,
    count(*) FILTER (
        WHERE price_complete AND trades_complete AND open_interest_complete
    )::integer AS complete_bars,
    sum(buy_total_notional_usd) FILTER (
        WHERE bucket_start > decision_bucket - interval '15 minutes'
    )::double precision AS buy_15m,
    sum(sell_total_notional_usd) FILTER (
        WHERE bucket_start > decision_bucket - interval '15 minutes'
    )::double precision AS sell_15m,
    sum(buy_total_notional_usd) FILTER (
        WHERE bucket_start <= decision_bucket - interval '15 minutes'
    )::double precision AS buy_prior,
    sum(sell_total_notional_usd) FILTER (
        WHERE bucket_start <= decision_bucket - interval '15 minutes'
    )::double precision AS sell_prior,
    sum(buy_max_10s_notional_usd) FILTER (
        WHERE bucket_start > decision_bucket - interval '15 minutes'
    )::double precision AS buy_burst_15m,
    sum(sell_max_10s_notional_usd) FILTER (
        WHERE bucket_start > decision_bucket - interval '15 minutes'
    )::double precision AS sell_burst_15m,
    (array_agg(open_interest_value ORDER BY bucket_start DESC))[1]::double precision
        AS oi_value_latest
FROM joined
GROUP BY
    trade_id, episode_id, cluster_key, source_exchange, source_native_id,
    decision_bucket, entry_at, exit_at, net_pnl_pct, net_pnl_usd
ORDER BY trade_id
""")


def _row(row: RowMapping) -> RawFeatureRow:
    return RawFeatureRow(
        trade_id=int(row["trade_id"]),
        episode_id=str(row["episode_id"]),
        cluster_key=str(row["cluster_key"]),
        source_exchange=str(row["source_exchange"]),
        source_native_id=str(row["source_native_id"]),
        decision_bucket=row["decision_bucket"],
        entry_at=row["entry_at"],
        exit_at=row["exit_at"],
        net_pnl_pct=float(row["net_pnl_pct"]),
        net_pnl_usd=float(row["net_pnl_usd"]),
        bars_observed=int(row["bars_observed"]),
        distinct_buckets=int(row["distinct_buckets"]),
        first_bucket=row["first_bucket"],
        last_bucket=row["last_bucket"],
        max_gap_seconds=(
            float(row["max_gap_seconds"]) if row["max_gap_seconds"] is not None else None
        ),
        complete_bars=int(row["complete_bars"]),
        buy_15m=float(row["buy_15m"]) if row["buy_15m"] is not None else None,
        sell_15m=float(row["sell_15m"]) if row["sell_15m"] is not None else None,
        buy_prior=float(row["buy_prior"]) if row["buy_prior"] is not None else None,
        sell_prior=float(row["sell_prior"]) if row["sell_prior"] is not None else None,
        buy_burst_15m=(float(row["buy_burst_15m"]) if row["buy_burst_15m"] is not None else None),
        sell_burst_15m=(
            float(row["sell_burst_15m"]) if row["sell_burst_15m"] is not None else None
        ),
        oi_value_latest=(
            float(row["oi_value_latest"]) if row["oi_value_latest"] is not None else None
        ),
    )


class EarlyMomentumUnusedFlowFeaturesRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, db_url: str) -> EarlyMomentumUnusedFlowFeaturesRepository:
        return cls(
            create_async_engine(
                async_database_url(db_url),
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
            )
        )

    async def fetch(
        self, *, cohort_start: datetime, cohort_end: datetime
    ) -> tuple[datetime, tuple[RawFeatureRow, ...]]:
        async with self._engine.connect() as raw_connection:
            connection: AsyncConnection = await raw_connection.execution_options(
                isolation_level="REPEATABLE READ",
                postgresql_readonly=True,
            )
            async with connection.begin():
                db_now = (await connection.execute(select(func.now()))).scalar_one()
                rows = (
                    (
                        await connection.execute(
                            FEATURE_ROWS_SQL,
                            {
                                "strategy_name": STRATEGY_NAME,
                                "strategy_version": STRATEGY_VERSION,
                                "contract_sha256_hex": EXPECTED_CONTRACT_SHA256_HEX,
                                "cohort_start": cohort_start,
                                "cohort_end": cohort_end,
                                "bars_start": cohort_start - timedelta(minutes=120),
                                "accounting_version": ACCOUNTING_VERSION,
                                "setup_context_strategy": EXPECTED_SETUP_CONTEXT_STRATEGY,
                            },
                        )
                    )
                    .mappings()
                    .all()
                )
        return db_now, tuple(_row(row) for row in rows)

    async def close(self) -> None:
        await self._engine.dispose()


__all__ = ["FEATURE_ROWS_SQL", "EarlyMomentumUnusedFlowFeaturesRepository"]
