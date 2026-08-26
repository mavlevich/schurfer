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
            print("Analyzing Funding Rates (Short Squeezes)...")
            q = """
            SELECT
                p.id,
                p.base,
                p.peak_pct,
                f.rate as funding_rate,
                f.exchange as funding_exchange
            FROM app.pump_events p
            JOIN app.funding_rate_snapshots f ON f.event_id = p.id
            WHERE f.rate IS NOT NULL
            """
            res = await conn.execute(text(q))
            rows = res.fetchall()

            df = pd.DataFrame(rows, columns=["id", "base", "peak_pct", "funding_rate", "exchange"])
            if df.empty:
                return print("No funding data linked to pumps.")

            df_grouped = df.groupby(["id", "base", "peak_pct"])["funding_rate"].mean().reset_index()

            print(f"\nFound pumps with funding snapshots: {len(df_grouped)}")

            df_negative = df_grouped[df_grouped["funding_rate"] < 0]
            df_positive = df_grouped[df_grouped["funding_rate"] >= 0]

            print("\n=== Profitability by Funding Context ===")

            if not df_negative.empty:
                print(f"🔴 Negative Funding (Short Squeezes): {len(df_negative)} cases")
                print(f"   -> Avg Peak: {df_negative['peak_pct'].mean():.2f}%")

            if not df_positive.empty:
                print(f"🟢 Positive/Neutral Funding: {len(df_positive)} cases")
                print(f"   -> Avg Peak: {df_positive['peak_pct'].mean():.2f}%")

            target = 40.0
            print(f"\n=== Chance of a Mega-Pump (> {target}%) ===")
            if not df_negative.empty:
                print(
                    f"🔴 With Negative Funding: {len(df_negative[df_negative['peak_pct'] > target]) / len(df_negative) * 100:.1f}%"
                )
            if not df_positive.empty:
                print(
                    f"🟢 With Positive Funding: {len(df_positive[df_positive['peak_pct'] > target]) / len(df_positive) * 100:.1f}%"
                )

    finally:
        await engine.dispose()


asyncio.run(run())
