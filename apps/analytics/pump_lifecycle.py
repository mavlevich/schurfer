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
            print("Analyzing Pump Time-to-Peak lifecycle...")
            q = """
            SELECT
                id,
                base,
                peak_pct,
                retrace_pct,
                first_seen_at,
                last_seen_at,
                closed_at,
                EXTRACT(EPOCH FROM (last_seen_at - first_seen_at)) / 60 as minutes_to_peak
            FROM app.pump_events
            WHERE last_seen_at IS NOT NULL AND first_seen_at IS NOT NULL
              AND EXTRACT(EPOCH FROM (last_seen_at - first_seen_at)) > 0
            """
            res = await conn.execute(text(q))
            rows = res.fetchall()

            df = pd.DataFrame(
                rows,
                columns=[
                    "id",
                    "base",
                    "peak_pct",
                    "retrace_pct",
                    "first_seen",
                    "last_seen",
                    "closed",
                    "mins_to_peak",
                ],
            )
            if df.empty:
                return print("No pump data found.")

            print(f"\nTotal pumps analyzed: {len(df)}")

            print("\n=== How fast does a pump reach its absolute peak? ===")
            print(f"Median time: {df['mins_to_peak'].median():.1f} minutes")
            print(f"Average time: {df['mins_to_peak'].mean():.1f} minutes")

            df_fast = df[df["mins_to_peak"] <= 5]
            df_med = df[(df["mins_to_peak"] > 5) & (df["mins_to_peak"] <= 30)]
            df_slow = df[df["mins_to_peak"] > 30]

            print("\n=== Profitability by Pump Speed ===")
            print(
                f"🚀 Flashes (<= 5 min): {len(df_fast)} cases | Avg Peak: {df_fast['peak_pct'].mean():.1f}%"
            )
            print(
                f"🚶‍♂️ Normal (5 - 30 min): {len(df_med)} cases | Avg Peak: {df_med['peak_pct'].mean():.1f}%"
            )
            print(
                f"🐢 Slow/Sustained (> 30 min): {len(df_slow)} cases | Avg Peak: {df_slow['peak_pct'].mean():.1f}%"
            )

            print("\n=== Growth vs Retrace Relationship ===")
            print("Do massive pumps crash harder than small ones?")
            df_small = df[df["peak_pct"] < 20]
            df_huge = df[df["peak_pct"] >= 40]
            if not df_small.empty:
                print(f"Small pumps (<20%) -> Avg Retrace: {df_small['retrace_pct'].mean():.1f}%")
            if not df_huge.empty:
                print(f"Massive pumps (>40%) -> Avg Retrace: {df_huge['retrace_pct'].mean():.1f}%")

    finally:
        await engine.dispose()


asyncio.run(run())
