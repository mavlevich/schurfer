# ruff: noqa
import asyncio
import os
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
import pandas as pd


async def run():
    url = os.getenv("DATABASE_URL")
    if not url:
        return print("Error: DATABASE_URL is not set.")

    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            res = await conn.execute(
                text("SELECT MIN(event_at) FROM timeseries.liquidation_events")
            )
            start_date = res.scalar()
            if not start_date:
                return print("No liquidations found.")

            print(
                "Extracting pumps strictly after capture started, filtering for strict market match..."
            )
            q = f"""
            WITH valid_pumps AS (
                SELECT id, base, peak_pct, first_seen_at
                FROM app.pump_events
                WHERE first_seen_at >= '{start_date}'
            ),
            pump_stats AS (
                SELECT
                    p.id, p.base, p.peak_pct,
                    SUM(l.estimated_liquidation_notional) FILTER (WHERE l.position_side = 'short') as short_liq_vol_usd,
                    SUM(l.estimated_liquidation_notional) FILTER (WHERE l.position_side = 'long') as long_liq_vol_usd
                FROM valid_pumps p
                LEFT JOIN timeseries.liquidation_events l
                    ON (l.native_market_id = p.base || 'USDT' OR l.native_market_id = p.base || '-USDT')
                    AND l.market_type = 'linear'
                    AND l.event_at >= p.first_seen_at - INTERVAL '30 seconds'
                    AND l.event_at <= p.first_seen_at + INTERVAL '60 seconds'
                GROUP BY p.id, p.base, p.peak_pct
            )
            SELECT * FROM pump_stats;
            """
            res = await conn.execute(text(q))
            rows = res.fetchall()

            df = pd.DataFrame(
                rows, columns=["id", "base", "peak_pct", "short_liq_vol_usd", "long_liq_vol_usd"]
            )
            if df.empty:
                return print("No data.")

            df["short_liq_vol_usd"] = df["short_liq_vol_usd"].fillna(0)
            df["long_liq_vol_usd"] = df["long_liq_vol_usd"].fillna(0)

            pumps_with_shorts = df[df["short_liq_vol_usd"] > 1000]  # Min $1000 filter
            pumps_without = df[df["short_liq_vol_usd"] <= 1000]

            print(f"\nTotal valid pumps in period: {len(df)}")
            print(f"With significant Short Liquidations (Fuel): {len(pumps_with_shorts)}")
            print(f"Without Short Liquidations: {len(pumps_without)}")

            print("\n=== Peak % Comparison ===")
            if not pumps_without.empty:
                print(f"Median Peak WITHOUT shorts: {pumps_without['peak_pct'].median():.2f}%")
            if not pumps_with_shorts.empty:
                print(f"Median Peak WITH shorts: {pumps_with_shorts['peak_pct'].median():.2f}%")

    finally:
        await engine.dispose()


asyncio.run(run())
