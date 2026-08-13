# ADR-0005: Frontend stack - React + Vite + Redux Toolkit

Date: 2026-05-08
Status: Superseded

Supersession note: React and Vite remain, but the Redux Toolkit decision was not
implemented. The current application uses TanStack Query and local React state; the
reviewed direction is documented in
[`docs/architecture/web-ui-evolution-v1.md`](../architecture/web-ui-evolution-v1.md).
The original decision below is retained unchanged as history.

## Context

Need a web dashboard behind private login. Real-time data
(prices, OI, funding) via WebSocket. Complex structured UI
(charts, tables, forms).

## Decision

- **Vite** as bundler (not Next.js - no SEO needed, it's a private dashboard)
- **React 19** + **TypeScript**
- **React Router 7** (more popular than TanStack Router, same effort)
- **Redux Toolkit + RTK Query** for state and server state (single solution)
- **shadcn/ui** on Radix for components (free, copy-paste)
- **Tailwind 4** for styles
- **Lightweight Charts** (TradingView free) for charts
- **TanStack Table** for tables with virtualization

## Rationale

All components:

- Free (MIT/Apache), no subscriptions
- Popular in the market (skill for CV)
- Cover 100% of our needs without custom workarounds

Redux was chosen over Zustand deliberately - more boilerplate, but
a much more valuable skill on the job market.

## Consequences

- Pro: all standard, easy to find developers, easy to search for answers
- Pro: RTK Query covers both REST and WebSocket subscriptions
- Con: slightly more code than with Zustand
- Revisit: not planned
