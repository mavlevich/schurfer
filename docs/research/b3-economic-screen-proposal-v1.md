# B3a: fixed taker economic baseline (proposal, 2026-11-13)

**Status: design accepted by the owner on 2026-11-13 ("ну значит го го го");
implementation awaits code review and publication in main before native reads.**
The acceptance follows the disclosed review response, including the limited
budget interpretation of 15 bps. Revised after colleague review: staged taker/maker reads, a funding-inclusive
budget stop, portfolio diagnostics, and a BTC-only first fetch. No new market
data or strategy return was read to prepare this proposal.
It follows the [B2 pilot](b2-entry-pilot-result-2026-11-13.md), merged as
`596d245` (#592). Publish the accepted contract and seen-windows registration
in main before fetching or reading outcomes. This proposal does not authorize
the 75.79 GB full book/tape scope, an exam, production changes or orders.

## Question and scope

Can a fixed minute-resolution taker baseline on B1's decision-time signals
justify further investment in track B? This is a separate policy from B2's
one-second maker arrival. A negative baseline does not prove the maker policy
loses; a positive baseline does not validate its fills. Do not obtain maker PnL
by changing a fee on the taker sample.

Freeze B1's complete d30 event export, SHA-256
`4f1554108d6da503630d24d5b123d8000532b7bb3decb33f033e57dab65711f7`:
1,637 events, 150 symbols, 718 forecast-settlement symbol-day clusters.
All are discovery-year Bybit USDT perpetuals. Preserve the original forecast
and decision even if the actual schedule later changes. No d15/d10 variants,
new filters or parameter search are proposed. The test year, backward holdout
and HYP-012 v2 blind window remain closed.

Selection history applies to B3a itself: 4h was selected after exploratory
returns were seen. The recorded screen reported +55.3 bps gross for 4h and
-2.0 bps net after removing its top five symbols, on a different hourly-implied
signal population; the older +35.5 bps pre-settlement diagnostic used future
settled-rate selection. Neither is an estimate for these 1,637 events. The
+20 bps nomination hurdle is inherited, not independent of that research
history. At least 124 exploratory configurations and eight B2 model
configurations precede this read. Discovery is already exposed.

## Two selectable policies, fixed before outcomes

Let t be B1's forecast settlement, and d = t minus 30 minutes.

| Item   | Before settlement                             | Through settlement      |
| ------ | --------------------------------------------- | ----------------------- |
| Signal | Frozen B1 d30 event                           | Same                    |
| Entry  | Open of native one-minute bar at d + 1 minute | Same                    |
| Exit   | Open at t minus 1 minute                      | Open at t plus 1 minute |
| Size   | USD 50 continuous notional                    | Same                    |
| Costs  | 30 bps round trip; 50 bps stress              | Same                    |

Bar opens are price proxies with a fixed execution-cost assumption, not
observed executable quotes. This screen cannot establish native sizing,
capacity, actual fees or a live fill. A zero-volume entry/exit bar is unresolved.
There is no approximation using hourly closes. The one-minute delay provides
a fixed entry time at native bar resolution after the decision, without
assuming an instantaneous fill at a bar already being observed. It is an
implementation convention, not proof that waiting is economically conservative
or a substitute for B2's one-second maker entry.

Gross bps = 10,000 times (exit / entry minus 1). Subtract the stated round-trip
cost and add funding for the held quantity. Use actual settled rates and
settlement times from the pinned B1 index, never future rates as an entry filter.
Missing funding evidence is unresolved, not zero. Count every settlement
strictly inside the holding interval, including changed schedules.

Funding valuation is explicitly approximate: USD quantity = 50 / entry price;
cash proxy per settlement = minus quantity times settlement-minute mark open
times the settled rate. Show the mark-bar low/high valuation sensitivity and
require any nomination to survive its less favorable value. This sensitivity
is not proof of the exchange's exact assessment price. API fields and funding semantics were checked against primary documentation
on 2026-11-13; sources are listed below. The minute mark remains a proxy.
Neither exit is within the documented five-second settlement uncertainty zone.

## Inputs, budget and integrity

Fetch native perpetual-price and mark-price one-minute bars only for
[t minus 30 minutes, t plus 2 minutes), 32 bars per event window. Preserve raw
responses and hashes before normalization. Bind venue, market type, native
instrument, endpoint, interval, start/end and retrieval version. Conflicting
duplicates, wrong identity, out-of-window rows or corrupt receipts fail closed.
An absent bar, fetch error or incomplete path is counted as unresolved; it
never silently removes the signal. Resolve identity through the existing
canonical route layer; do not fabricate symbols.

The [metadata-only budget](evidence/b3-economic-screen-proposal-2026-11-13/scope-budget.json)
was computed solely from frozen event IDs/times, after checking the export hash:

- 1,637 event-price requests plus 1,637 event-mark requests.
- 926 distinct forecast times; at most 926 BTC price requests.
- Total upper bound: 4,200 request keys before cross-role deduplication;
  at 3 requests/second this is 23 minutes 20 seconds of request pacing,
  excluding network overhead and retries.
- No liquid-pool median requests in B3a. The earlier upper bound of 19,446
  extra requests is retained as superseded planning, not a fetched dataset.
- Proposed hard wire cap: 256 MiB, at most two attempts per key, retries charged
  to the same persisted byte budget. Stop on exhaustion; no automatic increase.

Freeze the exact BTC and event request keys before fetching outcomes. Missing
BTC bars remain explicit gaps, not replacement picks. The funding index must
demonstrate coverage through each position's exit; failure is unresolved.
The liquid-pool median becomes a non-gating, deferred diagnostic, not a second
market-adjustment requirement. It has no data fetch or result in this stage;
any later diagnostic needs its own point-in-time pool and registry/budget.

### One tape-mask policy for track B, proposed before B3a

B3a reads price/mark klines, not native trade tape. Its return exposure must be
registered separately. For the later frozen maker stage, propose releasing
only [t minus 5 minutes, t plus 2 minutes) of reserved tape on each of the
same 1,637 d30 events' instrument/forecast-settlement pairs. Deduplicate these
windows and clip them to discovery bounds. This covers the two fixed exits;
all other reserved :55 through :04 minutes stay closed. This is a proposed
release, not permission to inspect those trades now. Before any such read,
publish exact interval keys, their hash, source scope, exposure and budget in
main under the stage go; include next-day files where necessary. No expansion
to every 4h settlement or other instrument follows from this proposal.

d30 entry plus settlement dates cover 751 symbol-days, including 44 settlement
dates absent from its 707 entry days. That is not permission to download
full-day archives. This one policy replaces ad hoc decisions about each exit;
if necessary source coverage cannot be obtained, mark the maker stage blocked
rather than moving an exit after seeing B3a.

## Estimation and decision

Average events within the original (symbol, forecast-settlement UTC date)
cluster, then give clusters equal weight. Retain incomplete clusters in the
coverage denominator. Resample whole UTC ISO weeks together, 20,000 draws with
fixed seed 3103; include every calendar ISO week touched by discovery, including
weeks without events. Use paired week draws for both exits and report circular
two-week blocks, truncated to the same calendar length, as sensitivity. A cluster
is complete only if all its original events are resolved; do not average just
the surviving members. A bootstrap with fewer than 95% defined draws is
unestimable. No assumption that simultaneous altcoin moves are independent.

This proposes a bounded discovery read without a new 50 bps planning-MDE gate.
Report uncertainty and detectable effects; the read can nominate, never confirm.
It does not reopen or waive HYP-037's registered stop. The program amendment
must be accepted before outcomes, rather than chosen after an attractive result.

For a laboratory nomination require all of:

1. At least 95% complete events, 150 complete clusters, 40 symbols, 26 weeks,
   and top-five-symbol share no greater than 30%.
2. Cluster mean net at 30 bps at least +20 bps, with the one-sided 97.5% week
   bootstrap lower bound above zero (two selectable exits, Bonferroni alpha
   0.05 / 2). Report 50 bps net alongside it. These are discovery screens,
   not claims to have corrected the whole earlier search history.
3. Net positive in each calendar half, and after separate removals of the five
   largest net-contributing symbols and five weeks. Recompute weights each time.
4. Market-adjusted net positive against BTC over identical entry/exit times.
   This is a common-market check, not evidence for a tradable BTC hedge.
5. Complete BTC/funding coverage at least 95%; positive net survives the
   stated funding-valuation sensitivity. Portfolio and liquid-pool median
   diagnostics do not gate event-level nomination.

A negative complete-case mean with at least 150 resolved clusters and 95%
event completeness is `fail` before diversity checks. Otherwise report
`insufficient_data`, `not_nominated` or `nominated_for_protocol`, with every
failed condition. Positive discovery never permits orders or opens an exam.
If neither exit qualifies, stop the taker baseline without searching new exits.
That alone neither proves nor refutes maker profitability.

### Proposed budget decision for the whole road

For each exit also report the upper 97.5th percentile of the same week-block
bootstrap mean of **price return plus funding, before execution costs**. Use
the more favorable of the registered mark low/high valuation sensitivities
for this upper diagnostic. Require at least 95% complete events and 150
resolved clusters; otherwise this branch is `budget_not_assessable`, not a
negative conclusion.

If both exits' upper diagnostics are strictly below 15 bps, set
`track_b_paused_budget`: do not commission the full maker archive or open the
exam. No automatic new candidate or parameter search follows. The proposed
15 bps is a research-spending hurdle motivated by the review's illustrative
maker-entry/taker-exit cost scenario. It is not a measured cost floor or an
upper confidence bound on the maker strategy's EV. If either diagnostic is
at least 15 bps, set `maker_source_review_needed`, not `maker_pass`; resolve
eligibility, sizing and the separate native-data budget before continuation.

Why this distinction matters: maker fills have different prices and timing
and select a subset of signals. For synthetic +100/-100 bps events, the
all-event mean is zero, while a maker filling only the +100 case can have
positive all-opportunity PnL even counting the unfilled case as zero. Evidence
of adverse selection elsewhere does not establish a bound here. Price gross
alone also omits funding at the through-settlement exit. Do not call the
15 bps rule proof that the maker version loses or that the family is closed.

Both policies also get a USD 50 cash-funded, one-slot feasibility report:
fixed USD 45 entry notional, no reinvestment or borrowing, cost/funding reserve
of USD 5 within initial cash USD 50, one position at a time. Event-level bps
use the same linear price/cost model; this smaller portfolio size is explicit. Simultaneous signals
are ordered by SHA-256 of `b3a-v1|symbol|decision_ms`; an exit at the same time
is processed before a new entry. Preserve capital, slot and missing-data skips.
If an accepted position has an unresolved path, subsequent portfolio value is
unknown until resolved, not a fictitious cash balance. Report daily PnL,
occupancy, drawdown, losing streak and all skips. This accounting model does
not establish futures margin safety or order minima. Its event mean remains
all 1,637 signals, not the slot-selected subset. Portfolio net is a mandatory
reported diagnostic, not a nomination gate: a hash-selected one-slot subset
cannot veto event-level evidence. A portfolio unable to trade profitably on
the available capital still blocks any deployment until that limitation is
resolved in a separately frozen operational design.

## Review and next stages

### Staged-read amendment and maker freeze

The program originally read taker and maker together. This proposal explicitly
changes that order: B3a reads taker first; B3b may read maker later. Maker
outcomes are no longer outcome-blind once B3a is read; the earlier prices and
selection history remain disclosed. Before B3a freeze the maker event list to
these exact 1,637 d30 events and the exits to t minus 1 minute and t plus 1
minute. Freeze entry to B2's primary: USD 50 continuous notional, decision bid,
1s arrival, 60s wait, no reprice and no taker fill of the remainder. Keep the
original full opportunity denominator, with partial fills, rejections,
missed signals and unknown paths separately accounted for.

The strict primary still excludes unknown-class prints; unknown eligibility
cannot become ordinary volume after seeing prices. The all-unknown-eligible
sensitivity remains conditional, not a selectable strategy. Other B2 variants
are not new selectable maker candidates. Do not tune events, entry, wait,
queue, eligibility or exits using B3a. If historical lot rules, trade classes
or source coverage cannot support the frozen model, report it blocked or
unidentified. Any alternative is a new exposed-discovery registration, not
completion of this frozen maker stage.

The changed items needing targeted colleague review are the funding-inclusive
budget stop and its limited meaning, staged maker freeze, BTC-only scope,
non-gating portfolio, and narrow future tape release. The owner accepted this design after the response. One focused code review
remains before merge; no full re-review of B1/B2 is requested.

After acceptance: freeze exact scopes, implement with boundary/regression tests,
publish contract/code/registry in main, then fetch and read from a clean revision
under the owner's stage go. Record both policies together. No future result
may steer which of these two is read first.

Any B4 candidate needs a separately frozen exam contract, with the full prior
selection history (including at least 124 exploratory configurations and B2's
eight model configurations), a single frozen test-year batch, and uncertainty
appropriate to that claim. Before maker economics, justify ordinary-flow
eligibility, lot constraints, source gaps, both exit paths and adverse selection.
No new venue, ML, production deployment or live order is part of this proposal.

## Frozen implementation and run sequence

The exact native request list is
[evidence](evidence/b3-economic-screen-proposal-2026-11-13/requests.json.gz),
SHA-256 `c199994e706ae956ed454682a757c867fab187581a233c7d3d5b3897bdb56213`.
It contains 4,200 endpoint/instrument/start/end identities and no market values.
Original B1 native symbols are reused directly; BTCUSDT is the fixed native
benchmark. No symbol is reconstructed from a base name or current listing.

Funding coverage is established from successful native funding pages in the
pinned index, with all truncated-page tails and intervening gaps accounted for.
The absence of a row without proven query coverage is never zero funding.
Zero-volume execution bars are unresolved; mark bars have no volume field.

The one-slot report uses a fixed USD 45 notional and USD 5 reserve for each
entry, so it refuses a new position if cash has fallen below USD 50. That
conservative restriction is explicit in capital-skip counts. Drawdown and daily
PnL are realized-only diagnostics; neither estimates intrabar liquidation risk.

See [runbook](b3-economic-screen-runbook-v1.md). Before opening a connection,
the CLI requires a clean revision included in origin/main, exact publication
of contract/registry/scope/code, and an immutable local run binding. Every
request and each 1 KiB body-read reservation are charged durably before I/O,
including retries and EOF probes. These conservative reservations bound exposed
HTTP response bodies, not TLS/HTTP headers or kernel buffers. All 4,200 keys
use the shared transport's `pilot` phase name; this is the entire frozen scope,
not an extra sampled pilot or another selection gate. The shared 95% transport
summary never substitutes for the economic report's event/cluster denominators.

Native invalid responses and conflicting values between retries stop sealing;
there is no last-response-wins merge. Gaps and exhausted fetch errors
remain unresolved opportunities. Retry limits, byte budgets, source hashes and
revision cannot change on resume. Sealing retains native bytes and receipt
hashes; reporting re-verifies the durable receipts and the sealed SHA.
No report is run until main publication and the authorized stage invocation.

Primary references checked for semantics, not historical execution evidence:
[Bybit native klines](https://bybit-exchange.github.io/docs/v5/market/kline),
[mark klines](https://bybit-exchange.github.io/docs/v5/market/mark-kline),
and [funding calculation](https://www.bybit.com/en/help-center/article/Funding-fee-calculation).
Documentation examples are illustrative, not new strategy inputs.
