# Trades/Decisions UI audit v1

Read-only audit of `apps/web`'s trade-facing pages (`/trades`, `/decisions`),
done 2026-08-24 while fixing the Source/Strategy column and net-accounting
display bugs on `/trades`. Findings only — no implementation here. The fixes
land no earlier than roadmap item 8 (`feat/trade-events-and-unified-
presentation-v1`), not as a standalone earlier PR.

## Findings

1. **"shadcn" here is not Radix.** `apps/web/package.json` has zero
   `@radix-ui/*` dependencies. The five existing primitives
   (`components/ui/{badge,button,card,input,table}.tsx`) are `cva` + `clsx`
   styling over plain HTML elements (`<span>`, `<button>`, `<table>`) — they
   look like shadcn but are not built on shadcn's actual accessible
   primitives. Adding a real `Select`/`Tabs`/`Collapsible` would be this
   repo's first genuine Radix adoption, worth doing once across every page
   that needs one rather than per-page.

2. **Native `<select>` is systemic, not `/trades`-specific.**
   `DecisionsPage.tsx:122` uses the identical unstyled-Radix `<select>`
   pattern as `TradesPage.tsx`. Any fix needs to cover both pages.

3. **`/trades`' table is too dense for one row.** 12 columns, `overflow-x-auto`
   horizontal scroll on typical viewports. `/decisions` has the same shape
   at 7 columns, less severe but the same pattern. Scrolling a wide table
   inside its own container is a legitimate shadcn pattern by itself — the
   actual problem is column count/density, not the scroll container.

4. **No `Tabs` (or segmented-control) component anywhere.** Nothing to
   build a Live/Paper/Research (or similar) top-level filter on top of.

5. **No `Collapsible`/`Accordion` component anywhere.** Nothing to hide
   secondary per-row detail (entry/exit price breakdown, exit reason,
   slippage, strategy version) behind an expand action.

6. **Strategy badge color/icon mapping is duplicated per page.**
   `TradesPage.tsx`'s `strategyBadgeStyle()` (added today) is local to that
   file; `DecisionsPage.tsx` independently derives its own Action/Reason
   badge styling. No shared `lib/` home for "map a strategy/action name to a
   color+icon" today.

7. **Summary/breakdown cards render unconditionally.** `/trades`' `StatRow`
   and the new by-strategy breakdown table both render regardless of
   whether any filter is applied — a first-time visitor sees a full
   multi-version breakdown before asking for one.

## Direction for the eventual fix (item 8)

- Add `@radix-ui/react-select`, `react-tabs`, `react-collapsible` (or
  `react-accordion`) in one PR, not one per page.
- Live / Paper / Research as top-level tabs, above the existing
  exchange/mode/side filters, on both `/trades` and `/decisions`.
- Row schema: keep ~5-6 always-visible columns (token, side, size, P&L,
  status, opened); move the rest behind a per-row expand.
- Move `StatRow`/the by-strategy breakdown behind their own tab or a
  collapsed-by-default section.
- One shared `lib/strategyBadge.ts`-style module for name→color/icon
  mapping, imported by both pages instead of each deriving its own.

Scope for the eventual PR: `TradesPage.tsx` + `DecisionsPage.tsx` together —
the pattern is shared, so the fix should be too.
