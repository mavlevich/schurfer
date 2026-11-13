# B2 pre-settlement maker feasibility v1

Status, 2026-11-13: scope and model fixed before any book/trade body or fill result
is read. The owner asked to continue after the passing B1 result, without another
review round. This stage checks archive availability and whether a defensible
entry replay can be constructed; it does not authorize a strategy-return read,
the test year or any order.

## Frozen population and windows

Use only B1's accepted 4h S3 identities:
`events.json.gz` SHA-256
`4f1554108d6da503630d24d5b123d8000532b7bb3decb33f033e57dab65711f7`,
bound to report `96ed96d58514815203aa221a35bce5ed19a37b9b5316947148375a033170311e`.
No eventual funding label selects an event. All three offsets remain represented:
d30/d15/d10 have 1,637/1,594/1,574 events. The union has 151 native instruments,
1,685 forecasts and 4,805 decision points; these are overlapping configurations,
not 4,805 independent observations.

Historical Bybit linear perpetuals only, 2024-10-01 inclusive through 2025-10-01
exclusive. The initial archive scope covers one minute before decision through
decision + 301 seconds, including both the latency assumption and longest wait.
It has exactly 724 native symbol-days. A daily file can contain other hours: those
bytes are storage exposure, and parsing outside the required windows is restricted
to locating/rebuilding the initial snapshot and source-quality checks. No price
series, setup return or exit/funding outcome is emitted by B2.

## Source and bounded metadata inventory

