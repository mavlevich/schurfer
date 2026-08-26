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
            print("Analyzing Orderflow Toxicity (Buy/Sell Ratio)...")
            q = """
            WITH pump_bars AS (
                SELECT
                    p.id,
                    p.base,
                    p.peak_pct,
                    p.first_seen_at,
                    b.buy_total_notional_usd,
                    b.sell_total_notional_usd,
                    (b.buy_total_notional_usd + b.sell_total_notional_usd) as total_vol
                FROM app.pump_events p
                JOIN timeseries.bybit_momentum_bars_1m b
                    ON b.symbol = p.base || 'USDT'
                    AND b.bucket_start = date_trunc('minute', p.first_seen_at)
                WHERE b.buy_total_notional_usd IS NOT NULL
                  AND (b.buy_total_notional_usd + b.sell_total_notional_usd) > 0
            )
            SELECT * FROM pump_bars;
            """
            res = await conn.execute(text(q))
            rows = res.fetchall()

            df = pd.DataFrame(
                rows,
                columns=[
                    "id",
                    "base",
                    "peak_pct",
                    "first_seen_at",
                    "buy_vol",
                    "sell_vol",
                    "total_vol",
                ],
            )
            if df.empty:
                return print("No intersection between pumps and minute bars.")

            df["buy_ratio"] = df["buy_vol"] / df["total_vol"]

            df_fake = df[df["buy_ratio"] < 0.5]
            df_normal = df[(df["buy_ratio"] >= 0.5) & (df["buy_ratio"] < 0.7)]
            df_aggro = df[df["buy_ratio"] >= 0.7]

            print(f"\nFound pumps with initial minute bars: {len(df)}")
            print(f"Average Peak % across all: {df['peak_pct'].mean():.2f}%")

            print("\n=== Profitability by Buyer Dominance (1st minute) ===")

            if not df_fake.empty:
                print(f"🔴 Fake/Trap Pumps (Buy Ratio < 50%): {len(df_fake)} cases")
                print(f"   -> Avg Peak: {df_fake['peak_pct'].mean():.2f}%")

            if not df_normal.empty:
                print(f"🟡 Normal Pumps (Buy Ratio 50-70%): {len(df_normal)} cases")
                print(f"   -> Avg Peak: {df_normal['peak_pct'].mean():.2f}%")

            if not df_aggro.empty:
                print(f"🟢 Aggressive/Exhaustion Pumps (Buy Ratio > 70%): {len(df_aggro)} cases")
                print(f"   -> Avg Peak: {df_aggro['peak_pct'].mean():.2f}%")

            target_pct = 30.0
            print(f"\n=== Chance to hit > {target_pct}% ===")
            if not df_fake.empty:
                print(
                    f"🔴 Fake/Trap Pumps: {len(df_fake[df_fake['peak_pct'] > target_pct]) / len(df_fake) * 100:.1f}%"
                )
            if not df_aggro.empty:
                print(
                    f"🟢 Aggressive Pumps: {len(df_aggro[df_aggro['peak_pct'] > target_pct]) / len(df_aggro) * 100:.1f}%"
                )

    finally:
        await engine.dispose()


asyncio.run(run())
