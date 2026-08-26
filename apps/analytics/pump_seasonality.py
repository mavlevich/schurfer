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
            print("Analyzing Pump Seasonality using robust statistics (Medians)...")
            q = """
            SELECT
                EXTRACT(HOUR FROM first_seen_at) as hour_utc,
                EXTRACT(DOW FROM first_seen_at) as dow,
                peak_pct,
                base
            FROM app.pump_events
            WHERE first_seen_at IS NOT NULL
            """
            res = await conn.execute(text(q))
            rows = res.fetchall()

            df = pd.DataFrame(rows, columns=["hour_utc", "dow", "peak_pct", "base"])
            if df.empty:
                return print("No pump data found.")

            days = {
                1: "Monday",
                2: "Tuesday",
                3: "Wednesday",
                4: "Thursday",
                5: "Friday",
                6: "Saturday",
                0: "Sunday",
            }
            df["day_name"] = df["dow"].map(days)

            print(f"\nTotal pumps analyzed: {len(df)}")

            print("\n=== Most Profitable Hours (UTC) ===")
            hourly = (
                df.groupby("hour_utc")
                .agg(
                    count=("peak_pct", "count"),
                    unique_assets=("base", "nunique"),
                    median_peak=("peak_pct", "median"),
                )
                .reset_index()
            )

            best_hours = (
                hourly[hourly["count"] > 10].sort_values("median_peak", ascending=False).head(5)
            )
            print("🕒 TOP-5 BEST hours:")
            for _, r in best_hours.iterrows():
                print(
                    f"  {int(r['hour_utc']):02d}:00 UTC -> Median Peak: {r['median_peak']:.1f}% (Assets: {int(r['unique_assets'])})"
                )

            worst_hours = (
                hourly[hourly["count"] > 10].sort_values("median_peak", ascending=True).head(3)
            )
            print("\n🕒 TOP-3 WORST hours (Avoid trading):")
            for _, r in worst_hours.iterrows():
                print(
                    f"  {int(r['hour_utc']):02d}:00 UTC -> Median Peak: {r['median_peak']:.1f}% (Assets: {int(r['unique_assets'])})"
                )

            print("\n=== Most Profitable Days of the Week ===")
            daily = (
                df.groupby("day_name")
                .agg(
                    count=("peak_pct", "count"),
                    unique_assets=("base", "nunique"),
                    median_peak=("peak_pct", "median"),
                )
                .reset_index()
                .sort_values("median_peak", ascending=False)
            )

            for _, r in daily.iterrows():
                print(
                    f"📅 {r['day_name']:<12} -> Median Peak: {r['median_peak']:.1f}% (Assets: {int(r['unique_assets'])})"
                )

    finally:
        await engine.dispose()


asyncio.run(run())
