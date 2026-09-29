# HYP-012 v2 shadow send-book cost capture v1

Status: implementation for future shadow attempts. The HYP-012 v2 formal cohort,
registered return, cost assumptions and verdict are unchanged. No return or
exit price is read by this capture.

The shadow worker already requests a fresh Bybit book before it would send a
long intent. Migration 0056 adds three nullable measurements and a nullable
capture-version marker to that same attempt row. They are computed from the
book already in memory, after its timestamp and crossed-book checks; this adds
no exchange request.

| Field                          | Meaning                                                                                                               |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------- |
| `send_spread_bps`              | Full best-ask minus best-bid spread divided by their midpoint.                                                        |
| `send_notional_ask_impact_bps` | $50 ask VWAP minus midpoint, divided by midpoint, in bps. Same $50 basis as the capture quote and `quote_change_bps`. |
| `send_qty_ask_impact_bps`      | Ask VWAP for the actual rounded hypothetical quantity minus midpoint, divided by midpoint, in bps.                    |

The midpoint-to-VWAP measurements **already include half the spread**. Adding
the full spread again would double count it. All three fields exclude fees,
funding, queue priority, fill probability and actual order slippage: no order
is sent. A maker limit at the desired price cannot be assigned a lower
realized cost without measuring its fills and adverse selection.

Every eligible qualified episode still gets one shadow-attempt outcome. A
valid book can yield a spread before $50 depth is known; a valid $50 quote can
yield the notional impact even if the rounded order later fails minimum-size
checks. A quantity impact exists only after the rounded quantity has full ask
depth. Old attempts have `send_cost_capture_version = NULL`. Every attempt
claimed by the new worker, including a failed attempt, has
`send_cost_capture_version = source_lead_send_book_costs_v1`. Thus a `NULL`
measurement on a versioned attempt means that particular book-side cost was
unavailable; it cannot be confused with pre-deployment missingness. No value
is imputed or backfilled from later books. Outcome and quote timestamps retain
their existing meanings.

## Read boundary

The three new cost values and the related entry or send prices are not read,
summarized, sampled or displayed before a diagnostic protocol is registered
**before the first look**. That protocol must fix the population, observation
window, metrics, coverage denominators, missingness, handling of late books and
the permitted interpretation. It must keep capture- and send-time books
separate. This document registers capture only; it does not authorize a cost
read. The existing latency diagnostic retains its own registered read and
continues to use only its stated operational fields.

The proposed diagnostic read is specified in
`source-lead-v2-book-cost-diagnostic-v1.md`. It becomes registered only when
that protocol is merged, before its first read.

The v2 cohort contract is stricter for exit books: until a separate registered
exit diagnostic read, only coverage, statuses and delays may be shown. Exit
prices, exit spread and exit impact may be read only under that registration.
Neither entry nor exit diagnostics may change the registered v2 verdict.

## Deployment record

Operations amendment, 2026-09-29: the pre-migration backup
`db-2026-09-29T14:36:42` completed with its receipt at 15:01:41 UTC.
Migration 0056 was applied before the updated execution container started at
15:02:54 UTC. Deployment completed before 15:05 UTC. The execution worker was
healthy with `SOURCE_LEAD_MODE=shadow`, `DRY_RUN=true` and `AUTO_TRADE=false`.
At the 15:05 UTC outcome-blind health check there were no v2-qualified Bybit
episodes, so the first versioned attempt and its exact data cutover were still
pending. Old attempts are not backfilled. The deployment interval should be
marked in the first weekly latency report.

The book-side fields support a descriptive cost calibration for a future
contract version. They do not replace the registered v2 cost model or turn a
shadow attempt into a realized fill.
