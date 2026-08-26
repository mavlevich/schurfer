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
            print("Extracting independent pump detections from event sources...")
            q = """
            SELECT
                event_id,
                MIN(first_seen_at) FILTER (WHERE exchange = 'binance') as binance_ts,
                MIN(first_seen_at) FILTER (WHERE exchange = 'bybit') as bybit_ts
            FROM app.pump_event_sources
            GROUP BY event_id
            HAVING MIN(first_seen_at) FILTER (WHERE exchange = 'binance') IS NOT NULL
               AND MIN(first_seen_at) FILTER (WHERE exchange = 'bybit') IS NOT NULL;
            """
            res = await conn.execute(text(q))
            rows = res.fetchall()

            data = []
            for r in rows:
                event_id, binance_ts, bybit_ts = r
                lag_ms = (bybit_ts - binance_ts).total_seconds() * 1000
                data.append(
                    {
                        "event_id": event_id,
                        "binance_ts": binance_ts,
                        "bybit_ts": bybit_ts,
                        "lag_ms": lag_ms,
                    }
                )

            df = pd.DataFrame(data)
            if df.empty:
                return print("No joint Binance + Bybit pumps found in sources.")

            print(f"Found {len(df)} joint pumps.")

            df_lead_binance = df[df["lag_ms"] > 0]
            df_lead_bybit = df[df["lag_ms"] < 0]

            print("\n=== Scanner Lead-Lag Statistics (Binance vs Bybit) ===")
            print(
                f"Binance detected first in {len(df_lead_binance)} cases ({len(df_lead_binance)/len(df)*100:.1f}%)"
            )
            print(
                f"Bybit detected first in {len(df_lead_bybit)} cases ({len(df_lead_bybit)/len(df)*100:.1f}%)"
            )

            if not df_lead_binance.empty:
                print(f"Average Binance lead: {df_lead_binance['lag_ms'].mean():.0f} ms")
                print(f"Median Binance lead: {df_lead_binance['lag_ms'].median():.0f} ms")
            if not df_lead_bybit.empty:
                print(f"Average Bybit lead: {abs(df_lead_bybit['lag_ms'].mean()):.0f} ms")
                print(f"Median Bybit lead: {abs(df_lead_bybit['lag_ms'].median()):.0f} ms")

    finally:
        await engine.dispose()


asyncio.run(run())
