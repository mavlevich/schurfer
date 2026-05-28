# ADR-0007: Trade Journal as core layer, not a feature

Date: 2026-05-08
Status: Accepted

## Context

Core requirement: "need clear winrate and logs by account, exchange,
strategy. To improve algorithms and ideas."

## Decision

**Trade Journal is the first service we build, before any strategies.**

Every system action (signal generated, alert sent, trade opened,
trade closed, funding paid, etc.) is recorded in the journal with
full context (`setup_context` JSONB with features that influenced
the decision).

## Consequences

- Every strategy must be instrumented from day one
- Enables SQL queries like:
  "winrate when funding > 0.05% AND OI growth > 100%"
- Ready foundation for tax export
- Ready foundation for backtest validation
- Cannot deploy a strategy that doesn't write to the journal

## Schema highlights

See `packages/journal/` for actual implementation.
Key fields:
- strategy_id + strategy_version
- setup_context (JSONB) - all features
- entry/exit prices, slippage, funding, fees
- outcome_label (win/loss/breakeven)
- outcome_quality (planned/lucky/mistake/force_majeure)
