# ruff: noqa
import json
from datetime import datetime
from pathlib import Path


def explore():
    cache_file = Path("backups/reports/candidates_cache.json")
    with open(cache_file) as f:
        candidates = json.load(f)

    episodes = []
    if not candidates:
        return

    current_episode = [candidates[0]]
    for row in candidates[1:]:
        prev_row = current_episode[-1]
        is_same_symbol = row["symbol"] == prev_row["symbol"]

        t1 = datetime.fromisoformat(str(row["bucket_start"]))
        t2 = datetime.fromisoformat(str(prev_row["bucket_start"]))
        time_diff = (t1 - t2).total_seconds() / 60.0

        if is_same_symbol and time_diff <= 60:
            current_episode.append(row)
        else:
            episodes.append(current_episode)
            current_episode = [row]
    episodes.append(current_episode)

    fee = 0.0012
    total_episodes = len(episodes)

    print(f"Total Accumulation Episodes: {total_episodes}")
    print("SL%\tTP%\tTrades\tWinRate\tAvgPnL")

    for sl in [0.01, 0.02, 0.03, 0.05, 0.08]:
        for tp in [0.01, 0.02, 0.04, 0.08, 0.15]:
            stop_loss = -sl
            take_profit = tp

            breakouts = 0
            wins = 0
            losses = 0
            total_pnl = 0.0

            for ep in episodes:
                entry_bar = ep[-1]
                if entry_bar["close_price"] is None:
                    continue

                ceiling = float(entry_bar["price_max_2h"])
                fwd_max = (
                    float(entry_bar["fwd_max_price_4h"]) if entry_bar["fwd_max_price_4h"] else 0
                )
                fwd_min = (
                    float(entry_bar["fwd_min_price_4h"]) if entry_bar["fwd_min_price_4h"] else 0
                )

                if fwd_max > ceiling:
                    breakouts += 1
                    entry_price = ceiling

                    mfe_pct = (fwd_max - entry_price) / entry_price
                    mae_pct = (fwd_min - entry_price) / entry_price

                    if mae_pct <= stop_loss:
                        pnl = stop_loss - fee
                        losses += 1
                    elif mfe_pct >= take_profit:
                        pnl = take_profit - fee
                        wins += 1
                    else:
                        fwd_close = (
                            float(entry_bar["fwd_close_4h"])
                            if entry_bar["fwd_close_4h"]
                            else entry_price
                        )
                        pnl = ((fwd_close - entry_price) / entry_price) - fee
                        if pnl > 0:
                            wins += 1
                        else:
                            losses += 1

                    total_pnl += pnl

            trades = wins + losses
            win_rate = (wins / trades) * 100 if trades > 0 else 0
            avg_pnl = (total_pnl / trades) * 100 if trades > 0 else 0

            print(f"-{sl * 100:.0f}%\t+{tp * 100:.0f}%\t{trades}\t{win_rate:.1f}%\t{avg_pnl:.2f}%")


if __name__ == "__main__":
    explore()
