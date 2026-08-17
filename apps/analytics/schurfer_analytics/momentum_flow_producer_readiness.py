"""Shared vocabulary for the "is this venue's own upstream producer
actually capable of what a WATCH/paper worker needs" question.

Exists because of a real incident (2026-08-15 to 2026-08-17):
momentum_flow_watch_binance ran for 32+ hours reporting health status
"ok" while its own upstream producer (Binance capture bars, see
docs/research/binance-momentum-capture-v1.md's own documented v1
limitation that OpenPrice/HighPrice/LowPrice/ClosePrice stay permanently
nil for that venue) never once populated close_price. The WATCH quality
gate (momentum_flow_watch_evaluator.py's own missing_price/stale_quote
reasons) correctly rejected every single evaluation, but nothing
distinguished "producer structurally cannot feed this worker" from "a
real, momentary quiet market" -- both look identical from the worker's
own "ok, zero watches this tick" health output. See docs/research/
binance-watch-input-readiness-v1.md for the full incident writeup, and
that same doc's own colleague-review addendum for why the first version
of this fix was not mergeable as written (a paper worker that refuses to
even start when blocked stops servicing its own already-open positions'
stops/exits -- a materially worse failure than the one being fixed).

Two additional health statuses, alongside "starting"/"ok"/"degraded"
(see momentum_flow_watch_worker.py's and momentum_flow_paper_worker.py's
own _write_health):

  BLOCKED_STATUS ("blocked_upstream_incompatible")
      This worker's own upstream producer does not (yet) satisfy what it
      needs -- checked successfully, and the answer is no. Distinct from
      "degraded" (the worker itself hit a transient error, expected to
      recover on its own): nothing this worker's own retry loop can fix,
      only a change to the producer (or the contract) resolves it. A
      worker in this state keeps running -- see run_watch_worker's/
      run_paper_worker's own per-tick readiness check, not a startup-only
      gate -- polling until the producer becomes ready, rather than
      crash-looping (Docker's own restart: unless-stopped policy turning
      a permanently-incompatible producer into indefinite restart churn,
      log noise, and repeated Postgres/Redis reconnects for no benefit).

  DEPENDENCY_UNAVAILABLE_STATUS ("degraded_dependency_unavailable")
      The readiness check itself could not run (a transient DB/Redis
      error) -- genuinely different from BLOCKED_STATUS: this is
      infrastructure flakiness, likely to resolve on its own on the next
      tick, not a producer/consumer contract problem. Kept as its own
      status (not folded into generic "degraded") because paper's own
      gate on watch's health status needs to tell "watch is definitely
      not ready" apart from "watch's own status could not be read right
      now" -- treating both as BLOCKED_STATUS would make an infra blip on
      one worker look, to an operator, identical to the other worker
      correctly detecting a real incompatibility.
"""

from __future__ import annotations

from datetime import UTC, datetime

BLOCKED_STATUS = "blocked_upstream_incompatible"
DEPENDENCY_UNAVAILABLE_STATUS = "degraded_dependency_unavailable"

# How far back a WATCH worker looks for a valid recent bar before deciding
# its own upstream producer cannot feed it at all. 30 minutes is
# comfortably longer than one bucket_batch_size cycle at the worker's own
# default poll_interval_seconds, so a genuinely healthy producer merely
# between writes never trips this.
PRICE_READINESS_LOOKBACK_MINUTES = 30

# How stale a foreign worker's own Redis health hash (read by
# _upstream_watch_block) may be before it stops counting as evidence of
# anything. Both watch and paper default to a 10s poll_interval_seconds;
# a health hash older than this was written by a process that has not
# ticked in a long time -- if that process crashed hard (OOM-killed, host
# reboot, not the graceful BLOCKED_STATUS write path), Redis would
# otherwise keep returning its last "ok" forever, and a paper worker
# reading it would have no way to ever notice its own upstream stopped
# updating at all.
UPSTREAM_HEALTH_MAX_AGE_SECONDS = 60.0


def upstream_health_is_ready(
    *,
    status: str | None,
    generated_at: str | None,
    now: datetime | None = None,
    max_age_seconds: float = UPSTREAM_HEALTH_MAX_AGE_SECONDS,
) -> bool:
    """Pure interpretation of a foreign worker's own health hash fields
    (status, generated_at -- both written by every _write_health call in
    this package). True only if status is exactly "ok" AND generated_at
    parses to a timestamp within max_age_seconds of now. A missing status
    (key never written), a missing/unparseable generated_at, or a stale
    generated_at are all treated as not ready -- fail-closed: a caller
    depending on this has no way to tell "genuinely fine" apart from
    "I can't actually confirm that" otherwise."""
    if status != "ok" or not generated_at:
        return False
    try:
        written_at = datetime.fromisoformat(generated_at)
    except ValueError:
        return False
    if written_at.tzinfo is None:
        written_at = written_at.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    age_seconds = (reference - written_at).total_seconds()
    # Only the upper bound matters: a negative age (generated_at
    # momentarily ahead of `now`) is ordinary clock jitter between
    # containers on the same host, not evidence of anything stale.
    return age_seconds <= max_age_seconds
