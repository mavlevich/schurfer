---
name: schurfer-execution-safety
description: Implement or review Schurfer strategy execution, paper/live brokers, episodes, orders, position lifecycle, accounting, and trading-mode changes. Use when a change can open, close, size, route, reconcile, or report a trade; not for analytics-only hypothesis work.
---

# Schurfer Execution Safety

Read `AI_RULES.md`, then trace the signal through route resolution, intent,
broker, episode/claim, journal, exchange/CCXT, Redis, accounting, notification,
API, and UI boundaries that the change touches.

## Preserve the execution contract

- Carry an `ExecutionInstrument`/canonical route across boundaries. Keep
  native market ID, CCXT symbol, base, quote, settle currency, venue, and
  market type distinct.
- Construct an `ExecutionIntent` with normalized `StrategyIdentity`, side,
  notional, leverage, episode ID, and deterministic idempotency key. Strategies
  do not call paper or live order functions directly.
- Resolve `TradingMode` at startup under the global safety ceiling. Disabled
  strategies perform no claims or durable side effects. Unimplemented modes
  and unsafe escalation fail configuration validation.
- Persist the intent/episode before an external order. Atomically create or
  reclaim claims; never check then insert. Retrying the same intent must
  return/reconcile the same trade/order rather than create another.
- A worker restart must be able to reconcile DB intent, Redis accelerator,
  exchange order/position, and terminal episode state without operator guesswork.

## Keep live execution fail closed

Before making a live mode reachable, require and test:

- deterministic `clientOrderId` and durable order-attempt state;
- exchange response persistence and reconciliation of timeout/unknown result;
- bounded retry rules that distinguish query retry from order resubmission;
- per-strategy and portfolio exposure budgets, max concurrent positions,
  daily loss/circuit breaker, and a kill switch;
- verified market metadata, precision, min size/notional, reduce-only close,
  and one-way/hedge-mode assumptions;
- startup recovery before new entries are accepted.

Never silently fall back from missing identity, market quality, durability, or
reconciliation to placing an order.

## Make paper evidence honest

- Snapshot the executable ask side for long entries and bid side for long
  exits; reverse the direction for shorts. Record filled notional and partial
  depth, not just top-of-book price.
- Compute gross and net PnL with the correct side sign. Record fees, funding,
  entry/exit slippage, accounting version/status/error, and the actual terminal
  reason. Do not label incomplete evidence as net-complete.
- Persist strategy/version and source separately. Notifications and UI consume
  normalized journal fields rather than reparsing setup JSON.

## Required tests

Include unit and, where state is involved, real-PostgreSQL integration tests
for duplicate delivery, concurrent claims, restart between DB/exchange/Redis
steps, stale claim ownership, timeout after possible order acceptance, partial
fill, long/short PnL direction, missing liquidity, and legacy rows without the
new identity. Run focused execution/journal tests, Ruff, mypy, migration
upgrade checks, and the full execution suite before declaring merge-ready.
