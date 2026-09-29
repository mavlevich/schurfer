# MEXC pre-move data feasibility v1

Status: outcome-blind inventory, 2026-09-29. This is not a hypothesis registration,
an economic result, or permission to start a feed or trade. HYP-029's registered
5-minute price trigger failed; this audit does not reopen or retune it.

## Decision

Can Schurfer test a signal **before** a visible MEXC price move, using trades,
open interest (OI), funding, or order-book pressure, with a route executable on
Bybit? The required historical unit is every eligible source-instrument minute,
including quiet minutes and instruments that never pump. Conditioning the dataset
on scanner events would select on the later outcome.

**Finding:** the existing historical archive supports only MEXC price and total
turnover at one-minute resolution. It does not contain public trade direction,
point-in-time OI, pre-settlement funding forecasts, or depth. The public MEXC
endpoints inspected below expose recent trades and current OI/depth, but not a
documented way to reconstruct those missing historical states over the archived
window. A historical test of a buy-pressure/OI/depth precursor cannot be built
from this archive. Bybit execution identity for arbitrary MEXC instruments is
also unapproved. This is a **data feasibility result**, not evidence that a
precursor has no predictive value.

## Boundary and evidence

- Reviewed repository code at `e7a3562a98f2e68553da3f9e89a63edd183bff51`
  (merged main). No production database, private account, prices, returns, or
  records on or after 2026-09-29T00:00:00Z were queried. Public exchange API
  documentation was inspected; live market endpoints were not called, because
  the HYP-012 v2 blind window has begun.
- The local `runtime/research/mexc_klines/Min1/manifest.json` pins
  `[2026-08-28T00:00:00Z, 2026-09-29T00:00:00Z)`. Its SHA-256 at this audit was
  `0ba65483cb573b9d41e79457ba943ad20d39419c6a5104f6d8cddd4289871543`.
  It lists 1,070 USDT perpetual symbols, 44,876,691 rows and no zero-row
  symbols; all 1,070 compressed files matched the manifest's SHA-256. These
  are **not** continuity or point-in-time listing proofs: archive `complete`
  means only that returned bars fell inside the requested window. The symbol
  list came from the exchange catalogue when the archive ran, so it is not a
  historical universe or evidence that each symbol traded for all 32 days.
- `mexc_kline_archive.py` stores `t,o,h,l,c,v,a` from the public kline API.
  It does not store individual deals, OI, funding, quotes, order-book changes,
  receive timestamps, or a historical instrument catalogue. It refuses an
  archive window ending after the v2 blind boundary.

## Source capability by prospective feature

| Candidate input                         | What the public source documents                                                                                                                                   | What Schurfer retained before 29 September                                                                                                                                           | Historical precursor status                                                                                                                                                                                                         |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| One-minute price and total turnover     | Kline response has OHLC, volume and amount; requests are capped at 2,000 bars.                                                                                     | The MEXC Min1 archive above.                                                                                                                                                         | Usable for a **separate** bar-level question after continuity and historical-universe checks. It cannot reveal buy versus sell pressure within the minute; HYP-029 already failed for its registered bar rule.                      |
| Individual deals and purchase/sell flag | REST `deals/{symbol}` returns at most the latest 100 deals, with no documented time cursor. WebSocket `push.deal` streams new deals with a native trade timestamp. | No MEXC deal tape. Pump snapshots start after selection and cannot fill quiet-minute controls.                                                                                       | Historical directional-flow test unavailable. The documented `T=1` purchase / `T=2` sell must not be called **aggressor side** without a separate semantic check.                                                                   |
| OI                                      | The ticker gives current `holdVol` (documented as total holdings) and a timestamp. The inspected public specification does not document historical OI pagination.  | `app.oi_snapshots` is fetched for detected/tracked pumps, not for the full source universe. The post-window derivatives resolver supports MEXC funding history, not MEXC OI history. | No complete pre-move or quiet-minute OI history. The units and semantics of `holdVol` need native verification before treating it as OI or converting it to USD; any conversion also needs contemporaneous contract size and price. |
| Funding                                 | Current rate and next settlement are public; funding history is a series of settled rates.                                                                         | `app.funding_rate_snapshots` is also selected by detected/tracked pumps.                                                                                                             | A later settled rate is not the rate a trader knew before a move. No full-universe point-in-time current-rate series is retained.                                                                                                   |
| Depth / imbalance                       | REST depth and depth commits describe current/recent book versions; WebSocket depth can be maintained prospectively with sequence checks.                          | No MEXC pre-event depth snapshots or update stream.                                                                                                                                  | Historical pre-move imbalance unavailable. A future collector would need native event time, receive time, version-gap detection and raw provenance.                                                                                 |
| Tradable target identity                | Bybit v4 qualification approves routes for the Gate-source registry.                                                                                               | HYP-029 used a Bybit market-status check and a 2x price-level band for historical research; it did not prove contract-address identity for arbitrary MEXC symbols.                   | No general MEXC-to-Bybit approved execution registry. Exact identity and status at signal time are required before any money-path claim.                                                                                            |

