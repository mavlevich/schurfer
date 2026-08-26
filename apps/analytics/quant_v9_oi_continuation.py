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
            print("🚀 Quant v9: Post-Event OI Continuation Test...")

            # SQL strictly fetches OI at T0 and OI at T+2m, along with exact entry/exit prices
            q = """
            WITH pump_bases AS (
                SELECT p.id, p.base, p.first_seen_at, p.base || 'USDT' as symbol
                FROM app.pump_events p
                WHERE p.first_seen_at >= NOW() - INTERVAL '5 days'
            ),
            oi_start AS (
                SELECT event_id, oi_usd,
                       ROW_NUMBER() OVER(PARTITION BY event_id ORDER BY recorded_at ASC) as rn
                FROM app.oi_snapshots
            ),
            oi_obs AS (
                SELECT oi.event_id, oi.oi_usd,
                       ROW_NUMBER() OVER(PARTITION BY oi.event_id ORDER BY oi.recorded_at ASC) as rn
                FROM app.oi_snapshots oi
                JOIN pump_bases pb ON pb.id = oi.event_id
                WHERE oi.recorded_at >= pb.first_seen_at + INTERVAL '2 minutes'
            ),
            outcomes AS (
                SELECT
                    pb.id,
                    t2.open_price as entry_price,
                    t15.close_price as exit_price
                FROM pump_bases pb
                JOIN timeseries.bybit_momentum_bars_1m t2
                  ON t2.symbol = pb.symbol AND t2.market_type = 'linear'
                 AND t2.bucket_start = date_trunc('minute', pb.first_seen_at) + INTERVAL '2 minutes'
                JOIN timeseries.bybit_momentum_bars_1m t15
                  ON t15.symbol = pb.symbol AND t15.market_type = 'linear'
                 AND t15.bucket_start = date_trunc('minute', pb.first_seen_at) + INTERVAL '15 minutes'
            )
            SELECT
                pb.id, pb.base, pb.first_seen_at,
                os.oi_usd as oi_initial,
                oo.oi_usd as oi_after_2m,
                o.entry_price, o.exit_price
            FROM pump_bases pb
            JOIN oi_start os ON os.event_id = pb.id AND os.rn = 1
            JOIN oi_obs oo ON oo.event_id = pb.id AND oo.rn = 1
            JOIN outcomes o ON o.id = pb.id;
            """

            print("Executing Strict Time-JOINs on OI & Price...")
            res = await conn.execute(text(q))
            rows = res.fetchall()

            cols = [
                "event_id",
                "base",
                "first_seen_at",
                "oi_initial",
                "oi_after_2m",
                "entry_price",
                "exit_price",
            ]
            df = pd.DataFrame(rows, columns=cols)
            if df.empty:
                return print("❌ No events found with full 15m continuity and OI snapshots.")

            print(f"Loaded {len(df)} validated pump events with OI observation windows.")

            # Calculate OI Delta % over the 2-minute observation window
            df["oi_delta_pct"] = (df["oi_after_2m"] - df["oi_initial"]) / df["oi_initial"] * 100

            # Calculate executable returns (Entry at T+2, Exit at T+15)
            TOTAL_COST_BPS = 0.25
            df["ret_gross"] = (df["exit_price"] - df["entry_price"]) / df["entry_price"] * 100
            df["ret_net"] = df["ret_gross"] - TOTAL_COST_BPS

            # Classify OI Behavior
            def classify_oi(delta):
                if delta > 2.0:
                    return "Strong OI Increase (> 2%)"
                if delta > 0.0:
                    return "Moderate OI Increase (0-2%)"
                return "OI Decrease (Short Covering)"

            df["oi_regime"] = df["oi_delta_pct"].apply(classify_oi)

            # Analyze Results
            print("\n" + "=" * 80)
            print(
                f"📊 QUANT v9: OPEN INTEREST CONTINUATION (Entry at T+2m, Net {TOTAL_COST_BPS}% Costs)"
            )
            print("=" * 80)

            # 1. Baseline
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
                f"[BASELINE (All)] Episodes: {len(df):<3} | Win Rate: {baseline_win_rate:5.1f}% | Net: {baseline_median_net:+6.3f}% | PF: {baseline_pf:.2f}"
            )
            print("-" * 80)

            # 2. OI Regimes
            matrix = (
                df.groupby("oi_regime", observed=True)
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

            # Sort by win rate descending
            matrix = matrix.sort_values(by="win_rate", ascending=False)

            for _, r in matrix.iterrows():
                print(
                    f"[{r['oi_regime']:<28}] Episodes: {r['episodes_count']:<3} | "
                    f"Win Rate: {r['win_rate']:5.1f}% | "
                    f"Net: {r['net_median']:+6.3f}% | "
                    f"PF: {r['profit_factor']:.2f}"
                )

            print("\n💡 VERDICT:")
            print(
                "If 'OI Increase' leads to positive Net PnL and PF > 1.0, the Continuation Hypothesis holds."
            )
            print("If 'OI Decrease' wins, it confirms the Short Squeeze hypothesis.")

    finally:
        await engine.dispose()


asyncio.run(run())
