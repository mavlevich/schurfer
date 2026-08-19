# Early Momentum Strategy (Accumulation -> Breakout)

## Overview

This document records the findings, parameters, and future ideas for the Early Momentum strategy, discovered and backtested in August 2026.

## Backtest Results (Tick-level Simulation)

- **Dataset:** 24 days of Bybit 1-minute bars (10.5M+ rows).
- **Candidates Found:** 7,200+ accumulation episodes.
- **Best Configuration (Grid Search):**
  - **Stop Loss:** -10%
  - **Take Profit:** +4%
  - **Trades Executed:** 4,416
  - **Win Rate:** 50.3%
  - **Average Net PnL (per trade):** +0.28%
  - **Total Uncompounded Return:** +1222.5%
- **Leverage:** Safe to run at 5x leverage due to the wide -10% Stop Loss (liquidation at 5x happens at -20%). 5x leverage yields a 1.4% ROI on margin per trade.

## Key Insights

1. **Take Profit Sweet Spot:** Pushing Take Profit to +8% or +15% actually _reduces_ average PnL because crypto pumps often retrace quickly. +4% is the statistical sweet spot to lock in the breakout.
2. **Wide Stop Loss:** A tight stop loss (-1% or -2%) gets stopped out by market noise. -10% gives the trade room to breathe while the breakout develops.

## Future Roadmap & Ideas

### 1. Binance Data as a "Whale Radar"

- **Constraint:** Binance Futures execution is blocked for Poland KYC.
- **Solution:** We will build a Binance Connector in `schurfer-collector` purely to consume anonymous Market Data (WebSockets for Klines and Open Interest).
- **Execution:** We will use Binance data to detect massive whale accumulation, but route the actual `BUY` orders to Bybit where execution is supported.

### 2. Orphan Trade Reaper

- **Goal:** Protect capital from desyncs. If the execution engine crashes after sending an order but before recording it to the DB, the position is "orphaned" without a stop-loss.
- **Implementation:** A background worker that compares active exchange positions against the Postgres database and emergency-manages any unrecognized positions.

## For Analysts: How to run the Simulation

If you want to run your own analysis, modify the parameters, or test new ideas, all the heavy lifting tools are included in `apps/analytics/schurfer_analytics/`.

1. **Prerequisites:** Ensure you have the `.venv` active and the DB tunnel open (`ssh -L 5432:localhost:5432 ...`).
2. **Find Candidates:** Run `uv run early-momentum-discovery-report` to generate the base accumulation candidates. This uses heavy SQL Window functions instead of Pandas to avoid OOM limits.
3. **Run Simulator:** Run `uv run python apps/analytics/schurfer_analytics/tick_simulator.py`. This script fetches 1m Klines for every candidate, caches them locally as JSON (so it runs instantly on subsequent runs), and steps through every minute to simulate precise Stop Loss / Take Profit hits.
4. **Experiment:** Open `tick_simulator.py` and modify the grid search ranges (`sl_grid` and `tp_grid`) or add custom exit logic (e.g. trailing stops) inside the `simulate()` loop to test your own strategies!
