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
            print("Analyzing mean reversion after major liquidation cascades...")
            q = """
            WITH cascade_minutes AS (
                SELECT
                    date_trunc('minute', event_at) as bucket,
                    exchange,
                    native_market_id as symbol,
                    position_side,
                    SUM(estimated_liquidation_notional) as cascade_vol
                FROM timeseries.liquidation_events
                WHERE position_side IN ('LONG', 'SHORT') AND exchange = 'bybit'
                GROUP BY 1, 2, 3, 4
                HAVING SUM(estimated_liquidation_notional) > 1000
            ),
            top_cascades AS (
                SELECT * FROM cascade_minutes
                ORDER BY cascade_vol DESC
                LIMIT 200
            )
            SELECT
                c.bucket, c.symbol, c.position_side, c.cascade_vol,
                b0.close_price as price_T0,
                b1.close_price as price_T1,
                b3.close_price as price_T3,
                b5.close_price as price_T5
            FROM top_cascades c
            JOIN timeseries.bybit_momentum_bars_1m b0
                ON b0.symbol = c.symbol AND b0.bucket_start = c.bucket
            LEFT JOIN timeseries.bybit_momentum_bars_1m b1
                ON b1.symbol = c.symbol AND b1.bucket_start = c.bucket + INTERVAL '1 minute'
            LEFT JOIN timeseries.bybit_momentum_bars_1m b3
                ON b3.symbol = c.symbol AND b3.bucket_start = c.bucket + INTERVAL '3 minutes'
            LEFT JOIN timeseries.bybit_momentum_bars_1m b5
                ON b5.symbol = c.symbol AND b5.bucket_start = c.bucket + INTERVAL '5 minutes';
            """
            res = await conn.execute(text(q))
            rows = res.fetchall()

            df = pd.DataFrame(
                rows, columns=["bucket", "symbol", "side", "vol", "T0", "T1", "T3", "T5"]
            )
            if df.empty:
                return print("No data. Cascades > $1000 likely not present yet.")

            df["ret_1m"] = (df["T1"] - df["T0"]) / df["T0"] * 100
            df["ret_3m"] = (df["T3"] - df["T0"]) / df["T0"] * 100
            df["ret_5m"] = (df["T5"] - df["T0"]) / df["T0"] * 100

            df_shorts = df[df["side"] == "SHORT"]
            df_longs = df[df["side"] == "LONG"]

            print(f"\nFound large cascades on Bybit (>$1000): {len(df)}")

            if not df_shorts.empty:
                print("\n=== SHORT Liquidations (Buys) -> Expecting price retrace DOWN (-%) ===")
                print(f"Avg change after 1m: {df_shorts['ret_1m'].mean():.3f}%")
                print(f"Avg change after 3m: {df_shorts['ret_3m'].mean():.3f}%")
                print(f"Avg change after 5m: {df_shorts['ret_5m'].mean():.3f}%")

            if not df_longs.empty:
                print("\n=== LONG Liquidations (Sells) -> Expecting price retrace UP (+%) ===")
                print(f"Avg change after 1m: {df_longs['ret_1m'].mean():.3f}%")
                print(f"Avg change after 3m: {df_longs['ret_3m'].mean():.3f}%")
                print(f"Avg change after 5m: {df_longs['ret_5m'].mean():.3f}%")

    finally:
        await engine.dispose()


asyncio.run(run())
