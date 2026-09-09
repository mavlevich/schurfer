"""Repeatable-read, read-only repository for the HYP-024 order-flow report.

Everything runs inside a single REPEATABLE READ, read-only transaction so the
whole read -- the `SELECT now()` snapshot timestamp, every cohort decision,
its point-in-time identity resolution, and its aggregated pre-decision taker
imbalance -- comes from one consistent Postgres snapshot. The snapshot
timestamp is the transaction's FIRST statement (the database's own clock, not
the report process's), matching every other formal report in this package.

SQL-SIDE AGGREGATION ONLY. The prod host is 4 GB and OOM-kills a report that
pulls raw bars into Python. The ten/five/twenty-minute taker imbalance is
summed per decision in SQL; this query returns exactly one row per cohort
decision (thousands of rows), never the underlying bars (millions). No
server-side cursor is needed because the result set is already one row per
decision.

Identity resolution (the hard part). pump_short decisions carry `base` +
`exchange`, never the momentum-bar native symbol, so a naive
`symbol = base || 'USDT'` is forbidden. Each decision is resolved point in
time through the SAME momentum-universe identity tables the liquidation-cascade
and cluster machinery use (`app.momentum_universe_snapshots` /
`app.momentum_universe_instruments`): the single most recent snapshot at or
before the decision's `ts` for that exchange, then the ready instrument row(s)
for that `(exchange, base)`. It FAILS CLOSED -- `match_count` is the number of
distinct native market ids the base resolved to, and only `match_count = 1`
resolves; 0 (no snapshot / not listed) and >1 (ambiguous) are both reported as
coverage loss, never a native symbol guess and never a negative outcome.

The bar join uses the bars table's REAL primary key -- `(exchange,
market_type, symbol, capture_version, bucket_start)` (see migration
0024_bybit_momentum_bars_1m.py; universe_version is deliberately NOT part of
that key, so joining on it would drop legitimate bars whose stored
universe_version differs from the identity snapshot's). `capture_version` is
pinned (see MOMENTUM_CAPTURE_VERSION): if a venue were ever captured under a
different contract, its bars simply fall out as visible per-exchange coverage
loss rather than silently interleaving two contracts.

Read-only, single statistic, no writes. The measure is the registered
ten-minute taker imbalance; the five- and twenty-minute sums are carried only
for context and never substitute for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .momentum_flow_capture_contract import BYBIT_MOMENTUM_CAPTURE_VERSION
from .orderflow_microstructure import (
    HELD_OUT_START,
    HORIZON_MINUTES,
    RESOLVER_VERSION,
    STRATEGY_VERSION,
    ResolvedDecisionRow,
)
from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.engine import RowMapping
    from sqlalchemy.ext.asyncio import AsyncConnection


class HeldOutWindowError(ValueError):
    """Raised when a caller asks this discovery pass to read at or past the
    frozen held-out boundary (2026-08-25). The held-out window must not be
    read here; a candidate earns that read only through a later, separately
    registered pass."""


# Both currently-captured venues (bybit, binance) are written by the same
# collector momentumcapture writer under this one capture-contract version.
# The constant is named for bybit in the shared capture-contract module, but
# it is the writer's contract version, not a venue tag. Pinned so a future
# contract bump cannot silently interleave two field sets in one aggregate
# (see the liquidation-cascade repository's own capture_version note); a venue
# on a different contract would show up as per-exchange coverage loss, which
# is honest, never a wrong number.
MOMENTUM_CAPTURE_VERSION = BYBIT_MOMENTUM_CAPTURE_VERSION


# One row per cohort decision. The per-bar taker imbalance is
# (sell - buy) / (sell + buy), summed over the complete (trades_complete) bars
# strictly before the decision minute; a zero-taker-notional bar contributes 0
# rather than dividing by zero. The pre-decision windows are anchored on
# date_trunc('minute', ts) so only bars fully BEFORE the decision are read
# (point in time). Identity is a LEFT JOIN LATERAL against the single
# most-recent snapshot at or before ts; the bar aggregate LATERAL runs only
# when identity resolved to exactly one native market.
_RESOLVED_DECISIONS_SQL = text("""
WITH cohort AS (
    SELECT
        d.decision_id::text AS decision_id,
        d.ts,
        d.base,
        d.exchange,
        o.short_return_pct::double precision AS short_return_pct,
        o.mfe_pct::double precision AS mfe_pct,
        o.mae_pct::double precision AS mae_pct
    FROM app.trade_decisions AS d
    JOIN app.trade_decision_outcomes AS o
      ON o.decision_id = d.decision_id
     AND o.horizon_minutes = :horizon_minutes
     AND o.resolver_version = :resolver_version
    WHERE d.strategy_version = :strategy_version
      AND d.decision_id IS NOT NULL
      AND d.ts >= :cohort_start
      AND d.ts < :cohort_end
      AND o.short_return_pct IS NOT NULL
),
resolved AS (
    SELECT
        c.*,
        ident.match_count,
        ident.native_market_id,
        ident.market_type
    FROM cohort AS c
    LEFT JOIN LATERAL (
        SELECT
            count(DISTINCT i.native_market_id) AS match_count,
            min(i.native_market_id) AS native_market_id,
            min(i.canonical_market_type) AS market_type
        FROM app.momentum_universe_instruments AS i
        JOIN LATERAL (
            SELECT s.universe_version, s.catalog_version
            FROM app.momentum_universe_snapshots AS s
            WHERE s.exchange = c.exchange
              AND s.captured_at <= c.ts
            ORDER BY s.captured_at DESC, s.created_at DESC
            LIMIT 1
        ) AS snap
          ON snap.universe_version = i.universe_version
         AND snap.catalog_version = i.catalog_version
        WHERE i.exchange = c.exchange
          AND i.base = c.base
          AND i.identity_status = 'ready'
    ) AS ident ON TRUE
)
SELECT
    r.decision_id,
    r.ts,
    r.base,
    r.exchange,
    r.short_return_pct,
    r.mfe_pct,
    r.mae_pct,
    COALESCE(r.match_count, 0)::integer AS match_count,
    r.native_market_id,
    r.market_type,
    COALESCE(bars.bars_10m, 0)::integer AS bars_10m,
    COALESCE(bars.bars_5m, 0)::integer AS bars_5m,
    COALESCE(bars.bars_20m, 0)::integer AS bars_20m,
    bars.imbalance_10m,
    bars.imbalance_5m,
    bars.imbalance_20m
