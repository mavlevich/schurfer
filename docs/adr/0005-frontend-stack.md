# ADR-0005: Frontend stack - React + Vite + Redux Toolkit

Date: 2026-05-08
Status: Accepted

## Context

Нужен web dashboard для приватного логина. Real-time данные
(цены, OI, funding) через WebSocket. Сложная structured UI
(charts, tables, forms).

## Decision

- **Vite** как bundler (не Next.js - SEO не нужен, это закрытый dashboard)
- **React 19** + **TypeScript**
- **React Router 7** (популярнее TanStack Router, тот же effort)
- **Redux Toolkit + RTK Query** для state и server state (одно решение)
- **shadcn/ui** на Radix для components (free, copy-paste)
- **Tailwind 4** для стилей
- **Lightweight Charts** (TradingView free) для графиков
- **TanStack Table** для таблиц с виртуализацией

## Rationale

Все компоненты:
- Бесплатные (MIT/Apache), никаких подписок
- Популярные на рынке (skill для CV)
- Покрывают 100% наших нужд без custom workaround'ов

Redux выбран над Zustand сознательно - больше boilerplate, но
гораздо более ценный skill на job market.

## Consequences

- Pro: всё стандартное, легко найти разработчиков, легко искать ответы
- Pro: RTK Query закрывает и REST, и WebSocket subscriptions
- Con: чуть больше кода чем с Zustand
- Revisit: не планируется в обозримом
