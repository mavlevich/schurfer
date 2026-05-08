# ADR-0004: Self-hosted GitHub Actions runner

Date: 2026-05-08
Status: Accepted

## Context

GitHub Free даёт 2000 Actions минут/мес для приватных репо.
При активной разработке (3-5 push/день × 10-15 мин CI) можно
улететь в лимит.

## Decision

**Self-hosted runner** на Hetzner Tokyo VPS.
Бесплатно, без лимита минут.

## Alternatives considered

- GitHub-hosted (платный апгрейд) - $4/мес минимум за Pro
- Forgejo Actions - отдельная админка, сложнее
- Mix (lint hosted, heavy self-hosted) - может пригодиться позже

## Consequences

- Pro: 0 рублей, без лимитов
- Pro: cache локально на runner - builds быстрее
- Con: сами поддерживаем runner, security настраиваем
- Revisit: если runner overhead станет больше выигрыша
