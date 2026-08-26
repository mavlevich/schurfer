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
            print("🚀 Quant Methodology v3: Building Denominator (All Eligible Minutes)...")

            # We look at the last 2 days of data to keep execution fast.
            # We filter for minutes with at least $20,000 in volume to exclude dead zones.
            q = """
            WITH eligible_minutes AS (
                SELECT
                    symbol,
                    bucket_start as t_bucket,
                    buy_total_notional_usd as buy_vol,
                    sell_total_notional_usd as sell_vol,
                    (buy_total_notional_usd + sell_total_notional_usd) as total_vol
                FROM timeseries.bybit_momentum_bars_1m
                WHERE market_type = 'linear'
                  AND bucket_start >= NOW() - INTERVAL '2 days'
                  AND bucket_start <= NOW() - INTERVAL '1 hour'
                  AND (buy_total_notional_usd + sell_total_notional_usd) > 20000
            ),
            forward_returns AS (
                SELECT
                    b.symbol,
                    b.t_bucket,
                    b.buy_vol,
                    b.sell_vol,
                    b.total_vol,
                    t1.open_price as entry_price,
                    t5.close_price as close_5m,
                    t15.close_price as close_15m
                FROM eligible_minutes b
                JOIN timeseries.bybit_momentum_bars_1m t1
                    ON t1.symbol = b.symbol AND t1.market_type = 'linear' AND t1.bucket_start = b.t_bucket + INTERVAL '1 minute'
                LEFT JOIN timeseries.bybit_momentum_bars_1m t5
                    ON t5.symbol = b.symbol AND t5.market_type = 'linear' AND t5.bucket_start = b.t_bucket + INTERVAL '5 minutes'
                LEFT JOIN timeseries.bybit_momentum_bars_1m t15
                    ON t15.symbol = b.symbol AND t15.market_type = 'linear' AND t15.bucket_start = b.t_bucket + INTERVAL '15 minutes'
            )
            SELECT * FROM forward_returns;
            """
            print("Executing SQL (this might take a few seconds)...")
            res = await conn.execute(text(q))
            rows = res.fetchall()

            cols = [
                "symbol",
                "t_bucket",
                "buy_vol",
                "sell_vol",
                "total_vol",
                "entry_price",
                "close_5m",
                "close_15m",
            ]
            df = pd.DataFrame(rows, columns=cols)

            if df.empty:
                return print("No eligible minutes found.")

            print(f"Loaded {len(df)} eligible instrument-minutes (Universe Denominator).")

            # 1. Feature Engineering (Quantile Bands for Flow)
            df["buy_imbalance"] = df["buy_vol"] / df["total_vol"]

            # Dynamically bin the imbalance into 3 bands (Moderate, High, Extreme)
            df["flow_regime"] = pd.qcut(
                df["buy_imbalance"], q=3, labels=["Moderate", "High", "Extreme"], duplicates="drop"
            )

            # 2. Executable Returns & Costs
            TAKER_FEE = 0.10  # 10 bps
            # Entry is T+1 Open. Return is calculated net of entry fees
            df["ret_5m"] = (
                (df["close_5m"] - df["entry_price"]) / df["entry_price"] * 100
            ) - TAKER_FEE
            df["ret_15m"] = (
                (df["close_15m"] - df["entry_price"]) / df["entry_price"] * 100
            ) - TAKER_FEE

            print("\n" + "=" * 80)
            print("📊 PROGRESSION MATRIX: FLOW REGIMES (Prospective Executable Returns)")
            print("=" * 80)

            # Aggregation by Flow Regime
            matrix = (
                df.groupby("flow_regime", observed=True)
                .agg(
                    opportunity_count=("symbol", "count"),
                    median_net_return_5m=("ret_5m", "median"),
                    median_net_return_15m=("ret_15m", "median"),
                    win_rate_15m=("ret_15m", lambda x: (x > 0).mean() * 100),
                )
                .reset_index()
            )

            for _, r in matrix.iterrows():
                print(
                    f"[{r['flow_regime']:<8} Flow] Opportunities: {r['opportunity_count']:<6} | "
                    f"Win Rate (15m): {r['win_rate_15m']:5.1f}% | "
                    f"Net PnL (5m): {r['median_net_return_5m']:+6.3f}% | "
                    f"Net PnL (15m): {r['median_net_return_15m']:+6.3f}%"
                )

            print("\n💡 VERDICT (Point-in-Time):")
            print("This measures every single active minute in the market over the last 2 days.")
            print(
                "If Extreme flow underperforms Moderate/High, it confirms the Blow-Off Exhaustion hypothesis."
            )

    finally:
        await engine.dispose()


asyncio.run(run())
