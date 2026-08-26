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
            print("Extracting pumps and aggregating liquidations...")
            q = """
            WITH pump_stats AS (
                SELECT
                    p.id,
                    p.base,
                    p.peak_pct,
                    COUNT(l.native_market_id) as liq_count,
                    SUM(l.estimated_liquidation_notional) as liq_vol_usd
                FROM app.pump_events p
                LEFT JOIN timeseries.liquidation_events l
                    ON (l.native_market_id = p.base || 'USDT' OR l.native_market_id = p.base || '-USDT')
                    AND l.event_at >= p.first_seen_at - INTERVAL '30 seconds'
                    AND l.event_at <= p.first_seen_at + INTERVAL '120 seconds'
                GROUP BY p.id, p.base, p.peak_pct
            )
            SELECT * FROM pump_stats;
            """
            res = await conn.execute(text(q))
            rows = res.fetchall()

            df = pd.DataFrame(rows, columns=["id", "base", "peak_pct", "liq_count", "liq_vol_usd"])
            if df.empty:
                return print("No data.")

            df["liq_vol_usd"] = df["liq_vol_usd"].fillna(0)
            df["has_liquidations"] = df["liq_vol_usd"] > 0

            pumps_with = df[df["has_liquidations"]]
            pumps_without = df[~df["has_liquidations"]]

            print(f"\nTotal pumps: {len(df)}")
            print(f"With liquidations (Fuel): {len(pumps_with)}")
            print(f"Without liquidations: {len(pumps_without)}")

            print("\n=== Peak % Comparison ===")
            if not pumps_without.empty:
                print(f"Avg Peak WITHOUT liquidations: {pumps_without['peak_pct'].mean():.2f}%")
            if not pumps_with.empty:
                print(f"Avg Peak WITH liquidations: {pumps_with['peak_pct'].mean():.2f}%")

            target = 25.0
            if not pumps_without.empty:
                win_without = (
                    len(pumps_without[pumps_without["peak_pct"] > target])
                    / len(pumps_without)
                    * 100
                )
                print(f"Chance to hit {target}% WITHOUT liquidations: {win_without:.1f}%")
            if not pumps_with.empty:
                win_with = len(pumps_with[pumps_with["peak_pct"] > target]) / len(pumps_with) * 100
                print(f"Chance to hit {target}% WITH liquidations: {win_with:.1f}%")

    finally:
        await engine.dispose()


asyncio.run(run())
