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
            print("Analyzing Pump Duration (First Seen to Close)...")
            q = """
            SELECT
                id,
                base,
                peak_pct,
                retrace_pct,
                first_seen_at,
                closed_at,
                EXTRACT(EPOCH FROM (closed_at - first_seen_at)) / 60 as minutes_duration
            FROM app.pump_events
            WHERE closed_at IS NOT NULL AND first_seen_at IS NOT NULL
              AND EXTRACT(EPOCH FROM (closed_at - first_seen_at)) > 0
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
                    "closed",
                    "mins_duration",
                ],
            )
            if df.empty:
                return print("No closed pump data found.")

            print(f"\nTotal closed pumps analyzed: {len(df)}")

            print("\n=== Pump Episode Duration (Not Peak Time, but Total Life) ===")
            print(f"Median duration: {df['mins_duration'].median():.1f} minutes")

            df_fast = df[df["mins_duration"] <= 5]
            df_med = df[(df["mins_duration"] > 5) & (df["mins_duration"] <= 30)]
            df_slow = df[df["mins_duration"] > 30]

            print("\n=== Profitability vs Duration ===")
            print(
                f"🚀 Short-lived (<= 5 min): {len(df_fast)} cases | Median Peak: {df_fast['peak_pct'].median():.1f}%"
            )
            print(
                f"🚶‍♂️ Normal (5 - 30 min): {len(df_med)} cases | Median Peak: {df_med['peak_pct'].median():.1f}%"
            )
            print(
                f"🐢 Sustained (> 30 min): {len(df_slow)} cases | Median Peak: {df_slow['peak_pct'].median():.1f}%"
            )

    finally:
        await engine.dispose()


asyncio.run(run())
