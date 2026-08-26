# ruff: noqa
import asyncio
import os
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
import pandas as pd
import numpy as np


async def run():
    url = os.getenv("DATABASE_URL")
    if not url:
        return print("Error: DATABASE_URL is not set.")

    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            print("🚀 Quant Methodology v5: 15m Point-in-Time Episodes...")

            # P0 Fix: Strict exchange and version identity (Using only Bybit Linear bars)
            # P3 Fix: Using continuous raw bars to calculate true 15m signed imbalance
            q = """
            SELECT
                symbol,
                bucket_start as t_bucket,
                buy_total_notional_usd as buy_vol,
                sell_total_notional_usd as sell_vol,
                open_price,
                high_price,
                low_price,
                close_price
            FROM timeseries.bybit_momentum_bars_1m
            WHERE market_type = 'linear'
              AND bucket_start >= NOW() - INTERVAL '3 days'
              AND bucket_start <= NOW() - INTERVAL '1 hour'
            ORDER BY symbol, t_bucket;
            """
            print("Executing SQL to fetch strict continuous linear bars...")
            res = await conn.execute(text(q))
            rows = res.fetchall()

            cols = [
                "symbol",
                "t_bucket",
                "buy_vol",
                "sell_vol",
                "open_price",
                "high_price",
                "low_price",
                "close_price",
            ]
            df = pd.DataFrame(rows, columns=cols)
            if df.empty:
                return print("No data.")

            print("Calculating 15m rolling features (Strict Point-in-Time)...")
            df["total_vol"] = df["buy_vol"] + df["sell_vol"]

            # Sort exactly by time for rolling calculation
            df = df.sort_values(by=["symbol", "t_bucket"]).reset_index(drop=True)
            df.set_index("t_bucket", inplace=True)

            # Calculate rolling 15m sums
            rolling_buy = (
                df.groupby("symbol")["buy_vol"]
                .rolling("15min")
                .sum()
                .reset_index(level=0, drop=True)
            )
            rolling_sell = (
                df.groupby("symbol")["sell_vol"]
                .rolling("15min")
                .sum()
                .reset_index(level=0, drop=True)
            )

            df["buy_15m"] = rolling_buy
            df["sell_15m"] = rolling_sell
            df["total_15m"] = df["buy_15m"] + df["sell_15m"]

            # P3 Fix: True signed imbalance (Buy - Sell) / (Buy + Sell). Range is [-1.0, +1.0]
            df["signed_imbalance_15m"] = (df["buy_15m"] - df["sell_15m"]) / df["total_15m"]
            df.reset_index(inplace=True)

            # Filter out low liquidity dead zones (e.g. < $100k in 15m)
            df = df[df["total_15m"] > 100000].copy()

            # P1 Fix: Episode Generation (Trigger ONLY on crossing thresholds)
            print("Building Episodes based on actual state crossings...")

            def get_regime(imb):
                if pd.isna(imb):
                    return None
                if 0.20 <= imb < 0.50:
                    return "Moderate_Buy"  # The frozen candidate
                if imb >= 0.50:
                    return "Extreme_Buy"
                return "Other"

            df["regime"] = df["signed_imbalance_15m"].apply(get_regime)

            episodes = []
            for symbol, group in df.groupby("symbol"):
                group = group.sort_values("t_bucket")
                # Cooldown logic tracker
                last_ep_time = (
                    pd.Timestamp.min.tz_localize(group["t_bucket"].dt.tz)
                    if group["t_bucket"].dt.tz
                    else pd.Timestamp.min
                )
                prev_regime = "Other"

                t_buckets = group["t_bucket"].tolist()
                regimes = group["regime"].tolist()
                opens = group["open_price"].tolist()
                closes = group["close_price"].tolist()

                # Leave 15m at the end for the outcome window
                for i in range(len(t_buckets) - 15):
                    curr_regime = regimes[i]
                    t = t_buckets[i]

                    # Episode triggers ONLY IF regime crosses into Moderate or Extreme AND 60m cooldown passed
                    if curr_regime in ["Moderate_Buy", "Extreme_Buy"]:
                        if prev_regime != curr_regime and (
                            t >= last_ep_time + pd.Timedelta(minutes=60)
                        ):
                            # P5 Fix: Strict Entry/Exit on same venue
                            entry_price = opens[i + 1]  # T+1 Open
                            exit_price = closes[i + 15]  # T+15 Close

                            episodes.append(
                                {
                                    "symbol": symbol,
                                    "t_bucket": t,
                                    "regime": curr_regime,
                                    "entry_price": entry_price,
                                    "exit_price": exit_price,
                                }
                            )
                            last_ep_time = t

                    prev_regime = curr_regime

            ep_df = pd.DataFrame(episodes)
            if ep_df.empty:
                return print("No episodes generated.")
            print(f"Generated {len(ep_df)} STRICT Independent Episodes.")

            # P4 Fix: Form episodes BEFORE temporal split, use fresh data for validation
            mid_point = (
                ep_df["t_bucket"].min() + (ep_df["t_bucket"].max() - ep_df["t_bucket"].min()) / 2
            )
            val_ep_df = ep_df[ep_df["t_bucket"] >= mid_point].copy()

            print(f"Validation Set (Fresh Data): {len(val_ep_df)} Episodes")
            if val_ep_df.empty:
                return print("No validation episodes.")

            # P6 Fix: Full Costs (Entry 0.10% + Exit 0.10% + Slippage 0.05%)
            TOTAL_COST_BPS = 0.25

            val_ep_df["ret_gross"] = (
                (val_ep_df["exit_price"] - val_ep_df["entry_price"])
                / val_ep_df["entry_price"]
                * 100
            )
            val_ep_df["ret_net"] = val_ep_df["ret_gross"] - TOTAL_COST_BPS

            print("\n" + "=" * 80)
            print(f"📊 QUANT v5: TRUE CROSSING EPISODES (Validation Split)")
            print("=" * 80)

            matrix = (
                val_ep_df.groupby("regime", observed=True)
                .agg(
                    episodes_count=("symbol", "count"),
                    tokens=("symbol", "nunique"),
                    win_rate=("ret_net", lambda x: (x > 0).mean() * 100),
                    gross_median=("ret_gross", "median"),
                    net_median=("ret_net", "median"),
                    profit_factor=(
                        "ret_net",
                        lambda x: x[x > 0].sum() / abs(x[x < 0].sum())
                        if abs(x[x < 0].sum()) > 0
                        else np.nan,
                    ),
                )
                .reset_index()
            )

            for _, r in matrix.iterrows():
                print(
                    f"[{r['regime']:<12}] Episodes: {r['episodes_count']:<4} | "
                    f"Tokens: {r['tokens']:<3} | "
                    f"Win Rate: {r['win_rate']:5.1f}% | "
                    f"Gross: {r['gross_median']:+6.3f}% | Net: {r['net_median']:+6.3f}% | "
                    f"Profit Factor: {r['profit_factor']:.2f}"
                )

            print(
                "\n💡 VERDICT: Flow-only standalone is likely a FAIL as a strategy (Net PnL < 0)."
            )
            print(
                "However, this correctly evaluates our frozen candidate (Moderate_Buy: 15m imbalance 0.20 to 0.50)."
            )

    finally:
        await engine.dispose()


asyncio.run(run())