FROM resolved AS r
LEFT JOIN LATERAL (
    SELECT
        count(*) FILTER (
            WHERE b.bucket_start >= date_trunc('minute', r.ts) - interval '10 minutes'
        )::integer AS bars_10m,
        count(*) FILTER (
            WHERE b.bucket_start >= date_trunc('minute', r.ts) - interval '5 minutes'
        )::integer AS bars_5m,
        count(*)::integer AS bars_20m,
        sum(b.imbalance) FILTER (
            WHERE b.bucket_start >= date_trunc('minute', r.ts) - interval '10 minutes'
        )::double precision AS imbalance_10m,
        sum(b.imbalance) FILTER (
            WHERE b.bucket_start >= date_trunc('minute', r.ts) - interval '5 minutes'
        )::double precision AS imbalance_5m,
        sum(b.imbalance)::double precision AS imbalance_20m
    FROM (
        SELECT
            bar.bucket_start,
            CASE
                WHEN (bar.buy_total_notional_usd + bar.sell_total_notional_usd) = 0 THEN 0.0
                ELSE (bar.sell_total_notional_usd - bar.buy_total_notional_usd)
                     / (bar.buy_total_notional_usd + bar.sell_total_notional_usd)
            END AS imbalance
        FROM timeseries.bybit_momentum_bars_1m AS bar
        WHERE bar.exchange = r.exchange
          AND bar.market_type = r.market_type
          AND bar.symbol = r.native_market_id
          AND bar.capture_version = :capture_version
          AND bar.trades_complete
          AND bar.bucket_start >= date_trunc('minute', r.ts) - interval '20 minutes'
          AND bar.bucket_start < date_trunc('minute', r.ts)
    ) AS b
) AS bars ON r.match_count = 1 AND r.native_market_id IS NOT NULL
ORDER BY r.exchange, r.ts, r.decision_id
""")


def _row(row: RowMapping) -> ResolvedDecisionRow:
    return ResolvedDecisionRow(
        decision_id=str(row["decision_id"]),
        base=str(row["base"]),
        exchange=str(row["exchange"]),
        ts=row["ts"],
        short_return_pct=float(row["short_return_pct"]),
        mfe_pct=float(row["mfe_pct"]) if row["mfe_pct"] is not None else None,
        mae_pct=float(row["mae_pct"]) if row["mae_pct"] is not None else None,
        match_count=int(row["match_count"]),
        native_market_id=(
            str(row["native_market_id"]) if row["native_market_id"] is not None else None
        ),
        market_type=str(row["market_type"]) if row["market_type"] is not None else None,
        bars_10m=int(row["bars_10m"]),
        bars_5m=int(row["bars_5m"]),
        bars_20m=int(row["bars_20m"]),
        imbalance_10m=float(row["imbalance_10m"]) if row["imbalance_10m"] is not None else None,
        imbalance_5m=float(row["imbalance_5m"]) if row["imbalance_5m"] is not None else None,
        imbalance_20m=float(row["imbalance_20m"]) if row["imbalance_20m"] is not None else None,
    )


class OrderflowMicrostructureRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, db_url: str) -> OrderflowMicrostructureRepository:
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
    ) -> tuple[datetime, tuple[ResolvedDecisionRow, ...]]:
        """Fetch the resolved-decision rows for one discovery window. Refuses
        to read at or past the frozen held-out boundary -- this defensive
        guard is redundant with the CLI's own check, but the repository is the
        thing that actually touches held-out rows, so it fails closed here
        too."""
        if cohort_end > HELD_OUT_START:
            raise HeldOutWindowError(
                f"cohort_end={cohort_end.isoformat()} is past the held-out boundary "
                f"{HELD_OUT_START.isoformat()}; this discovery pass may not read the "
                "held-out window."
            )
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
                            _RESOLVED_DECISIONS_SQL,
                            {
                                "horizon_minutes": HORIZON_MINUTES,
                                "resolver_version": RESOLVER_VERSION,
                                "strategy_version": STRATEGY_VERSION,
                                "cohort_start": cohort_start,
                                "cohort_end": cohort_end,
                                "capture_version": MOMENTUM_CAPTURE_VERSION,
                            },
                        )
                    )
                    .mappings()
                    .all()
                )
        return db_now, tuple(_row(row) for row in rows)

    async def close(self) -> None:
        await self._engine.dispose()


__all__ = [
    "MOMENTUM_CAPTURE_VERSION",
    "_RESOLVED_DECISIONS_SQL",
    "HeldOutWindowError",
    "OrderflowMicrostructureRepository",
]
