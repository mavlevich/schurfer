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
            print("🚀 Quant v7: Early Momentum Prospective Shadow Test...")

            # Pure SQL Point-in-Time joins to prevent data leakage and join explosions
            q = """
            WITH pump_bases AS (
                SELECT p.id, p.base, p.first_seen_at, p.base || 'USDT' as symbol
                FROM app.pump_events p
                WHERE p.first_seen_at >= NOW() - INTERVAL '5 days'
            ),
            flow_15m AS (
                SELECT
                    pb.id,
                    SUM(b.buy_total_notional_usd) as buy_15m,
                    SUM(b.sell_total_notional_usd) as sell_15m,
                    COUNT(b.bucket_start) as bar_count
                FROM pump_bases pb
                JOIN timeseries.bybit_momentum_bars_1m b
                  ON b.symbol = pb.symbol
                 AND b.market_type = 'linear'
                 AND b.bucket_start >= date_trunc('minute', pb.first_seen_at) - INTERVAL '15 minutes'
                 AND b.bucket_start < date_trunc('minute', pb.first_seen_at)
                GROUP BY pb.id
            ),
            outcomes AS (
                SELECT
                    pb.id,
                    t1.open_price as entry_price,
                    t15.close_price as exit_price
                FROM pump_bases pb
                JOIN timeseries.bybit_momentum_bars_1m t1
                  ON t1.symbol = pb.symbol AND t1.market_type = 'linear'
                 AND t1.bucket_start = date_trunc('minute', pb.first_seen_at) + INTERVAL '1 minute'
                JOIN timeseries.bybit_momentum_bars_1m t15
                  ON t15.symbol = pb.symbol AND t15.market_type = 'linear'
                 AND t15.bucket_start = date_trunc('minute', pb.first_seen_at) + INTERVAL '15 minutes'
            )
            SELECT
                pb.base, pb.first_seen_at,
                f.buy_15m, f.sell_15m, f.bar_count,
                o.entry_price, o.exit_price
            FROM pump_bases pb
            JOIN flow_15m f ON f.id = pb.id
            JOIN outcomes o ON o.id = pb.id
            WHERE f.bar_count = 15; -- STRICT continuity: exactly 15 bars prior
            """

            print("Executing Strict SQL Join on Event Database...")
            res = await conn.execute(text(q))
            rows = res.fetchall()

            cols = [
                "base",
                "first_seen_at",
                "buy_15m",
                "sell_15m",
                "bar_count",
                "entry_price",
                "exit_price",
            ]
            df = pd.DataFrame(rows, columns=cols)
            if df.empty:
                return print("No events with full continuity found.")

            print(f"Loaded {len(df)} validated pump events with strict 15m prior continuity.")

            df["total_15m"] = df["buy_15m"] + df["sell_15m"]

            # Liquidity filter
            df = df[df["total_15m"] > 100000].copy()

            df["signed_imbalance"] = (df["buy_15m"] - df["sell_15m"]) / df["total_15m"]

            # Cost Structure (25 bps roundtrip + slippage)
            TOTAL_COST_BPS = 0.25
            df["ret_gross"] = (df["exit_price"] - df["entry_price"]) / df["entry_price"] * 100
            df["ret_net"] = df["ret_gross"] - TOTAL_COST_BPS

            # Classify based on the FROZEN CANDIDATE (0.20 to 0.50 is ACCEPT, Extreme is REJECT)
            def apply_filter(imb):
                if 0.20 <= imb < 0.50:
                    return "ACCEPTED (Moderate)"
                if imb >= 0.50:
                    return "REJECTED (Extreme)"
                return "REJECTED (Low/Sell)"

            df["shadow_decision"] = df["signed_imbalance"].apply(apply_filter)

            # Analyze Results
            print("\n" + "=" * 80)
            print(f"📊 QUANT v7: EARLY MOMENTUM SHADOW TEST (Net {TOTAL_COST_BPS}% Costs)")
            print("=" * 80)

            # 1. Baseline (If we traded every pump signal)
            baseline_win_rate = (df["ret_net"] > 0).mean() * 100
            baseline_median_net = df["ret_net"].median()
            baseline_pf_arr = df["ret_net"].values
            baseline_pf = (
                baseline_pf_arr[baseline_pf_arr > 0].sum()
                / abs(baseline_pf_arr[baseline_pf_arr < 0].sum())
                if abs(baseline_pf_arr[baseline_pf_arr < 0].sum()) > 0
                else np.nan
            )

            print(
                f"[BASELINE - All Signals] Episodes: {len(df):<4} | Win Rate: {baseline_win_rate:5.1f}% | Net: {baseline_median_net:+6.3f}% | PF: {baseline_pf:.2f}"
            )

            # 2. Filter Performance
            matrix = (
                df.groupby("shadow_decision", observed=True)
                .agg(
                    episodes_count=("base", "count"),
                    win_rate=("ret_net", lambda x: (x > 0).mean() * 100),
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
                    f"[{r['shadow_decision']:<20}] Episodes: {r['episodes_count']:<4} | "
                    f"Win Rate: {r['win_rate']:5.1f}% | "
                    f"Net: {r['net_median']:+6.3f}% | "
                    f"PF: {r['profit_factor']:.2f}"
                )

            print("\n💡 VERDICT:")
            print(
                "If 'ACCEPTED (Moderate)' has a Profit Factor > 1.0 and positive Net PnL, the filter adds value."
            )
            print(
                "If 'REJECTED (Extreme)' has negative Net PnL, the Veto logic is statistically correct."
            )

    finally:
        await engine.dispose()


asyncio.run(run())
