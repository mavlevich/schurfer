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
            print("🚀 Quant v8: Open Interest (OI) Data Funnel Check...")

            q = """
            SELECT
                e.id, e.base, e.first_seen_at,
                oi.exchange, oi.oi_usd, oi.recorded_at
            FROM app.pump_events e
            JOIN app.oi_snapshots oi ON oi.event_id = e.id
            WHERE e.first_seen_at >= NOW() - INTERVAL '5 days'
            ORDER BY e.id, oi.recorded_at;
            """
            print("Fetching OI snapshots linked to recent pump events...")
            res = await conn.execute(text(q))
            rows = res.fetchall()

            df = pd.DataFrame(
                rows,
                columns=["event_id", "base", "first_seen_at", "exchange", "oi_usd", "recorded_at"],
            )

            if df.empty:
                return print("❌ No OI snapshots found in the last 5 days.")

            snapshots_per_event = df.groupby("event_id").size()
            print("\n" + "=" * 80)
            print("📊 OPEN INTEREST (OI) SNAPSHOT METRICS")
            print("=" * 80)
            print(f"Total OI snapshots : {len(df)}")
            print(f"Total Pump Events  : {len(snapshots_per_event)}")
            print(f"Average per event  : {snapshots_per_event.mean():.1f}")
            print(f"Max per event      : {snapshots_per_event.max()}")

            if snapshots_per_event.max() < 2:
                print("\n⚠️ VERDICT: INSUFFICIENT DATA FOR DELTA ANALYSIS")
                print("We only record exactly 1 snapshot per event (at T0).")
                print(
                    "To test the hypothesis 'OI UP -> Fresh Positioning', we need to measure the CHANGE (delta) in OI."
                )
                print(
                    "Without continuous 1m OI timeseries (or pre-event snapshots), we cannot evaluate this strategy."
                )
            else:
                print(
                    "\n✅ We have multiple snapshots per event! We can calculate post-event OI Continuation."
                )

    finally:
        await engine.dispose()


asyncio.run(run())
