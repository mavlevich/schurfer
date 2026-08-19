# ruff: noqa
#!/usr/bin/env python3
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("tick-simulator")


def group_into_episodes(
    candidates: list[dict[str, Any]], merge_window_minutes: int = 60
) -> list[dict[str, Any]]:
    episodes = []
    if not candidates:
        return episodes

    current_episode = [candidates[0]]
    for row in candidates[1:]:
        prev_row = current_episode[-1]
        is_same_symbol = row["symbol"] == prev_row["symbol"]

        t1 = datetime.fromisoformat(str(row["bucket_start"]))
        t2 = datetime.fromisoformat(str(prev_row["bucket_start"]))
        time_diff = (t1 - t2).total_seconds() / 60.0

        if is_same_symbol and time_diff <= merge_window_minutes:
            current_episode.append(row)
        else:
            episodes.append(current_episode)
            current_episode = [row]

    episodes.append(current_episode)
    return episodes


async def fetch_bars(cur, symbol: str, entry_time: datetime, duration_hours: int = 4):
    query = """
        SELECT bucket_start, high_price, low_price, close_price
        FROM timeseries.bybit_momentum_bars_1m
        WHERE symbol = %s AND bucket_start >= %s AND bucket_start <= %s
        ORDER BY bucket_start ASC
    """
    end_time = entry_time + timedelta(hours=duration_hours)
    await cur.execute(query, (symbol, entry_time, end_time))
    return await cur.fetchall()


async def worker(queue: asyncio.Queue, db_url: str, results: list):
    try:
        async with await psycopg.AsyncConnection.connect(db_url) as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                while True:
                    episode = await queue.get()
                    if episode is None:
                        break

                    entry_bar = episode[-1]
                    if entry_bar["close_price"] is None:
                        queue.task_done()
                        continue

                    symbol = entry_bar["symbol"]
                    entry_time = datetime.fromisoformat(str(entry_bar["bucket_start"]))

                    # We enter on BREAKOUT. Breakout is when price exceeds the 2h accumulation ceiling.
                    ceiling = float(entry_bar["price_max_2h"])

                    # Fetch 4h of forward bars
                    bars = await fetch_bars(cur, symbol, entry_time)

                    results.append(
                        {
                            "episode": episode,
                            "entry_bar": entry_bar,
                            "symbol": symbol,
                            "ceiling": ceiling,
                            "forward_bars": bars,
                        }
                    )
                    queue.task_done()
    except Exception as e:
        logger.error(f"Worker failed: {e}")
        # Clear the queue so it doesn't hang
        while not queue.empty():
            queue.get_nowait()
            queue.task_done()


