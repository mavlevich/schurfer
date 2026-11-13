# B2 entry replay pilot contract v1

Status: fixed design for a local entry-only pilot, 2026-11-13. The owner accepted
the review response and instructed continuation. Publish this contract and its
registry entry in main before the next native-data computation. It permits no
new download, strategy return, exit, funding payment, test-year or live action.

## Population and configurations

Use the accepted B1 event export, SHA-256
`4f1554108d6da503630d24d5b123d8000532b7bb3decb33f033e57dab65711f7`,
and only the ten original symbol-days in source plan SHA-256
`39c5bda6f438f51e90fbb899a2eb93250acf995727d032a6e725f5a011fe8630`.
The two ob200 format fixtures remain separate quality fixtures and do not enter
the pilot's strategy denominator. No source day is resampled after inspecting
fill statistics. Report all original eligible IDs and every unresolved reason.

Native files are the exact 24-file manifest SHA-256
`527b7446887d1f6c466c9891d9f1ef67134dc4b86aaf7a30d3f83c8ddc9bb0a0`.
The source-quality result is
`7cb1a8af1444fd890e3566b799991c18cfa2ac880457a788ef38bf8564732b67`.
Checksums, native identity, file boundaries and the discovery boundary are
mandatory. The original eight configurations stay: primary d30/USD50/1s arrival/
60s wait; one-axis d15, d10, 300s wait, zero queue ahead, USD25, USD100, 5s arrival.
The 300s wait is d30 only. Every pilot entry window is outside :55 through :04.

## Clocks, continuity and ordering

- At decision, choose the last generated public book with `ts <= decision`.
  Treat generation time as a zero-feed-delay availability proxy, explicitly;
  historical receive time is unavailable. Engine `cts` alone cannot authorize a
  decision-time observation. The source's `ts - cts` diagnostics stay separate.
- Use the book available by the modeled arrival for queue/depth estimation.
  This is a discretely observed queue model, not exact order-by-order evidence.
  Require a valid prior snapshot, native symbol and monotone system/engine clocks.
- Fix **2,000 ms** as the maximum book gap and quote staleness for the pilot.
  The source-only calibration had worst per-file p99 engine gap 1,804 ms and
  p99 generation lag at most 100 ms. The rounded threshold is fixed before fills,
  not selected by maximizing fill coverage. A larger gap intersecting the
  decision/arrival/wait path is `incomplete_book_data`, never silently dropped.
- Conflicting payloads with the same native `(u, seq)` are unresolved; identical
  duplicates are counted once. A source reset inside a waiting path is unresolved.
  Increasing IDs need not be adjacent merely because this sample happened to be.
- Trades exactly tied to arrival or cancellation time do not advance the modeled
  queue; report these ties. Missing internal paths stay unresolved even if a
  later snapshot looks normal.

## Price priority and partial execution

The virtual buy limit L is the decision's bid and never reprices. At arrival,
`L >= ask` is a post-only rejection. Join behind displayed quantity Q0 at L.
If L is absent but lies within observed bid depth, Q0 may be zero; if L is outside
visible depth, queue size is unknown, not fabricated as zero.

Only aggressive sells can advance this buy. Sells above L execute against better
bids and do not consume the queue at L. Sells at L advance it. Ordinary sells
below L are a separate trade-through case: in the volume-limited counterfactual
model they contribute their actual volume, without an automatic full fill.
Retain at-L and below-L contributions separately. Buys never fill this buy limit.
The model remains `min(q, max(0, V - Q0))`; cancellations do not advance Q0.
Keep partial fills, cancel the remainder at expiry, and never add taker completion.

A trade inside the preceding sampled spread can be legitimate between book
publications. A trade beyond an old quote can also reflect a moved book. The
requested inside-spread/opposite-quote breakdown is diagnostic, not a rule that
declares all such prints corrupt or fills our order from a price touch.

## Eligibility and native quantity

Normalize explicit boolean/0/1 flag representations, retaining their native form.
An explicit special flag excludes that print. Ordinary volume requires both BT
and RPI explicitly false; absence is unknown. The ten-day primary may therefore
have unidentified flow, because BT is absent throughout this source pilot.

Report the registered excluded-unknown-volume case separately from the sensitivity
that assumes unknown prints eligible. Never report the former's zero modeled
progress as observed zero real-world fill probability. The latter is a conditional
simulation, not identified API-maker execution or a strict mathematical upper bound.

USD quantities remain continuous research quantities. An observed minimum fill
may be a partial execution of a larger order, and minimum observed size differences
do not establish exchange qtyStep. Historical qtyStep, minOrderQty and minNotional
need historical specification evidence. Until then report native sizing as unknown;
do not certify that USD50 orders were admissible.

## Scope of exits and the reserved tape

The entry pilot does not lift the :55 through :04 tape reservation. Keep original
strategy exits rather than shifting them to :54 or :05 to fit a data mask.
Before B3, its exact pre/post-settlement exits and required windows must be frozen,
then a main-published amendment can release only those necessary 4h discovery
windows from the old seconds-study reservation. Test and blind boundaries stay.
No masked price/size parsing occurs under this entry contract.

The 724-day metadata inventory covers entries only. From the frozen event IDs,
236 unique forecasts settle at midnight; coverage of the settlement day alone
requires 46 additional symbol-days, giving at least 770 in the entry-plus-settlement
union. This is an offline timestamp count, not a fetch or a complete exit budget.
Native after-midnight trailers are retained in raw archives and can support a
later adjacent-file consistency check; duplicate/conflicting rows must be handled
explicitly. They do not replace the missing next-day source path.

## Deliverable and stopping boundary

Emit counts, source/clock/eligibility/sizing assumptions, partial/full/no modeled
fill, timeouts/rejections and unresolved reasons for every configuration. Show
original signal denominators, separating the conditional eligibility sensitivity.
No prices, PnL, favorable excursions, exit return or funding payment is emitted.
A small selected source pilot supports model calibration, not strategy promotion.

Before a larger corpus or B3, discuss whether uncertainty is informative enough
to continue. Do not make an unidentifiable primary look estimable by declaring
all missing flags false or dropping difficult events.
