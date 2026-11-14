# HYP-036 economic pilot, contract v1 (2026-11-14)

Study identifier: `hyp036-econ-v1`.

Status: **contract reviewed by the colleague on 2026-11-14, with their three text
fixes applied; it registers nothing yet, fetches nothing and reads no outcome.** It
follows the [bounded power audit](hyp036-power-audit-result-2026-11-14.md) (merged
#595). The colleague's review of that audit proposed the prices, the budget and the
role of this read. The read still needs three things in this order:

1. code and a registry row in main;
2. a frozen request list;
3. the owner's go.

## Question and role

The question: is the economics of HYP-036's short good enough to justify buying the
rest of the discovery tape (about 520 GB)? This is a **spending decision, not a test**.

- **Too small to confirm.** Four ISO weeks and 784 events give a planning MDE of about
  69 bps at this size, so the read cannot confirm a strategy.
- **What it can do.** It can give a reason to stop spending, or leave the line open for a
  full discovery protocol.
- **What a negative result does not mean.** A negative mean here is not proof that no
  effect exists anywhere in the absorbed-flow family.

What it is not:

- not a reading of HYP-036 v1, which stays stopped at its pilot gate;
- not an exam;
- not a new strategy registration;
- not permission for orders.

## Sample, fixed and unchanged

- **Events.** The frozen pilot events `pairs.json`, SHA-256
  `338b6e3811bbeb95312e4bb17dd0ff5b1f6b8acfbd3dff8cd7a090fe07a5cba4`, from
  `counts.json` `db24fde0...`.
- **Size.** 784 events, 750 symbol-day clusters, 194 symbols, 393 distinct decision
  hours, four ISO weeks.
- **Decision blocks.** 2025-01-13 to 2025-01-26 and 2025-07-07 to 2025-07-20, inclusive.
- **Controls.** Their identities in that file are ignored: nothing here selects,
  weights or reads them.
- **Nothing is added or dropped.** No event is added, dropped, reranked or filtered,
  and the trigger is HYP-036 v1's unchanged.
- **One read only.** The read happens once.

Everything is in the discovery year, before the HYP-012 v2 blind window. No backward
holdout input and no HYP-031 test-year input is accepted.

## Trade definition, from the HYP-036 protocol

As fixed in [bybit-absorbed-buying-v1.md](bybit-absorbed-buying-v1.md), "Entry" to
"Costs":

- **Entry:** short at the open of the native 1-minute perp bar that starts at t + 1 min.
- **Exit:** the close of the native 1-minute perp bar that ends at t + 1 min + 4 h.
  H = 4 h.
- **Funding:**
  - Settlements come from the pinned discovery funding index (SHA-256
    `f48fe09f7023a476e524cf70c6ec1b629eee30a9a07cff41c9d149dfb59e0cf5`, the index B3a
    used).
  - For each settlement s with entry < s < exit, the short's funding return is
    +r_s x mark(s) / entry price. Here mark(s) is the open of the 1-minute mark-price bar
    that starts at s (HYP-032's rule, the B3a convention).
  - The mark bar's low and high are reported as a valuation sensitivity.
- **Costs:**
  - 30 bps round trip is primary; 50 bps is a sensitivity.
  - This is a cost model, not a measured execution price.
  - The break-even round-trip cost is reported.
- **Units and formulas.** Prices and funding are fractions; results are bps.
  - funding_fraction = the sum over settlements s inside the hold of
    r_s x mark(s) / entry. It is zero when the hold has no settlement, and only after
    coverage is proven (see "Inputs and budget").
  - net30_bps = 10,000 x ((entry - exit) / entry + funding_fraction) - 30, and net50_bps
    the same with 50.
  - BTC diagnostic: btc_adjusted_bps = net30_bps + btc_price_return_bps, where
    btc_price_return_bps = 10,000 x (btc_exit - btc_entry) / btc_entry over the same
    entry and exit bars. The BTC leg has no fee and no funding: it is a common-market
    adjustment, not an imaginary position.
- **Weights:**
  - Events are averaged within their symbol-day cluster.
  - Clusters get equal weight, as in the audit.
  - Means use fully resolved clusters only. A missing event is never filled with zero.

**Completeness of net.** It depends only on the instrument, its execution and its
funding. An event is `incomplete`, counted by reason and kept in the coverage
denominator, when it lacks any of:

- its entry bar or its exit bar;
- non-zero volume on the entry and exit bars;
- a mark bar at a settlement inside its hold;
- proven funding coverage of its hold.

A cluster is complete only when all its events are complete.

**Completeness of the BTC diagnostic** is a separate counter. A missing BTCUSDT bar
removes that event from the BTC diagnostic only. It never makes the net incomplete
and never triggers `not_assessable`.

No price is substituted from another bar, venue or proxy.

## Inputs and budget

The coverage check below was outcome-blind. It ran on 2026-11-14 over event identities
and funding settlement **timestamps** only; no rate, price or return was read:

- The pinned index holds funding history for all 194 pilot symbols and for BTCUSDT,
  with no fetch errors for them.
- **Coverage is proven by pagination, not by gaps between settlements.** A missed
  hourly settlement can leave a gap of only two hours, so a gap rule proves nothing.
  - **What a successful page covers.** Only pages with `retCode` 0 and rows of the
    right symbol count.
    - If the page is not full (fewer rows than its `limit`), it covers its requested
      [startTime, endTime].
    - If the page is full, it covers only from its oldest returned settlement to its
      requested endTime.
  - **When a hold is covered.** The hold [entry, exit] must lie inside one contiguous
    union of those spans for its symbol.
  - **What follows.** A hold not covered this way is `incomplete` (funding coverage),
    never zero funding. A covered hold with no settlement inside it gets zero funding.
  - **Result.** By this rule, 0 of the 784 holds are uncovered. The colleague
    checked this independently, and the reviewer repeated it.
- **Settlements strictly inside a hold:**
  - none for 238 events;
  - one for 498;
  - two for 13;
  - four for 35.

  So 546 events need mark bars.

- **Status today:** 168 of the 194 symbols are `Trading` and 26 are `Closed`.
  - The same V5 endpoint served the hourly klines of the closed ones for the features.
  - That 1-minute bars exist for them is not verified. A gap makes the event
    incomplete; it is not a reason to change the sample.

**Request list.** It is computed from metadata only and frozen as
[`requests.json.gz`](evidence/hyp036-economic-pilot-v1/requests.json.gz), SHA-256
`1cc29544b3d7928e3a99c34c906801f1c0aae9153d34a7ef3a8d0c6ad3a785b0`, named in the
[registry](seen-windows-registry.md) before the first request.

| Data                                                        | Window per request           | Logical requests |
| ----------------------------------------------------------- | ---------------------------- | ---------------: |
| Native 1-minute perp klines, per event                      | [t + 1 min, t + 4 h + 1 min) |              784 |
| Native 1-minute mark-price klines, events with a settlement | the same hold window         |              546 |
| BTCUSDT 1-minute klines, per distinct decision hour         | the same hold window         |              393 |
| **Total**                                                   |                              |        **1,723** |

- **Request size.** Each window is 240 bars, under the endpoint's 1,000-bar limit, so
  one request each.
- **Endpoints.** V5 `market/kline` (`category=linear`) and `market/mark-price-kline`,
  the B3a transport.
- **Limits:**
  - at most two attempts per key: 3,446 attempts in total;
  - a hard cap of 128 MiB on response bytes, persisted across restarts;
  - stop when the cap is exhausted, with no automatic increase.
- **Pacing.** At 3 requests per second this is about 10 minutes.
- **Receipts.** Raw responses and their hashes are kept before normalization.
- **Fail closed:** wrong identity, an out-of-window row, a conflicting duplicate or a
  corrupt receipt fails closed.
- **Unresolved, never dropped:** an absent bar or a fetch error counts as unresolved.
  It never removes the event.

The colleague's upper budget of 1,961 requests assumed a mark request for every event.
Mark bars are needed only at settlements, so this draft requests them for the 546
events that have one. This keeps exposure smaller and stays under that bound.

**Exposure, for the registry row.** Outcome-level 1-minute perp, mark and BTCUSDT price
paths over:

- 2025-01-13T00:01Z to 2025-01-27T03:01Z;
- 2025-07-07T00:01Z to 2025-07-21T03:01Z.

They cover only the 784 holds and their BTC intervals, plus funding rates at the
settlements inside those holds. After this read, these windows count as read at
outcome level for this trigger.

## Decision, fixed before the read

Outcomes, checked in this order:

1. **`not_assessable`:**
   - When it applies: fewer than 95% of events complete for net, or fewer than 95% of
     clusters complete. BTC completeness does not count here.
   - What follows: report the reasons and stop. No new sample, budget or rule.
2. **`continue_to_discovery_protocol`.** Both conditions must hold:
   - the cluster mean net30_bps over complete clusters, funding included, is **above
     0**;
   - it stays **above 0** after removing the top 5 symbols, with weights recomputed.
     - **Ranking.** Each symbol's total is the sum of its complete clusters'
       contributions: each cluster's mean net30_bps divided by the number of complete
       clusters.
     - **Which five.** The five largest totals are removed.
     - **Ties.** Broken by symbol in ascending byte order.

   What follows: only the next proposal opens, not a fetch. That proposal is a full
   discovery protocol on the whole discovery year under a new hypothesis ID. It needs
   its own registry row in main, the colleague's review and the owner's go before any
   tape request. The pilot parts can seed that fetch.

3. **`stop_spending`:** every other complete result.
   - HYP-036's short at this trigger and horizon gets no further budget.
   - No variant is read on these events: no horizon, filter, mirror, subgroup, cost
     level or v1 variant V1 to V3.
   - The decision is recorded in the discovery ledger.
   - Whether the program continues with something else or goes to maintenance until
     the v2 read is the owner's decision.

**One corrective recomputation** is allowed after any first verdict, not only
`not_assessable`. All of these must hold:

- a proven defect against this contract;
- a regression test that fails on the defect;
- the same sealed data, with no new request;
- both results kept and published.

Changing the strategy, the rule or the sample after seeing a result is not a
correction.

**Why there is no "inconclusive" branch.** At this size the planning standard error is
about 28 bps, so nearly any result is inconclusive as evidence. The only decision is
whether to spend more. A third branch with no action of its own would leave room to
argue after the read. The sign rule is lenient on purpose: it stops only a line whose
pilot net is not positive. It does not promise that a positive pilot will survive.

What the rule deliberately does not use:

- **A size threshold such as HYP-036's +30 bps.** That is not because the threshold
  could never pass. If the true mean were +30 bps, a +30 bps threshold on the mean
  would pass in about half of samples. This contract simply chooses a softer budget
  rule for a spending decision. It does not repair an impossible test.
- **A week bootstrap**, since four weeks cannot support one.
- **The matched excess**, since controls are diagnostics.

## Report, fixed now (decides nothing beyond the rule above)

- **Counts:**
  - completeness by reason;
  - events, clusters, symbols, weeks;
  - top-5 symbol share.
- **Net, cluster mean:**
  - at 30 and 50 bps;
  - without funding;
  - funding as a share of net;
  - mark low/high valuation sensitivity;
  - break-even cost.
- **By block (January, July):** the same means. Reported only; a block never decides.
- **BTC-adjusted net:**
  - btc_adjusted_bps as defined above, over BTC-complete events, with its own
    completeness count;
  - a common-market diagnostic, not a tradable hedge.
- **Uncertainty:**
  - the cluster standard error, labeled as understating same-hour and same-week
    dependence;
  - the audit's planning standard error alongside it.
- **USD 50 one-slot portfolio**, with B3a's rules:
  - notional min(USD 45, cash minus USD 5), skipped below USD 5;
  - simultaneous signals ordered by SHA-256 of `hyp036-econ-v1|symbol|t`, exits before
    entries;
  - daily PnL, occupancy, drawdown, losing streak and every skip reported.

  It is required by the program plan's rule 2 and gates nothing here.

- **Provenance:**
  - code revision and dirty state;
  - contract, request-list, receipt and input hashes;
  - start and end times.

Nothing else is computed. No added split, filter or favorable subgroup is reported
after the read.

## Selection history carried into any successor

- **HYP-036 v1:** stopped twice on controls (#567, #568). Its pilot features were seen;
  its returns were not, until this read.
- **Abnormal-flow v1:** its long on buy dominance with contained price lost at every K
  on 2026-07 to 2026-09. A positive short here would partly mirror that known result,
  on another window with another trigger.
- **Other families:** at least 124 exploratory configurations of other families were
  read on this discovery year in the 2026-11-11 series.
- **This read:** one configuration, ledger count +1.

## Implementation, after review

- **Module:** `bybit_absorbed_buying_economic_pilot.py`, reusing B3a's bounded
  transport, receipts, `publish_once`, source binding and portfolio code.
- **`plan`:**
  - metadata only;
  - checks the input hashes;
  - writes the request list and its hash.
- **`fetch`:**
  - refuses a dirty tree, a request list other than the frozen one, or a run without a
    registry row naming that hash;
  - enforces the caps.
- **`read`:**
  - computes only the report above;
  - publishes once.
- **Tests:**
  - synthetic events for entry and exit bar alignment;
  - funding sign for a short and the strict inequality at settlements;
  - completeness reasons;
  - cluster weights;
  - top-5 removal;
  - the three decision branches;
  - portfolio ordering.

## Review record

The colleague reviewed `271f0f7` on 2026-11-14 and found no blocking issue. Three text
fixes are applied above:

- explicit units and formulas, including the BTC diagnostic without fee or funding;
- net completeness separated from BTC completeness, with means over fully resolved
  clusters and no zero fill;
- funding coverage proven by page bounds, not by a gap rule.

Answers to the draft's questions:

1. **Spending rule.** The sign rule with top-5 removal is accepted, with the ranking
   and ties fixed above. No +30 bps threshold is added.
   - The draft's reason was wrong: a +30 bps threshold would not almost always stop.
   - This is a softer budget rule chosen on purpose.
2. **Mark bars.** Only for the 546 events with a settlement. The budget of 1,723
   requests, 3,446 attempts and 128 MiB is agreed.
3. **Identifier.** No new HYP ID for this screen. It uses the study identifier
   `hyp036-econ-v1`, its own registration and a ledger entry of +1. HYP-036 v1 stays
   stopped.
4. **Corrections.** One corrective recomputation after any first verdict, under the
   conditions above.

No new power audit is needed before the code.
