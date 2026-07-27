# Strategy: pump_short_v1

Status: current deployed baseline (DRY_RUN). This document describes the automated
strategy exactly as it runs, so it can serve as the fixed `v1` baseline that virtual
challengers are compared against. Open questions and proposed changes live in
`docs/research/pump-short-hypotheses.md`, not here.

## Hypothesis

Low-liquidity tokens that pump hard in a short window tend to mean-revert. Shorting the
exhaustion of the pump on perps captures the retrace.

## Trigger (scanner)

- `PUMP_ENTRY_MIN_PCT=30`: a token becomes eligible for this strategy once its **24h
  ticker change on an exchange** reaches +30% (this is the live ticker change, not the
  episode's historical peak).
- `PUMP_MEASUREMENT_MIN_PCT=20` feeds a separate private research path. Observations
  from +20% through +30% are recorded under `pump_short_measurement_v1`, rechecked
  every minute, and can never reach the v1 order path. They are absent from the public
  pumps feed and Telegram.
- The token must trade on a perp on at least one configured exchange.
- The trader evaluates each candidate once (a per-token `seen` key with a TTL debounces
  re-evaluation: 1 min below the entry floor, 30 min after a stable skip, 5 min after
  an entry-quality wait, 24 h after a trade).

## Signal score and gates

Every candidate is scored and every decision (enter or skip) is recorded with its full
context to `app.trade_decisions`.

- `SCORE_THRESHOLD=6`: score below this is skipped. The composite score is built from
  pump age, price extent, OI trend, funding rate, and retrace-from-peak.
- Funding: `REQUIRE_FUNDING_RATE=false` (a missing funding rate does not block);
  `MIN_FUNDING_RATE_PCT=-0.1` (a funding-rate risk check).
- Entry confirmation, both **off** by default: `REQUIRE_RED_CANDLE=false`,
  `MIN_RETRACE_PCT=0`. So an entry can fire near the peak without a confirmed reversal —
  see HYP-002.
- Liquidation guard: an entry is skipped if the initial SL sits too close to the
  liquidation price given leverage and `LIQUIDATION_BUFFER_PCT=20`.

## Sizing

- Fixed notional `SIGNAL_POSITION_USD=50` per trade; `RISK_PER_TRADE_PCT=0` (risk-based
  sizing is wired but off, so sizing is currently flat).
- Leverage `SIGNAL_LEVERAGE=3`.
- Portfolio caps: `MAX_POSITIONS=5`, `MAX_POSITION_USD=500`, daily loss limit
  `DAILY_LOSS_LIMIT_USD=200`.

## Exit (3-phase dynamic, scales with pump size)

There is **no fixed take-profit**. Phases: initial stop -> trailing activation ->
trailing (tightens after a while) -> max-hold timeout. Parameters by pump magnitude:

| Pump size | Initial SL | Trail activation | Trail | Tighten to | Tighten after | Max hold |
| --------- | ---------- | ---------------- | ----- | ---------- | ------------- | -------- |
| < 50%     | 8%         | 8%               | 12%   | 8%         | 90 min        | 180 min  |
| 50-100%   | 10%        | 12%              | 15%   | 10%        | 120 min       | 240 min  |
| >= 100%   | 12%        | 15%              | 20%   | 12%        | 180 min       | 360 min  |

- Phase 1: exit at `initial_sl` if the trade moves that far against the short.
- Phase 2: once in profit by `activation`, a trailing stop follows the best price at
  `trail` distance.
- Phase 3: after `tighten after`, the trail narrows to `tighten to`.
- Backstop: `max_hold` closes the position on a timer regardless of price.

## Known weaknesses (baseline, not yet fixed)

Documented so `v1` is honest, and tracked as hypotheses to test before changing prod:

- Losers tend to hit the full `initial_sl`; winners tend to close on the `max_hold`
  timer or give most of the move back on a wide trail (OBS-001).
- Entry confirmation is off while the score rewards near-peak price, so we can short a
  still-running pump (OBS-002).
- The 30% threshold and 3x leverage are heuristic, not measured (HYP-003, HYP-004).

## Execution notes

- Runs in DRY_RUN (paper) today; positions and outcomes recorded in `app.trades`, exit
  reason in `notes`, decision context in `app.trade_decisions`.
- Trade quality is visible in the Trade Journal (dollar P&L, ROE, exit reason,
  duration) with server-side aggregate stats.