The [official Bybit history page](https://www.bybit.com/en/derivative-activity/history-data)
was opened in a browser. Its own public `list-files` request returned the filename,
size and direct `quote-saver.bycsi.com` URL for a 2025-09-30 order-book file. A direct
HEAD matched the catalogue's 91,864,958-byte size. This confirms the source link;
it does not validate any archive's contents. The proof is retained separately from
the archive study. That one provenance example is not a study event.

Before a large download, HEAD **both** ob500 and ob200 and the trade gzip for each
of the 724 symbol-days: 2,172 fixed URLs, at most 4,344 attempts including retries,
3 request starts per second. Read no response body. Persist each attempt before
HTTP and each selected-header response once; resume does not restart successful
requests. HTTP 403/error/redirect means unknown, not missing; only 404 is
`not_found`. Missing Content-Length means unknown size, not zero. Prefer a verified
ob500 file, otherwise a verified ob200; retain both responses. Unknown sizes and
missing inputs must remain visible before a download budget is chosen.

The metadata tool is `bybit_presettlement_book_scope`. `plan` publishes the frozen
scope to a separate plan directory; `probe` uses its own resumable run directory.
It cannot issue GET for an archive and cannot consume a body. Source identity,
input SHA, clean code revision, request list and request limits are bound before
the first HEAD. No current instrument catalogue is fetched.

## Entry policies, fixed before replay

- Primary: d30, buy USD 50 notional at the last causally observed best bid at the
  decision, assumed order arrival 1,000 ms later, wait 60 seconds after arrival.
  Keep the original price, without repricing. A post-only order that would cross
  the ask on arrival is rejected, not converted to taker.
- The virtual quantity joins behind the visible quantity Q0 at that price on
  arrival. Only subsequent qualifying aggressive sells at or below that bid
  advance it: `filled = min(q, max(0, V - Q0))`. Do not count cancellations as
  progress. Do not give a full fill solely because one trade crosses the price.
- Keep partial fills; cancel the unfilled remainder at expiry. No implicit taker
  completion. A cash/unfilled event stays in the original event denominator.
- Fixed sensitivities: d15/d10; 300-second wait; zero queue ahead; USD 25/USD 100;
  5,000-ms arrival. Report each by name, without selecting a winner from B2.
  These change placement assumptions, not the signal or its universe.
  Each sensitivity changes one primary axis at a time, without a Cartesian grid:
  eight configurations including the primary. The longest window is therefore
  1,000 + 300,000 ms, not a combined long-wait/long-latency variant.
- Quantity is a research notional converted using the placement price. Base-coin
  units and multiplier identity must be validated. Unavailable historical lot/minimum
  constraints are explicit, so a continuous-quantity simulation is never presented
  as proof that a native USD 50 order was valid.

Entry fee assumptions for a later cost comparison are 2 bps maker and 5.5 bps
taker on actual filled notional; these are fixed modelling assumptions, not a
claim about the owner's current account fee. Exits, funding payments, profitable
trade classification and the all-signals economic comparison belong to B3.

## Source-quality pilot before any fill calculation

Choose 10 symbol-days, 5 delivered by the discovery end and 5 continuing then,
using the smallest SHA-256 of `b2-source-pilot-v1:symbol:day` within each stratum,
at most one day per instrument. Delivery identity comes from the already pinned
index, not today's listing. Freeze the IDs and byte cap before fetching bodies.
If a stratum has fewer than 5 eligible instruments, report the shortfall; do not
silently replace it. Metadata existence cannot resample the pilot.

The completed [HEAD result](bybit-presettlement-b2-scope-result-2026-11-13.md)
shows that all ten original pilot books use ob500. Before any body read, two
separate ob200 format fixtures were therefore frozen by a published metadata-only
hash rule; the original ten are unchanged. Their 24 book/tape files total
1,555,848,890 bytes, with a 2 GiB wire cap including retries/partials and at most
48 attempts at 3 requests/s. No complete-corpus download follows from this cap.

Check snapshots/deltas, depth, sizes/multipliers, clocks, resets, crossed books,
cadence/gaps, and alignment with trades. Emit quality/count/format diagnostics
only, no fill rate or PnL during this calibration. Retain the exact native inputs,
checksums and starting snapshot. Freeze the replay gap/staleness rule from this
cadence check before starting the fill replay, as required by strategy v2.

The official page describes u/seq as sequence identifiers and u=1 as a possible
restart; it does not promise adjacent integers. The legacy burst-cost reader's
`u == previous + 1` condition therefore cannot be adopted without validation.
This is an unsupported assumption to check, not a new conclusion about that
closed study's returns. A reset/snapshot replaces the book; ambiguous ordering,
conflicting equal-ID messages or unsupported clocks remain explicit failures.
System-generated `ts` is not automatically a matching-engine timestamp.

Block/RPI eligibility is not inferred from an absent field. Verify the actual
archive fields and their semantics in the pilot. Unknown-class volume stays
unknown: report an excluded-volume lower bound and included-volume sensitivity,
and do not label the latter identified API-maker fills. A price touch is not a
substitute for missing eligible volume. This uncertainty cannot be repaired by
changing the signal or selecting profitable filled orders.

### Source-pilot implementation registration

Before the first archive body read, bind the fixed `pilot-body-plan.json`
SHA-256 `39c5bda6f438f51e90fbb899a2eb93250acf995727d032a6e725f5a011fe8630`
to a clean code revision and source fingerprint. The transport reserves each
whole native file, rounded to 64 KiB plus a 64 KiB read guard, durably before
reading its body. Failed attempts retain that reservation. A restart cannot
replenish the 2 GiB cap or the 48 global / two-per-file attempt limits. Refuse
changed Content-Length or ETag, redirects, corrupt completed files and mismatched
run bindings. Keep native files opaque and immutable with SHA-256 receipts.

The quality inspector streams ZIP/CSV without extracting members: at most one
book member, 1 MiB per line, 4 GiB inflated per file and 32 GiB in total. Refuse
unsupported schemas and in-scope timestamps outside the registered discovery.
Report schemas, counts, sequence/clock regressions, observed depth counts, cadence,
quantity/notional consistency and strict-prior-quote alignment. The diagnostic
staleness counter uses 5 seconds; it is not a replay admission threshold.

HYP-031's native trade-tape reservation remains intact: minutes :55 through :04
of every hour are skipped before converting trade price, size or turnover.
Only their row count leaves the inspector. No masked trade values, quote series,
fill quantity, fill rate or strategy return are emitted. Raw daily source files
are retained for provenance; their masked rows are not analytical observations.
An absent BT/RPI field remains unknown, and `ts` without `cts` remains a system
clock proxy. No missing field is inferred from present-day API documentation.

After this source-only pilot, record limitations before deciding whether a
defensible entry replay can proceed. Neither a complete download nor a successful
parse by itself is an execution-quality or economic pass.

The first implementation (`c1c9fae`) rejected all 12 pairs at a book row outside
the named file day. Retained timestamp-only boundary inspection showed that all
12 archives end with a snapshot 0.655 to 1.904 seconds after the next midnight.
The original report is retained as `quality-v1.json`; it is not replaced or called
a successful parse. Before rerunning, version 2 clips book rows to the named UTC
day: count off-day rows, skip their levels before numeric conversion and before
advancing trade alignment. An entirely off-day book remains invalid. This is a
correction of the archive-boundary assumption, not a change to an execution,
signal, cost, coverage or profitability threshold. No fill/strategy return has
been read. The transport, frozen pilot and native checksums remain unchanged.

## What can proceed

The metadata stage may finish with sizes/availability or explicit unknowns. Only
then fix the pilot/body download budget. Source calibration precedes the fill
model; unresolved format, unit or clock problems block affected events as
`incomplete_book_data`, never as filled/unfilled. Retain the full denominator.

The later fill report must cover every placement configuration and every original
signal: complete/partial/no fill, rejected/unknown, waiting time and reason counts.
At least 95% input completeness is required for an interpretable configuration;
this is a coverage condition, not a profitability pass. Preserve the adverse
selection check required by the program plan: B3 must compare taker economics of
filled and unfilled groups and maker economics over **all** signals. B2 does not
read or choose an exit, run a portfolio or release live trading.

## Tooling verification before HEAD

Ten focused tests pass, including frozen-scope identity/bounds, no body consumption,
unknown size versus zero, HTML error-page refusal, interrupted-attempt accounting
and resume without duplicate requests. Ruff and strict mypy pass for the scope
module and its tests. This is an analytics-only metadata tool; no production code,
orders or fill calculation runs in this step.
Full `make verify` also passed after correcting an unreachable-code issue in the
test-only body-forbidden stream. The completed HEAD inventory and original run
binding are retained with the result.
