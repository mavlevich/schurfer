# ruff: noqa
import asyncio
import os
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text


async def run():
    url = os.getenv("DATABASE_URL")
    if not url:
        return print("Error: DATABASE_URL is not set.")

    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            print("Verifying database records...")

            res = await conn.execute(
                text("SELECT COUNT(*) FROM timeseries.bybit_momentum_bars_1m;")
            )
            bars_count = res.scalar()
            print(f"Total Bybit 1m bars: {bars_count}")

            if bars_count > 0:
                res = await conn.execute(
                    text(
                        "SELECT symbol, bucket_start FROM timeseries.bybit_momentum_bars_1m ORDER BY bucket_start DESC LIMIT 3;"
                    )
                )
                print("Latest Bybit bars:", res.fetchall())

            res = await conn.execute(
                text("SELECT COUNT(*) FROM timeseries.liquidation_events WHERE exchange='bybit';")
            )
            liqs_count = res.scalar()
            print(f"Total Bybit liquidations: {liqs_count}")

            if liqs_count > 0:
                res = await conn.execute(
                    text(
                        "SELECT native_market_id, event_at FROM timeseries.liquidation_events WHERE exchange='bybit' ORDER BY event_at DESC LIMIT 3;"
                    )
                )
                print("Latest Bybit liquidations:", res.fetchall())

    finally:
        await engine.dispose()


asyncio.run(run())
