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
            print("🚀 Quant Methodology v4: Episode-based Data Funnel...")

            # The reviewer explicitly requested strict joins (no multiplication)
            # We use ROW_NUMBER() to strictly deduplicate any overlapping versions.
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
            dedup_eligible AS (
                SELECT *, ROW_NUMBER() OVER(PARTITION BY symbol, t_bucket ORDER BY total_vol DESC) as rn
                FROM eligible_minutes
            ),
            forward_returns AS (
                SELECT
                    b.symbol,
                    b.t_bucket,
                    b.buy_vol,
                    b.sell_vol,
                    b.total_vol,
                    t1.open_price as entry_price,
                    t15.close_price as close_15m
                FROM dedup_eligible b
                JOIN timeseries.bybit_momentum_bars_1m t1
                    ON t1.symbol = b.symbol AND t1.market_type = 'linear' AND t1.bucket_start = b.t_bucket + INTERVAL '1 minute'
                LEFT JOIN timeseries.bybit_momentum_bars_1m t15
                    ON t15.symbol = b.symbol AND t15.market_type = 'linear' AND t15.bucket_start = b.t_bucket + INTERVAL '15 minutes'
                WHERE b.rn = 1
            )
            SELECT * FROM forward_returns ORDER BY symbol, t_bucket;
            """
            print("Executing SQL & strict deduplication...")
            res = await conn.execute(text(q))
            rows = res.fetchall()

            cols = [
                "symbol",
                "t_bucket",
                "buy_vol",
                "sell_vol",
                "total_vol",
                "entry_price",
                "close_15m",
            ]
            df = pd.DataFrame(rows, columns=cols)
            if df.empty:
                return print("No data.")

            df["buy_imbalance"] = df["buy_vol"] / df["total_vol"]

            # 1. Temporal Split (No Lookahead Bias)
            mid_point = df["t_bucket"].min() + pd.Timedelta(days=1)
            discovery_df = df[df["t_bucket"] < mid_point]
            validation_df = df[df["t_bucket"] >= mid_point].copy()

            if discovery_df.empty or validation_df.empty:
                return print("Not enough data to split into discovery and validation.")

            print(
                f"Loaded {len(df)} total minutes. Split: {len(discovery_df)} Discovery, {len(validation_df)} Validation."
            )

            # 2. Frozen Quantiles (Train on Discovery, Apply to Validation)
            try:
                _, bins = pd.qcut(
                    discovery_df["buy_imbalance"], q=3, retbins=True, duplicates="drop"
                )
            except ValueError:
                bins = [0, 0.45, 0.65, 1.0]  # Fallback

            bins[0] = -0.01
            bins[-1] = 1.01

            validation_df["flow_regime"] = pd.cut(
                validation_df["buy_imbalance"], bins=bins, labels=["Moderate", "High", "Extreme"]
            )

            # 3. EPISODE BUILDER (60 min cooldown to eliminate overlap)
            print("Building Independent Episodes with 60m cooldown...")
            validation_df = validation_df.sort_values(by=["symbol", "t_bucket"])
            episodes = []

            for symbol, group in validation_df.groupby("symbol"):
                last_event_time = group["t_bucket"].iloc[0] - pd.Timedelta(minutes=61)
                for _, row in group.iterrows():
                    if row["t_bucket"] >= last_event_time + pd.Timedelta(minutes=60):
                        episodes.append(row)
                        last_event_time = row["t_bucket"]

            ep_df = pd.DataFrame(episodes)
            print(
                f"Reduced {len(validation_df)} overlapping minutes to {len(ep_df)} independent episodes."
            )

            # 4. FULL COSTS (Entry + Exit + Slippage)
            TAKER_IN = 0.10
            TAKER_OUT = 0.10
            SLIPPAGE = 0.05
            TOTAL_COST_BPS = TAKER_IN + TAKER_OUT + SLIPPAGE

            ep_df["ret_15m_gross"] = (
                (ep_df["close_15m"] - ep_df["entry_price"]) / ep_df["entry_price"] * 100
            )
            ep_df["ret_15m_net"] = ep_df["ret_15m_gross"] - TOTAL_COST_BPS

            print("\n" + "=" * 80)
            print(
                f"📊 QUANT v4: INDEPENDENT EPISODES (Validation Set, Net of {TOTAL_COST_BPS}% roundtrip costs)"
            )
            print("=" * 80)

            matrix = (
                ep_df.groupby("flow_regime", observed=True)
                .agg(
                    episodes_count=("symbol", "count"),
                    win_rate=("ret_15m_net", lambda x: (x > 0).mean() * 100),
                    median_net_return=("ret_15m_net", "median"),
                    worst_loss=("ret_15m_net", "min"),
                    best_gain=("ret_15m_net", "max"),
                )
                .reset_index()
            )

            for _, r in matrix.iterrows():
                print(
                    f"[{r['flow_regime']:<8} Flow] Episodes: {r['episodes_count']:<5} | "
                    f"Win Rate: {r['win_rate']:5.1f}% | "
                    f"Median Net PnL: {r['median_net_return']:+6.3f}% | "
                    f"Worst: {r['worst_loss']:+6.2f}% | Best: {r['best_gain']:+6.2f}%"
                )

    finally:
        await engine.dispose()


asyncio.run(run())
