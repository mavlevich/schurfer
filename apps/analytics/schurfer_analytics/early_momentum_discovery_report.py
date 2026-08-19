#!/usr/bin/env python3
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("early-momentum-discovery")

# We use Postgres Window Functions to do the heavy lifting out-of-core.
# This prevents OOM on the VPS by letting the DB engine handle the 10.5M+ rows.
SQL_QUERY = """
WITH base AS (
    SELECT
        symbol,
        bucket_start,
        close_price,
        open_interest,
        buy_total_notional_usd,
        sell_total_notional_usd
    FROM timeseries.bybit_momentum_bars_1m
    WHERE open_interest IS NOT NULL
),
rolling AS (
    SELECT
        symbol,
        bucket_start,
        close_price,
        open_interest,
        buy_total_notional_usd,
        sell_total_notional_usd,
        -- 2-hour lookback window (120 minutes)
        FIRST_VALUE(open_interest) OVER w_2h AS oi_start_2h,
        MAX(close_price) OVER w_2h AS price_max_2h,
        MIN(close_price) OVER w_2h AS price_min_2h,
        SUM(buy_total_notional_usd) OVER w_2h AS buy_vol_2h,
        SUM(sell_total_notional_usd) OVER w_2h AS sell_vol_2h
    FROM base
    WINDOW w_2h AS (
        PARTITION BY symbol
        ORDER BY bucket_start
        ROWS BETWEEN 120 PRECEDING AND CURRENT ROW
    )
),
candidates AS (
    SELECT
        *,
        (open_interest - oi_start_2h) / NULLIF(oi_start_2h, 0) AS oi_growth,
        (buy_vol_2h - sell_vol_2h) AS net_taker_flow,
        (price_max_2h - price_min_2h) / NULLIF(price_min_2h, 0) AS price_range_pct
    FROM rolling
    WHERE
        -- Accumulation logic:
        -- 1. OI grew by at least 5% over 2h
        -- 2. Net taker flow is positive (more aggressive buying)
        -- 3. Price is contained (max deviation < 3% over 2h)
        (open_interest - oi_start_2h) / NULLIF(oi_start_2h, 0) > 0.05
        AND (buy_vol_2h - sell_vol_2h) > 0
        AND (price_max_2h - price_min_2h) / NULLIF(price_min_2h, 0) < 0.03
)
-- Get the breakouts: where price suddenly breaks the 2h maximum
-- within 10 minutes AFTER the accumulation state.
-- (For this initial pass, we just return the accumulation candidates
-- to python to simulate the forward breakout/economics in memory).
SELECT * FROM candidates
ORDER BY bucket_start;
"""


async def main() -> None:
    db_url = os.environ.get(
        "DATABASE_URL", "postgresql://schurfer:schurfer@localhost:5432/schurfer"
    )
    logger.info("Connecting to DB to compute momentum accumulation states...")

    candidates: list[dict[str, Any]] = []

    try:
        async with (
            await psycopg.AsyncConnection.connect(db_url) as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            logger.info("Executing heavy rolling window query on 1m bars...")
            await cur.execute(SQL_QUERY)
            candidates = await cur.fetchall()
    except Exception as e:
        logger.error(f"Database query failed: {e}")
        return

    logger.info(f"Found {len(candidates)} accumulation state candidates.")

    if not candidates:
        logger.info("No candidates found. Exiting.")
        return

    # In-memory evaluation of opportunities, lead time, etc.
    opportunities_by_day: dict[str, int] = {}

    for row in candidates:
        day = row["bucket_start"].date().isoformat()
        opportunities_by_day[day] = opportunities_by_day.get(day, 0) + 1
        # Mocking forward returns simulation for the scaffolding step
        # A full simulation would fetch forward bars or join them in SQL.

    report = {
        "total_accumulation_candidates": len(candidates),
        "opportunities_by_day": opportunities_by_day,
        "discovery_parameters": {
            "window": "2h",
            "oi_growth_threshold": 0.05,
            "price_containment_threshold": 0.03,
            "requires_positive_taker_flow": True,
        },
    }

    out_dir = Path("backups/reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "early-momentum-discovery-v1.json"

    with out_file.open("w") as f:
        json.dump(report, f, indent=2, default=str)

    logger.info(f"Saved discovery report to {out_file}")


if __name__ == "__main__":
    asyncio.run(main())
