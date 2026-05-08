# ADR-0003: Go workspaces для backend сервисов

Date: 2026-05-08
Status: Accepted

## Context

Несколько Go сервисов в одном репо: collectors, execution, api-gateway.
Нужна модель управления Go модулями.

## Decision

**Go workspaces** (`go.work`). Каждый сервис — отдельный module.

## Alternatives considered

- Один go.mod на весь репо — проще, но конфликты зависимостей
  между сервисами. Сложно вытащить отдельный сервис в свой репо.

## Consequences

- Pro: изоляция зависимостей, модулярность
- Pro: каждый сервис может быть выпущен отдельно
- Con: чуть больше boilerplate (go.mod в каждой папке)
