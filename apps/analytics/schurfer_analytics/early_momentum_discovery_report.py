#!/usr/bin/env python3
import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("early-momentum-discovery")

SQL_QUERY = """
WITH base AS (
    SELECT
        symbol,
        bucket_start,
        close_price,
        open_interest,
        buy_total_notional_usd,
        sell_total_notional_usd,
        high_price,
        low_price
    FROM timeseries.bybit_momentum_bars_1m
    WHERE open_interest IS NOT NULL
),
rolling AS (
    SELECT
        *,
        -- 2-hour lookback
        FIRST_VALUE(open_interest) OVER w_2h_back AS oi_start_2h,
        MAX(close_price) OVER w_2h_back AS price_max_2h,
        MIN(close_price) OVER w_2h_back AS price_min_2h,
        SUM(buy_total_notional_usd) OVER w_2h_back AS buy_vol_2h,
        SUM(sell_total_notional_usd) OVER w_2h_back AS sell_vol_2h,

        -- 4-hour forward lookahead for MFE/MAE
        MAX(high_price) OVER w_4h_fwd AS fwd_max_price_4h,
        MIN(low_price) OVER w_4h_fwd AS fwd_min_price_4h,
        LEAD(close_price, 240) OVER w_symbol AS fwd_close_4h
    FROM base
    WINDOW
        w_symbol AS (PARTITION BY symbol ORDER BY bucket_start),
        w_2h_back AS (
            PARTITION BY symbol ORDER BY bucket_start
            ROWS BETWEEN 120 PRECEDING AND CURRENT ROW
        ),
        w_4h_fwd AS (
            PARTITION BY symbol ORDER BY bucket_start
            ROWS BETWEEN 1 FOLLOWING AND 240 FOLLOWING
        )
),
candidates AS (
    SELECT
        *,
        (open_interest - oi_start_2h) / NULLIF(oi_start_2h, 0) AS oi_growth,
        (buy_vol_2h - sell_vol_2h) AS net_taker_flow,
        (price_max_2h - price_min_2h) / NULLIF(price_min_2h, 0) AS price_range_pct
    FROM rolling
    WHERE
        (open_interest - oi_start_2h) / NULLIF(oi_start_2h, 0) > 0.05
        AND (buy_vol_2h - sell_vol_2h) > 0
        AND (price_max_2h - price_min_2h) / NULLIF(price_min_2h, 0) < 0.03
)
SELECT * FROM candidates ORDER BY symbol, bucket_start;
"""


def group_into_episodes(
    candidates: list[dict[str, Any]], merge_window_minutes: int = 60
) -> list[dict[str, Any]]:
    episodes = []
    if not candidates:
        return episodes

    current_episode = [candidates[0]]

    for row in candidates[1:]:
        prev_row = current_episode[-1]

        # If same symbol and within the merge window, group them
        is_same_symbol = row["symbol"] == prev_row["symbol"]
        time_diff = (row["bucket_start"] - prev_row["bucket_start"]).total_seconds() / 60.0

        if is_same_symbol and time_diff <= merge_window_minutes:
            current_episode.append(row)
        else:
            episodes.append(current_episode)
            current_episode = [row]

    episodes.append(current_episode)
    return episodes


def simulate_episode(episode: list[dict[str, Any]]) -> dict[str, Any]:
    entry_bar = episode[-1]
    if entry_bar["close_price"] is None:
        return None

    entry_price = float(entry_bar["close_price"])
    fwd_max = float(entry_bar["fwd_max_price_4h"]) if entry_bar["fwd_max_price_4h"] else entry_price
    fwd_min = float(entry_bar["fwd_min_price_4h"]) if entry_bar["fwd_min_price_4h"] else entry_price
    fwd_close = float(entry_bar["fwd_close_4h"]) if entry_bar["fwd_close_4h"] else entry_price

    mfe_pct = (fwd_max - entry_price) / entry_price
    mae_pct = (fwd_min - entry_price) / entry_price
    close_pct = (fwd_close - entry_price) / entry_price

    # Assume a simple strategy: Take Profit at +2%, Stop Loss at -1%, or close at 4h
    take_profit = 0.02
    stop_loss = -0.01
    fee = 0.0012  # 12 bps taker fee (in/out combined estimation)

    # Very naive path dependency estimation since we don't have tick data inside the 4h window
    if mfe_pct >= take_profit and mae_pct > stop_loss:
        pnl = take_profit - fee
        outcome = "take_profit"
    elif mae_pct <= stop_loss:
        pnl = stop_loss - fee
        outcome = "stop_loss"
    else:
        pnl = close_pct - fee
        outcome = "time_exit"

    return {
        "symbol": entry_bar["symbol"],
        "entry_time": entry_bar["bucket_start"].isoformat(),
        "entry_price": entry_price,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "pnl_pct": pnl,
        "outcome": outcome,
        "duration_minutes": len(episode),
    }


async def run_report() -> None:
    cache_file = Path("backups/reports/candidates_cache.json")
    candidates = []

    if cache_file.exists():
        logger.info("Loading candidates from cache (skipping 7-minute SQL query)...")
        import json
        from datetime import datetime

        with open(cache_file) as f:
            candidates = json.load(f)
            for c in candidates:
                c["bucket_start"] = datetime.fromisoformat(c["bucket_start"])
    else:
        db_url = os.environ.get(
            "DATABASE_URL", "postgresql://schurfer:schurfer@localhost:5432/schurfer"
        )
        logger.info("Connecting to DB to compute momentum accumulation + forward lookahead...")
        try:
            async with (
                await psycopg.AsyncConnection.connect(db_url) as conn,
                conn.cursor(row_factory=dict_row) as cur,
            ):
                logger.info(
                    "Executing heavy rolling query with 4h forward lookahead (this might take 3-5 mins)..."
                )
                await cur.execute(SQL_QUERY)
                candidates = await cur.fetchall()

            logger.info("Saving candidates to cache...")
            import json

            with open(cache_file, "w") as f:
                json.dump(candidates, f, default=str)
        except Exception as e:
            logger.error(f"Database query failed: {e}")
            return

    logger.info(f"Found {len(candidates)} raw accumulation minutes.")

    episodes = group_into_episodes(candidates)
    logger.info(f"Grouped into {len(episodes)} unique accumulation episodes (trade setups).")

    results = [res for ep in episodes if (res := simulate_episode(ep)) is not None]

    total_trades = len(results)
    wins = len([r for r in results if r["pnl_pct"] > 0])
    win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0
    total_pnl = sum(r["pnl_pct"] for r in results)

    outcomes = {}
    for r in results:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1

    report = {
        "summary": {
            "total_raw_signals": len(candidates),
            "total_episodes_traded": total_trades,
            "win_rate_pct": win_rate,
            "total_simulated_pnl_pct": total_pnl * 100,
            "avg_pnl_per_trade_pct": (total_pnl / total_trades * 100) if total_trades else 0,
            "outcomes": outcomes,
        },
        "trades": results[:100],  # Save first 100 for review
    }

    out_dir = Path("backups/reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "early-momentum-discovery-v2.json"

    with out_file.open("w") as f:
        json.dump(report, f, indent=2, default=str)

    logger.info("=== REPORT SUMMARY ===")
    logger.info(f"Episodes (Trades): {total_trades}")
    logger.info(f"Win Rate: {win_rate:.2f}%")
    logger.info(
        f"Average PnL per trade: {(total_pnl / total_trades * 100) if total_trades else 0:.2f}%"
    )
    logger.info(f"Saved detailed report to {out_file}")


def main() -> None:
    asyncio.run(run_report())


if __name__ == "__main__":
    main()