async def run_simulation() -> None:
    cache_file = Path("backups/reports/candidates_cache.json")
    if not cache_file.exists():
        logger.error("No candidates cache found. Run early_momentum_discovery_report.py first.")
        return

    logger.info("Loading cached accumulation candidates...")
    with open(cache_file) as f:
        candidates = json.load(f)

    episodes = group_into_episodes(candidates)
    logger.info(f"Grouped into {len(episodes)} episodes. Fetching forward ticks for backtest...")

    bars_cache_file = Path("backups/reports/forward_bars_cache.json")
    if bars_cache_file.exists():
        logger.info("Loading forward bars from cache...")
        with open(bars_cache_file) as f:
            results = json.load(f)
    else:
        db_url = os.environ.get(
            "DATABASE_URL", "postgresql://schurfer:schurfer@localhost:5432/schurfer"
        )

        queue = asyncio.Queue()
        for ep in episodes:
            queue.put_nowait(ep)

        results = []
        # 20 concurrent connections to blast through 7000 small queries
        workers = [asyncio.create_task(worker(queue, db_url, results)) for _ in range(20)]

        # Progress reporter
        async def reporter():
            total = len(episodes)
            while not queue.empty():
                rem = queue.qsize()
                logger.info(
                    f"Progress: {total - rem}/{total} ({((total - rem) / total) * 100:.1f}%)"
                )
                await asyncio.sleep(2)

        rep_task = asyncio.create_task(reporter())

        await queue.join()
        rep_task.cancel()

        for _ in workers:
            queue.put_nowait(None)
        await asyncio.gather(*workers)

        with open(bars_cache_file, "w") as f:
            json.dump(results, f, default=str)

    logger.info("Tick fetch complete. Simulating parameters...")

    fee = 0.0012
    # Grid search exactly as requested
    print("\n=== TICK-LEVEL BACKTEST RESULTS ===")
    print("SL%\tTP%\tTrades\tWinRate\tAvgPnL\tTotalReturn")

    best_combo = None
    best_pnl = -9999

    for sl_pct in [0.03, 0.05, 0.08, 0.10]:
        for tp_pct in [0.02, 0.04, 0.08, 0.15]:
            wins = 0
            losses = 0
            total_pnl = 0.0
            trades = 0

            for res in results:
                bars = res["forward_bars"]
                ceiling = res["ceiling"]

                # Check if it ever broke out
                entered = False
                entry_price = ceiling
                take_profit_price = entry_price * (1 + tp_pct)
                stop_loss_price = entry_price * (1 - sl_pct)

                for bar in bars:
                    if (
                        bar.get("high_price") is None
                        or bar.get("low_price") is None
                        or bar.get("close_price") is None
                    ):
                        continue
                    high = float(bar["high_price"])
                    low = float(bar["low_price"])
                    close = float(bar["close_price"])

                    if not entered:
                        if high >= entry_price:
                            entered = True
                            trades += 1
                            # Did it instantly hit TP or SL in the same minute?
                            # Assume worst case: if low <= SL, it hit SL first.
                            if low <= stop_loss_price:
                                total_pnl += -sl_pct - fee
                                losses += 1
                                break
                            if high >= take_profit_price:
                                total_pnl += tp_pct - fee
                                wins += 1
                                break
                    else:
                        # We are in the trade
                        if low <= stop_loss_price:
                            total_pnl += -sl_pct - fee
                            losses += 1
                            break
                        if high >= take_profit_price:
                            total_pnl += tp_pct - fee
                            wins += 1
                            break
                else:
                    # Time limit reached (4 hours)
                    if entered and bars:
                        last_close = None
                        for b in reversed(bars):
                            if b.get("close_price") is not None:
                                last_close = float(b["close_price"])
                                break
                        if last_close is None:
                            last_close = entry_price

                        pnl = ((last_close - entry_price) / entry_price) - fee
                        total_pnl += pnl
                        if pnl > 0:
                            wins += 1
                        else:
                            losses += 1

            win_rate = (wins / trades) * 100 if trades > 0 else 0
            avg_pnl = (total_pnl / trades) * 100 if trades > 0 else 0
            tot_ret = total_pnl * 100

            print(
                f"-{sl_pct * 100:.0f}%\t+{tp_pct * 100:.0f}%\t{trades}\t{win_rate:.1f}%\t{avg_pnl:.2f}%\t{tot_ret:.1f}%"
            )

            if avg_pnl > best_pnl and trades > 100:
                best_pnl = avg_pnl
                best_combo = (sl_pct, tp_pct, trades, win_rate, avg_pnl, tot_ret)

    print("\n=== BEST CONFIGURATION ===")
    print(f"Stop Loss: -{best_combo[0] * 100:.0f}%")
    print(f"Take Profit: +{best_combo[1] * 100:.0f}%")
    print(f"Trades: {best_combo[2]}")
    print(f"Win Rate: {best_combo[3]:.1f}%")
    print(f"Avg PnL: {best_combo[4]:.2f}%")
    print(f"Total Return: {best_combo[5]:.1f}%")


def main() -> None:
    asyncio.run(run_simulation())


if __name__ == "__main__":
    main()
