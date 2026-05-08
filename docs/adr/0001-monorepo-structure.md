# ADR-0001: Monorepo structure

Date: 2026-05-08
Status: Accepted

## Context

Нужна структура репо для multi-service trading платформы с
разными языками (Go, Python, TypeScript).

## Decision

**Monorepo** с разделением `apps/` (запускаемые сервисы) и
`packages/` (shared библиотеки).

## Alternatives considered

- Polyrepo — отдельные репо. Отброшено: cross-service refactoring
  становится болью, версии shared lib рассинхронизируются.
- Hybrid (public + private + shared) — обсуждалось когда планировался
  публичный продукт. Отброшено когда решили что продукт полностью
  приватный (см. ADR-0002).

## Consequences

- Pro: атомарные изменения через несколько сервисов, единый CI
- Con: репо растёт, нужен CI который умеет частичные builds
- Revisit: если репо превысит 1GB или появится команда >5 человек
