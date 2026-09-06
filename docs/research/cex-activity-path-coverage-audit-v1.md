# CEX activity path coverage audit v1

## Purpose

`research/cex-activity-discovery-result-v1` recorded HYP-016's real
verdict (`insufficient_data`, both directions) and named two independent
causes: an evidence-floor counterfactual (56/60 eligible episodes, both
under the registered 100-pair floor) and a strict-completeness finding
(99.91% overall minute coverage, but the frozen contract requires exact
1,440/1,440 completeness). That PR explicitly deferred one question:
**why** are these minutes missing. This is that audit.

Scope, agreed with colleague review before any code was written: read-only,
using only the `request_id`/`symbol`/`entry_at` values already present in
the frozen artifact (fingerprint
`382ac208890119447c09e2945b7869c29711ca826e054cbe8676b71f6d74bbb1`). No new
candidate detection, no threshold changes, no outcome-label re-derivation.
The tool itself is `apps/analytics/schurfer_analytics/
cex_activity_path_coverage_audit.py` (`make cex-activity-path-coverage-
audit-report` / `make prod-cex-activity-path-coverage-audit-report`).

## Method

For each of the 678 requests (99 signal, 579 control) the frozen artifact
already marked `incomplete_24h_path`, this audit re-queries
`timeseries.bybit_momentum_bars_1m` for that request's own
`[entry_at, entry_at + 1440m)` window, WITHOUT the live pipeline's own
completeness/positive-price filters, and classifies every one of the 1,440
expected minutes as one of:

- `row_absent` -- no row at all for that `(symbol, bucket_start)`.
- `price_incomplete_or_null` -- a row exists but `price_complete` is not
  `true` (`false`, or `NULL` for rows written before migration 0030 --
  none observed in this window, see below).
- `invalid_or_missing_ohlc` -- a row exists with `price_complete = true`
  but open/high/low/close is missing or not strictly positive.

A wrong-`capture_version` category was checked once, directly, before
writing any per-minute logic: exactly one `capture_version` (`'v1'`) exists
across the whole `2026-08-17`..`2026-08-29` span these requests fall in, so
that category is empty by construction and was not implemented as a
per-minute check.

## Findings

**No resolver bug.** Spot-checked five requests' independently recomputed
`observed_minutes` against the frozen artifact's own recorded value:
`STEEMUSDT` `signal:50` (1439/1439), `control:50:m2` (1436/1436),
`control:50:p3` (1439/1439), `control:50:p4` (1437/1437), `control:50:p5`
(1439/1439) -- exact match every time. `ExactPricePath.unresolved_reason`'s
existing `observed_minutes != 1440` check is correct; this audit's own
independent re-derivation agrees with it.

**No delisting or universe exit.** For every one of the 678 requests, its
own symbol's most-recent bar in the whole table (not just this request's
24h window) falls well after that request's own window end -- none of the
678 requests' symbols had their last available bar fall before the window
needed it. This rules out delisting/universe-exit as a cause for this
window.

**Reason totals**: `price_incomplete_or_null` 1,143, `invalid_or_missing_
ohlc` 287, `row_absent` 383 (1,813 total bad-minute instances across 678
requests, out of 976,320 possible). A real asymmetry: **every** `row_absent`
instance (383/383) is on a control path; signal paths have zero. Signal
paths only ever show `price_incomplete_or_null` (131) and
`invalid_or_missing_ohlc` (16). Not fully explained by this audit --
plausibly signal episodes, by construction, follow high-activity windows
where the capture pipeline is more likely to write SOME row even if
imperfect, while quiet-day control candidates can fall in genuinely
sparser periods -- flagged as an open question, not asserted as fact.

**Missingness is not uniform noise; it clusters by symbol.** The top 20
symbols by missing-minute total (`FLUXUSDT` 76, `CUSDT` 68, `RLCUSDT` 66,
`CKBUSDT` 64, `ANKRUSDT`/`RVNUSDT`/`CELOUSDT`/`CROSSUSDT` 60 each, ...) are
a small, specific set of lower-liquidity symbols, not a random sample of
the ~517-symbol universe. A uniform per-minute failure rate applied
independently across 1,440 minutes/request would predict almost every
request having at least one bad minute; the actual 17/116 (signal) and
102/681 (control) fully-clean rate is far higher than that would predict,
consistent with badness concentrating on specific symbols/periods rather
than spreading evenly.

**Two distinct failure populations, not one.** Longest-consecutive-gap
histogram across the 678 requests: `{1: 646, 2: 7, 8: 9, 12: 16}`. The
dominant population (646/678, 95.3%) is isolated single-minute blips. A
much smaller population (32/678, 4.7%) shows a genuine 8- or 12-minute
continuous outage -- a materially different failure mode from routine
noise.

**A real, if partial, global-outage signature exists.** 46 minutes across
the 9-day window show `>=5` distinct symbols simultaneously bad at the
same minute, several reaching 40-78 symbols at once (2026-08-19 08:20,
2026-08-26 13:56, 2026-08-18 21:48, among others). Even the largest of
these affects well under a third of the ~517-symbol universe -- these read
as short, real capture-service blips/restarts, not a full-service outage.

## Verdict-branch conclusion

Per the audit-outcome policy agreed in `docs/research/discovery-ledger.md`
before this audit ran: **data genuinely absent.** The missingness is real
(not a resolver bug), not explained by delisting/universe exit, and not an
artifact of an overly strict validator applied to otherwise-clean data --
the underlying capture data itself has real, if small, gaps, concentrated
on specific symbols and including a real minority population of genuine
short outages.

Per that same policy: **HYP-016 stays `insufficient_data` on this window
permanently.** This does not, on its own, change the earlier verdict --
the evidence-floor counterfactual (56/60 eligible episodes under the
100-pair floor) already independently ruled out `forward_candidate` here
regardless of completeness. If a prospective continuation is pursued, it
needs, in addition to a new/longer/untouched forward window sized to
plausibly clear the pairs floor: a pre-declared (before viewing any new
data) policy for these routine 1-3 minute gaps -- not a bare "allow N
missing minutes" rule (a missing minute could have contained the very
touch that would flip an outcome label), but provenance-backed exact-candle
backfill or a pre-registered worst/best-case sensitivity bound, matching
what was already agreed before this audit ran. No CEXTrack-style
live-capture infrastructure and no direction selection before that.

## What this audit does not answer

Root-causing the SOURCE of the per-symbol clustering (a real Bybit-side
liquidity/update-rate difference for these specific symbols? a
capture-service-side issue specific to certain instruments?) and the exact
cause of the ~46 short multi-symbol outage minutes (a specific capture
restart, a network blip, a Bybit-side event) are both out of scope here --
this audit answers "is the missingness real and where does it cluster",
not "why does the capture pipeline behave this way for these symbols".
Either would need its own investigation, and neither blocks recording this
window's own verdict as final.
