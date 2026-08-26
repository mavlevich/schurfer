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
            print("🚀 Quant Methodology v6: Strict Continuous Time & Exact Outcomes...")

            # Fetch 5 days to ensure fresh validation embargo
            q = """
            SELECT
                symbol,
                bucket_start as t_bucket,
                buy_total_notional_usd as buy_vol,
                sell_total_notional_usd as sell_vol,
                open_price,
                close_price
            FROM timeseries.bybit_momentum_bars_1m
            WHERE market_type = 'linear'
              AND bucket_start >= NOW() - INTERVAL '5 days'
              AND bucket_start <= NOW() - INTERVAL '1 hour'
            ORDER BY symbol, t_bucket;
            """
            print("Fetching data from DB...")
            res = await conn.execute(text(q))
            rows = res.fetchall()

            cols = ["symbol", "t_bucket", "buy_vol", "sell_vol", "open_price", "close_price"]
            df = pd.DataFrame(rows, columns=cols)
            if df.empty:
                return print("No data.")

            df["t_bucket"] = pd.to_datetime(df["t_bucket"])

            # P0 Fix: Enforce Continuous Timeline & Exact 15 Bars
            print("Enforcing strictly continuous time series (exposing missing bars as gaps)...")

            # Deduplicate just in case there are multiple capture_versions in the DB for the same minute
            df = df.drop_duplicates(subset=["symbol", "t_bucket"])

            def process_symbol(group):
                # Set index and select numeric columns before resampling. Gaps become NaN.
                cols = ["buy_vol", "sell_vol", "open_price", "close_price"]
                group = group.set_index("t_bucket")[cols].resample("1min").asfreq()
                # min_periods=15 ensures ANY gap in the last 15 mins invalidates the feature entirely
                group["buy_15m"] = group["buy_vol"].rolling(15, min_periods=15).sum()
                group["sell_15m"] = group["sell_vol"].rolling(15, min_periods=15).sum()
                return group

            df = df.groupby("symbol").apply(process_symbol).reset_index()

            # Calculate point-in-time features on continuous data
            df["total_15m"] = df["buy_15m"] + df["sell_15m"]
            df["signed_imbalance_15m"] = (df["buy_15m"] - df["sell_15m"]) / df["total_15m"]

            def get_regime(row):
                imb = row["signed_imbalance_15m"]
                total = row["total_15m"]
                # P1 Fix: Liquidity filter applied AT state evaluation, not removing rows before
                if pd.isna(imb) or pd.isna(total) or total < 100000:
                    return "Neutral"
                if 0.20 <= imb < 0.50:
                    return "Moderate_Buy"
                if imb >= 0.50:
                    return "Extreme_Buy"
                return "Neutral"

            print("Applying regimes...")
            df["regime"] = df.apply(get_regime, axis=1)

            episodes = []
            print("Building crossing episodes with exact T+1/T+15 lookups...")

            for symbol, group in df.groupby("symbol"):
                group = group.sort_values("t_bucket")
                last_ep_time = (
                    pd.Timestamp.min.tz_localize(group["t_bucket"].dt.tz)
                    if group["t_bucket"].dt.tz
                    else pd.Timestamp.min
                )

                prev_regime = "Neutral"
                group_indexed = group.set_index("t_bucket")

                for t, row in group_indexed.iterrows():
                    curr_regime = row["regime"]

                    if curr_regime in ["Moderate_Buy", "Extreme_Buy"]:
                        # Only trigger on CROSSING into the regime, and if cooldown passed
                        if prev_regime != curr_regime and (
                            t >= last_ep_time + pd.Timedelta(minutes=60)
                        ):
                            # P0 Fix: Exact time lookups for outcomes
                            t_entry = t + pd.Timedelta(minutes=1)
                            t_exit = t + pd.Timedelta(minutes=15)

                            if t_entry in group_indexed.index and t_exit in group_indexed.index:
                                entry_price = group_indexed.loc[t_entry, "open_price"]
                                exit_price = group_indexed.loc[t_exit, "close_price"]

                                # Ensure both prices are actually present (not NaN from gaps)
                                if not pd.isna(entry_price) and not pd.isna(exit_price):
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
            print(f"Generated {len(ep_df)} TRUE Point-in-Time Episodes.")

            # Strict temporal embargo split
            mid_point = (
                ep_df["t_bucket"].min() + (ep_df["t_bucket"].max() - ep_df["t_bucket"].min()) / 2
            )
            val_ep_df = ep_df[ep_df["t_bucket"] >= mid_point].copy()

            print(f"Validation Set (Fresh Embargo Data): {len(val_ep_df)} Episodes")

            if val_ep_df.empty:
                return print("No validation episodes.")

            # Full costs: 10bps taker in + 10bps taker out + 5bps slippage = 25bps
            TOTAL_COST_BPS = 0.25
            val_ep_df["ret_gross"] = (
                (val_ep_df["exit_price"] - val_ep_df["entry_price"])
                / val_ep_df["entry_price"]
                * 100
            )
            val_ep_df["ret_net"] = val_ep_df["ret_gross"] - TOTAL_COST_BPS

            print("\n" + "=" * 80)
            print(
                f"📊 QUANT v6: EXACT CONTINUOUS OUTCOMES (Validation Split, Net {TOTAL_COST_BPS}%)"
            )
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
                    f"PF: {r['profit_factor']:.2f}"
                )

            print(
                "\n💡 VERDICT: If net PnL is still negative, standalone flow crossing is OFFICIALLY DEAD."
            )

    finally:
        await engine.dispose()


asyncio.run(run())
