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
            print("Extracting pumps...")
            q = "SELECT id, base, peak_pct, exchanges FROM app.pump_events WHERE exchanges IS NOT NULL;"
            res = await conn.execute(text(q))
            rows = res.fetchall()

            data = []
            for r in rows:
                exchanges = r[3]
                if not isinstance(exchanges, list):
                    continue

                binance_ts = None
                bybit_ts = None

                for ex in exchanges:
                    name = ex.get("exchange")
                    ts = ex.get("ticker_timestamp_ms") or ex.get("observed_at_ms")
                    if not ts:
                        continue

                    if name == "binance":
                        binance_ts = ts
                    elif name == "bybit":
                        bybit_ts = ts

                if binance_ts and bybit_ts:
                    data.append(
                        {
                            "base": r[1],
                            "peak_pct": r[2],
                            "binance_ts": binance_ts,
                            "bybit_ts": bybit_ts,
                            "lag_ms": bybit_ts - binance_ts,
                        }
                    )

            df = pd.DataFrame(data)
            if df.empty:
                return print("No joint Binance + Bybit pumps found.")

            print(f"Found {len(df)} joint pumps.")

            df_lead_binance = df[df["lag_ms"] > 0]
            df_lead_bybit = df[df["lag_ms"] < 0]

            print("\n=== Lead-Lag Statistics (Binance vs Bybit) ===")
            print(
                f"Binance led Bybit in {len(df_lead_binance)} cases ({len(df_lead_binance)/len(df)*100:.1f}%)"
            )
            print(
                f"Bybit led Binance in {len(df_lead_bybit)} cases ({len(df_lead_bybit)/len(df)*100:.1f}%)"
            )

            if not df_lead_binance.empty:
                print(f"Average Binance lead: {df_lead_binance['lag_ms'].mean():.0f} ms")
            if not df_lead_bybit.empty:
                print(f"Average Bybit lead: {abs(df_lead_bybit['lag_ms'].mean()):.0f} ms")

    finally:
        await engine.dispose()


asyncio.run(run())
