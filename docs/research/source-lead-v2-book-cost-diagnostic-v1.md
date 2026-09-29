# HYP-012 v2 book-cost diagnostic v1

Status: PROPOSED 2026-09-29, before any `send_*_bps` value or exit-book price,
spread or impact has been read. Merging this document registers the diagnostic;
the reader must not run before merge. This is a descriptive engineering read,
not the formal HYP-012 v2 return read or a new strategy test.

## Question and allowed decision

What book-side cost is visible at the registered capture, the later hypothetical
send, and the scheduled exit? The report may identify capture or measurement
problems and inform a separately registered future cost model. It cannot change
the running v2 cohort, replace its 15 bps exit assumption, establish net
profitability, or authorize orders. No v2 return, OHLCV exit close, funding,
`trade_decisions`, or realized fill is read.

## Fixed population and timing

- One row per `source_lead_qualified_capture_v4` qualification with
  `status='qualified'`, selected target `bybit`, and its capture's
  `source_first_observed_at` in **[2026-09-30T00:00:00Z,
  2026-10-14T00:00:00Z)**. This is the first 14 full UTC days after the
  2026-09-29 deployment; the partial deployment day is excluded. No asset,
  outcome, attempt status, or available book is a population filter.
- The denominator includes an episode with no target, shadow attempt, or exit
  observation row. Unique database keys permit at most one row of each kind per
  capture and version. The reader refuses unexpected versions or duplicate
  rows instead of silently multiplying the denominator.
- The read may begin at **2026-10-15T00:00:00Z**, 24 hours after the window
  closes, by the database clock. This exceeds the scheduled 30-minute exit and
  the workers' 10-minute claim recovery. Open or missing rows remain visible;
  no later book is fetched or imputed.
- One first-writer-wins JSON artifact under
  `/runtime/research/source_lead_v2_book_cost_diagnostic`. A rerun returns the
  saved artifact and verifies its SHA-256 without querying the database. A
  crash before publication can retry the same fixed window; the artifact
  records its actual read time and a SHA-256 of the rows selected in a single
  read-only repeatable-read transaction.

## Three distinct measurements

All bps are relative to the **same book's midpoint**. Impact already includes
half the spread. Spread must not be added again. Each distribution has its own
`n`, mean, p50, p90, p99, and maximum; no missing value is replaced with zero.

1. **Capture:** the selected Bybit target observation's stored
   `liquidity.spread_bps` and `liquidity.ask_impact_bps` for $50, with its
   registered point-in-time identity and fresh, instrument-sourced contract
   size. This is the v2 entry book, not an order fill.
2. **Hypothetical send:** only `source_lead_shadow_v1` attempts tagged
   `source_lead_send_book_costs_v1`. The principal distribution requires
   `shadow_recorded`, the exact selected `identity_key`, `late=false`, and book
   age in [-1000, 2000] ms. It shows
   `send_spread_bps`, `send_notional_ask_impact_bps` ($50), and
   `send_qty_ask_impact_bps` (the rounded intended quantity) separately.
   `shadow_recorded` late attempts are a separate sensitivity. Rejections can
   carry a partial book measurement, but never enter the principal cost
   distribution.
3. **Scheduled exit:** only `source_lead_exit_book_v1` observations with
   `outcome='sampled'`, `timeliness='on_time'` (at most 30 s late), valid book
   age in [-1000, 2000] ms, instrument-sourced contract size, complete bid
   depth for the positive stored hypothetical quantity, and the exact selected
   `identity_key` and entry time. The reader verifies the stored raw snapshot
   against `book_sha256` before using `spread_bps` and `impact_bps` (midpoint
   minus bid VWAP for that quantity). A sampled snapshot with a missing or
   mismatched hash is an integrity error, not an ordinary missing book.

Capture, send and exit books are never interchangeable. Send's $50 VWAP and
exit's entry-derived quantity need not represent the same position size; the
report counts exact quantity matches but does not add the two impacts. It does
not compare exit bid VWAP with the formal OHLCV close. Hence it cannot tell
whether 15 bps of formal exit slippage is conservative or compute a net
return. Fees, funding, maker fill probability, queue position and adverse
selection are unmeasured. A maker limit order is not assigned a cheaper cost.

## Coverage and missingness

Report the full eligible denominator, target status, attempt outcome and
capture-version counts (including no attempt and pre-version attempts), exit
outcome/timeliness counts (including no exit row), and each measurement's own
coverage. Send distributions are split by the registered 30-second `late`
flag. Exit books late by 31-120 s or missed are counted but do not replace an
on-time price. Identity, timing, book-age, depth and cost-field failures each
have their own count. The principal paired count requires both an on-time send
and an on-time exit; an additional count requires identical rounded quantities.
No cost estimate from observed books is extrapolated to missing episodes.

The report is descriptive even if its sample is large. It may support a new
prospective cost contract only after separate registration and review. It
cannot be used to select a profitable asset, timing rule, venue, or variant
from this v2 cohort.

## Implementation boundary

The reader has a fixed window and canonical artifact path, with no CLI option
for an alternate period or output directory. It reads only qualification,
capture target, shadow attempt, and exit observation fields named above. Raw
exit snapshots are hashed in memory and never emitted. The JSON includes the
reader version, code revision, dirty-tree flag, database and artifact times,
row fingerprint, all denominators, and only aggregate cost distributions.
The formal v2 reader remains independent and unchanged.
