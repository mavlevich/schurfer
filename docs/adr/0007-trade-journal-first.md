# ADR-0007: Trade Journal как core слой, не feature

Date: 2026-05-08
Status: Accepted

## Context

Главное требование от пользователя: "нужен чёткий винрейт и логи
по аккаунтам, биржам, стратегиям. Чтобы улучшать алгоритмы и идеи".

## Decision

**Trade Journal — первый сервис который мы строим, до любых стратегий.**

Каждое действие системы (signal generated, alert sent, trade opened,
trade closed, funding paid, etc.) записывается в journal с полным
контекстом (`setup_context` JSONB с features которые повлияли
на decision).

## Consequences

- Каждая стратегия должна быть instrumented с первого дня
- Можно делать SQL-запросы типа:
  "winrate когда funding > 0.05% AND OI growth > 100%"
- Готовая основа для tax export
- Готовая основа для backtest validation
- Нельзя deploy стратегию которая не пишет в journal

## Schema highlights

См. `packages/journal/` для actual implementation.
Ключевые поля:
- strategy_id + strategy_version
- setup_context (JSONB) — все features
- entry/exit prices, slippage, funding, fees
- outcome_label (win/loss/breakeven)
- outcome_quality (planned/lucky/mistake/force_majeure)
