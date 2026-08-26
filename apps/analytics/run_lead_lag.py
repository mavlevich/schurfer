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
            print("Executing SQL aggregation on server (last 7 days)...")
            q = """
            SELECT
                date_trunc('second', event_at) as sec,
                exchange,
                SUM(estimated_liquidation_notional) as vol_usd
            FROM timeseries.liquidation_events
            WHERE event_at >= NOW() - INTERVAL '7 days'
            GROUP BY 1, 2
            ORDER BY 1;
            """
            res = await conn.execute(text(q))
            rows = res.fetchall()

            df = pd.DataFrame(rows, columns=["sec", "exchange", "vol_usd"])
            if df.empty:
                return print("No data.")

            df["sec"] = pd.to_datetime(df["sec"])

            print(f"Fetched {len(df)} aggregated rows. Resampling to continuous 1S timeline...")

            # Pivot first
            df_pivot = df.pivot(index="sec", columns="exchange", values="vol_usd").fillna(0)

            if "binance" not in df_pivot.columns or "bybit" not in df_pivot.columns:
                return print("Missing one of the exchanges for correlation.")

            # Resample to 1 second frequency to fill gaps with 0
            df_resampled = df_pivot.resample("1s").sum().fillna(0)

            print("Calculating Time-Lagged Cross-Correlation (TLCC)...")
            lags = range(-10, 11)
            corrs = []
            for lag in lags:
                corr = df_resampled["binance"].corr(df_resampled["bybit"].shift(lag))
                corrs.append((lag, corr))

            print("\n=== Cross-Correlation (Binance vs Bybit) ===")
            for lag, corr in corrs:
                print(f"Shift {lag:3}s: {corr:.4f}")

            best_lag, best_corr = max(corrs, key=lambda x: x[1] if pd.notna(x[1]) else -1)
            print(f"\nMax correlation: {best_corr:.4f} at shift {best_lag} sec.")
            if best_lag < 0:
                print("Conclusion: Binance Lags Bybit.")
            elif best_lag > 0:
                print("Conclusion: Binance LEADS Bybit.")
            else:
                print("Conclusion: Synchronous.")
    finally:
        await engine.dispose()


asyncio.run(run())
