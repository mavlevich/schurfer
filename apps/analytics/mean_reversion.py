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
            print(
                "Analyzing mean reversion without join multiplication and adding trading costs..."
            )
            # Filter linear only, Bybit only, use strict grouping
            q = """
            WITH cascade_minutes AS (
                SELECT
                    date_trunc('minute', event_at) as bucket,
                    native_market_id as symbol,
                    position_side,
                    SUM(estimated_liquidation_notional) as cascade_vol
                FROM timeseries.liquidation_events
                WHERE position_side IN ('long', 'short')
                  AND exchange = 'bybit'
                  AND market_type = 'linear'
                GROUP BY 1, 2, 3
                HAVING SUM(estimated_liquidation_notional) > 1000
            ),
            top_cascades AS (
                SELECT * FROM cascade_minutes
                ORDER BY cascade_vol DESC
                LIMIT 200
            )
            SELECT
                c.bucket, c.symbol, c.position_side, c.cascade_vol,
                b1.open_price as entry_T1,
                b2.close_price as close_T2,
                b4.close_price as close_T4,
                b6.close_price as close_T6
            FROM top_cascades c
            JOIN timeseries.bybit_momentum_bars_1m b1
                ON b1.symbol = c.symbol AND b1.market_type = 'linear' AND b1.bucket_start = c.bucket + INTERVAL '1 minute'
            LEFT JOIN timeseries.bybit_momentum_bars_1m b2
                ON b2.symbol = c.symbol AND b2.market_type = 'linear' AND b2.bucket_start = c.bucket + INTERVAL '2 minutes'
            LEFT JOIN timeseries.bybit_momentum_bars_1m b4
                ON b4.symbol = c.symbol AND b4.market_type = 'linear' AND b4.bucket_start = c.bucket + INTERVAL '4 minutes'
            LEFT JOIN timeseries.bybit_momentum_bars_1m b6
                ON b6.symbol = c.symbol AND b6.market_type = 'linear' AND b6.bucket_start = c.bucket + INTERVAL '6 minutes';
            """
            res = await conn.execute(text(q))
            rows = res.fetchall()

            df = pd.DataFrame(
                rows,
                columns=[
                    "bucket",
                    "symbol",
                    "side",
                    "vol",
                    "entry_T1",
                    "close_T2",
                    "close_T4",
                    "close_T6",
                ],
            )
            if df.empty:
                return print("No data.")

            df = df.drop_duplicates(subset=["bucket", "symbol", "side"])

            # Add trading costs: ~10 bps total for taker-in taker-out + slippage
            TAKER_FEE_PCT = 0.10

            df["ret_1m"] = (
                (df["close_T2"] - df["entry_T1"]) / df["entry_T1"] * 100
            ) - TAKER_FEE_PCT
            df["ret_3m"] = (
                (df["close_T4"] - df["entry_T1"]) / df["entry_T1"] * 100
            ) - TAKER_FEE_PCT
            df["ret_5m"] = (
                (df["close_T6"] - df["entry_T1"]) / df["entry_T1"] * 100
            ) - TAKER_FEE_PCT

            # For SHORT cascades (Sells), we go LONG, expecting positive return
            # For LONG cascades (Buys), we go SHORT, expecting negative return -> we invert it to positive PnL
            df.loc[df["side"] == "long", "ret_1m"] *= -1
            df.loc[df["side"] == "long", "ret_3m"] *= -1
            df.loc[df["side"] == "long", "ret_5m"] *= -1

            df_shorts = df[df["side"] == "short"]
            df_longs = df[df["side"] == "long"]

            print(f"\nFound large unique cascades on Bybit (>$1000): {len(df)}")

            if not df_shorts.empty:
                print("\n=== SHORT Liquidations -> Reversion LONG Trade ===")
                print(
                    f"Net Executable Median Return (after {TAKER_FEE_PCT}% fees) 1m: {df_shorts['ret_1m'].median():.3f}%"
                )
                print(
                    f"Net Executable Median Return (after {TAKER_FEE_PCT}% fees) 3m: {df_shorts['ret_3m'].median():.3f}%"
                )

            if not df_longs.empty:
                print("\n=== LONG Liquidations -> Reversion SHORT Trade ===")
                print(
                    f"Net Executable Median Return (after {TAKER_FEE_PCT}% fees) 1m: {df_longs['ret_1m'].median():.3f}%"
                )
                print(
                    f"Net Executable Median Return (after {TAKER_FEE_PCT}% fees) 3m: {df_longs['ret_3m'].median():.3f}%"
                )

    finally:
        await engine.dispose()


asyncio.run(run())
