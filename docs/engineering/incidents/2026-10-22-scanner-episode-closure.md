# Scanner episode closure incident: 2026-10-22

## Confirmed scope

ENG-038 records the read-only production observations. CoinEx is unavailable and
the scanner requires all configured venues to succeed before advancing absent
episodes toward closure. Removing the unavailable venue from the scanner defaults
restores the opportunity to close on healthy iterations. Other venue errors still
block closing. Neither a new capture nor quiet-alert recovery is guaranteed until
a new Gate source is naturally observed.

This is an explicit prospective coverage change, not an API recovery or a new
episode definition. CoinEx remains supported for explicit configuration,
historical reports and default outcome resolution. No captured source, episode, qualification, return or study
window is rewritten. No cohort outcome was read during diagnosis.

## Scoped rollout after review and operator authorization

1. Use a clean production `main` containing the fix. Record its revision, UTC
   rollout time, analytics image/container identity, container restart count and
   available disk/RAM. Check the current non-secret `PUMP_EXCHANGES`: empty or
   missing selects the fixed defaults; an explicit list containing CoinEx overrides
   them and needs its own operator decision. Production was empty during diagnosis.
2. Use `make prod-deploy-svc SERVICE=analytics`. No migration or datastore repair is
   required. Do not restart momentum WATCH, hold12h, capture or other research
   workers for this change. Pending captures follow the existing process-restart
   recovery contract; record any restart abandonments separately.
3. Inspect `scanner.starting`, recent `exchange.scanned` / `exchange.failed`,
   `scanner.stored`, `scanner.retrace_skip` and `persistence.episode_closed` logs.
   Confirm CoinEx is absent from the configured/scanned venue list, and that a
   fully successful iteration actually reaches episode-close processing. Keep
   real errors from the remaining venues visible.
4. Observe at least three fully successful iterations. Query only lifecycle
   timestamps, miss counters and capture statuses: `app.pump_events.closed_at`,
   `miss_count`, last-seen timestamps, stale collecting and abandoned errors.
   Verify naturally absent old episodes close. Do not bulk-close rows manually or
   reconstruct old episodes. Record the first new natural Gate source and capture
   when available; confirm its normal recovery notification separately.
5. Expect a backlog of old episodes to close at the current processing time and
   some subsequent reappearances to get fresh identities. This is not evidence of
   more independent market opportunities. Record rollout and first verified
   closure progress as an operational boundary before any later formal read.

## Research limitations and remaining work

The last observed close was October 12; that timestamp alone is not the outage
onset. Determine the affected interval from available scan-error/skip logs and
metadata, including whether earlier fully successful scans existed. Preserve old
rows and frozen study definitions. Audit affected episode/source identity and
missingness without examining prospective prices, returns, funding outcomes or
PnL. Do not restart, extend or reselect a cohort to compensate for this incident.

Any effect on HYP-012 requires an outcome-blind metadata audit; HYP-015's separate
minute WATCH workers cannot be declared affected merely because the scanner
episode table is degraded. Keep its registered read boundary intact.

A separate change should expose the last completed episode-close evaluation and
its blocking venues, with a bounded alert for prolonged blocked progress. Avoid
using "time since last closed episode" alone: a healthy market can legitimately
have nothing due to close. CoinEx may rejoin defaults only after a bounded public
catalog/ticker check succeeds and the coverage boundary is recorded.

## Rollback

If analytics fails to start, stops publishing fresh output, or source/capture
consistency regresses after rollout, revert the merged change and redeploy only
analytics from clean `main`. This restores the earlier defaults and its known
CoinEx blockage; it does not undo episode closes already processed. A database
rollback or historical episode repair is not part of this procedure.