The source documents used here are the [MEXC contract API market endpoints](https://mexcdevelop.github.io/apidocs/contract_v1_en/),
its [public WebSocket channels](https://mexcdevelop.github.io/apidocs/contract_v1_en/),
and MEXC's [2026 futures API domain change](https://www.mexc.com/announcements/article/futures-api-access-domain-update-17827791532974).
The older API examples use `contract.mexc.com`; the announced current REST
domain is `api.mexc.com`. These are documentation claims, **not** a successful
live API probe or a measured retention guarantee. The source code inspected
was `mexc_kline_archive.py`, `main.py`, `persistence.py`,
`derivatives_context_resolver.py`, `mexc_early_trigger_hyp029.py`, and the v4
source-lead identity registry. The existing
`execution-venue-matrix-v1.md` does not confirm MEXC account/product access.

## Gate for another research PR

1. Use the registered v2 latency report **if** it reaches its 30-attempt
   decision floor. It measures the pipeline after first observation for v2's
   qualified Gate-source routes; it cannot establish MEXC's source-move-to-
   detection delay. If no decision is locked by the 2026-10-31 reassessment,
   record this diagnostic as unavailable and decide whether a new data canary
   merits its own budget there. Neither zero attempts nor the Gate-route
   latency is evidence about MEXC's unknown detection delay. The v2 book-cost
   diagnostic may first be read on 2026-10-15 and cannot change v2's model.
2. A prospective pre-move data canary is a **new research-line decision**, not
   a continuation or rescue of HYP-029. Before building it, update the
   `ROADMAP.md` research slot and register **one bounded data canary**: one
   source venue, an explicit instrument selection independent of later pumps,
   a fixed time range and hard storage and request budgets. Retain native
   payload plus event/receive timestamps,
   OI units, trade-side semantics, gap reasons and full eligible denominator.
   Check disk headroom and the restore path first. Capture the prebuffer for
   quiet periods as well as pump periods. The canary first measures data
   feasibility, not returns. Define the source-move timestamp `M` in that
   protocol, then measure `S - M` from its newly recorded native trades;
   `S - M` is a canary measurement, not a prerequisite for the canary. Do not
   infer an historical tape from 1-minute OHLCV or current REST snapshots.
3. Audit exact MEXC-to-Bybit asset/contract identity and Bybit book capacity
   at decision time, or separately establish the owner's MEXC execution access.
   A ticker and 2x price band can screen historical routes but cannot authorize
   orders. Freeze any feature, entry rule, costs, baseline and future cohort
   **before** reading its outcomes. The four money gates in `ROADMAP.md` still
   apply.

No feed, database migration, formal reader or execution setting changes in this
audit. If a bounded canary and an executable route cannot be justified at the
reassessment within data and server budgets, stop this precursor line and
compare a bounded spot/perpetual carry feasibility check instead.
