"""Pure health-status computation for early_momentum, shared by the HTTP
health endpoint (`routers/health.py`) and the independent health monitor
task (`early_momentum.run_early_momentum_health_monitor`) -- one place
decides the status; alerting and the read path can never silently
disagree.

Deliberately not in `schurfer_market_quality` (that package is about input
window quality, not worker/process liveness) and not inside the HTTP
router (the monitor task needs the exact same decision without going
through FastAPI).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from datetime import datetime

    from .worker_health import WorkerHeartbeat

Status = Literal["ok", "degraded", "error"]

REASON_SCANNER_HEARTBEAT_MISSING = "scanner_heartbeat_missing"
REASON_SCANNER_HEARTBEAT_STALE = "scanner_heartbeat_stale"
REASON_TRIGGER_HEARTBEAT_MISSING = "trigger_heartbeat_missing"
REASON_TRIGGER_HEARTBEAT_STALE = "trigger_heartbeat_stale"
REASON_SCANNER_TICK_FAILED = "scanner_tick_failed"
REASON_TRIGGER_TICK_FAILED = "trigger_tick_failed"
REASON_SOURCE_BARS_STALE = "source_bars_stale"
REASON_LIFECYCLE_METRICS_UNAVAILABLE = "lifecycle_metrics_unavailable"
REASON_OVERDUE_ARMED = "overdue_armed_episodes"
REASON_EXPIRED_CLAIMS = "expired_claims"
REASON_QUALITY_READY_ZERO = "quality_ready_zero"
REASON_QUALITY_READY_ZERO_SUSTAINED = "quality_ready_zero_sustained"
REASON_QUALITY_WINDOW_REWARMING = "quality_window_rewarming"
REASON_IDENTITY_CATALOG_STALE = "identity_catalog_stale"
REASON_IDENTITY_HEALTH_UNAVAILABLE = "identity_health_unavailable"

_ERROR_REASONS = frozenset(
    {
        REASON_SCANNER_HEARTBEAT_MISSING,
        REASON_SCANNER_HEARTBEAT_STALE,
        REASON_TRIGGER_HEARTBEAT_MISSING,
        REASON_TRIGGER_HEARTBEAT_STALE,
        REASON_LIFECYCLE_METRICS_UNAVAILABLE,
        REASON_QUALITY_READY_ZERO_SUSTAINED,
        REASON_IDENTITY_HEALTH_UNAVAILABLE,
    }
)


def _heartbeat_reasons(
    heartbeat: WorkerHeartbeat | None,
    *,
    worker: str,
    now: datetime,
    ttl_seconds: int,
    within_startup_grace: bool,
    failed_reason: str,
    missing_reason: str,
    stale_reason: str,
) -> list[str]:
    if heartbeat is None:
        return [] if within_startup_grace else [missing_reason]
    reference = heartbeat.completed_at or heartbeat.started_at
    age_seconds = (now - reference).total_seconds()
    reasons = []
    if age_seconds > ttl_seconds and not within_startup_grace:
        reasons.append(stale_reason)
    if heartbeat.state == "failed":
        reasons.append(failed_reason)
    return reasons


def compute_status(
    *,
    now: datetime,
    startup_at: datetime,
    grace_period_seconds: int,
    scanner_heartbeat: WorkerHeartbeat | None,
    trigger_heartbeat: WorkerHeartbeat | None,
    heartbeat_ttl_seconds: int,
    source_max_lag_seconds: dict[str, float | None],
    source_lag_limit_seconds: int,
    overdue_armed: int | None,
    oldest_overdue_armed_age_seconds: float | None,
    expired_claims: int | None,
    oldest_expired_claim_age_seconds: float | None,
    lifecycle_reaper_grace_seconds: int,
    consecutive_zero_quality_ready_ticks: int,
    zero_quality_ready_error_threshold: int,
    quality_window_rewarming: bool,
    quality_window_rewarming_max_ticks: int,
    identity_health: dict[str, dict[str, Any]],
) -> tuple[Status, tuple[str, ...]]:
    """Pure: every input is passed in, nothing is read from a clock, Redis,
    or Postgres here -- callers gather the inputs, this only judges them.

    `identity_health` is `episodes.identity_health`'s per-exchange
    `{"age_seconds": ..., "stale": bool}` shape. Without folding it into
    the overall verdict, a stale identity catalog can reject every single
    candidate at ARM time (REASON_IDENTITY_CATALOG_STALE in episodes.py)
    while quality_ready/candidates_found still look perfectly healthy --
    exactly the "zero trades, no explanation" failure mode this whole PR
    exists to eliminate (colleague review). An empty/missing dict (the
    identity query itself unavailable) fails closed as an error, same
    principle as REASON_LIFECYCLE_METRICS_UNAVAILABLE above.

    overdue_armed/expired_claims becoming momentarily positive right after
    their own expiry is expected scheduling noise, not degradation -- the
    reaper only runs once per trigger tick (~60s), so a row can sit
    "overdue" for up to a tick's worth of ordinary delay before the reaper
    even gets a chance at it. Only once the OLDEST such row's age reaches
    `lifecycle_reaper_grace_seconds` does this count as a real stall and
    raise a reason; the raw count itself is still always returned to
    callers for display (see gather_health_status/routers/health.py), it
    just doesn't by itself flip status away from ok. A positive count with
    no age reading (the age sub-select disagreeing with the count
    sub-select -- should never happen, but this function trusts nothing)
    fails closed exactly like a missing count does."""
    within_grace = (now - startup_at).total_seconds() < grace_period_seconds
    reasons: list[str] = []

    reasons += _heartbeat_reasons(
        scanner_heartbeat,
        worker="scanner",
        now=now,
        ttl_seconds=heartbeat_ttl_seconds,
        within_startup_grace=within_grace,
        failed_reason=REASON_SCANNER_TICK_FAILED,
        missing_reason=REASON_SCANNER_HEARTBEAT_MISSING,
        stale_reason=REASON_SCANNER_HEARTBEAT_STALE,
    )
    reasons += _heartbeat_reasons(
        trigger_heartbeat,
        worker="trigger",
        now=now,
        ttl_seconds=heartbeat_ttl_seconds,
        within_startup_grace=within_grace,
        failed_reason=REASON_TRIGGER_TICK_FAILED,
        missing_reason=REASON_TRIGGER_HEARTBEAT_MISSING,
        stale_reason=REASON_TRIGGER_HEARTBEAT_STALE,
    )

    if not within_grace:
        for lag in source_max_lag_seconds.values():
            if lag is None or lag > source_lag_limit_seconds:
                reasons.append(REASON_SOURCE_BARS_STALE)
                break

    if overdue_armed is None or expired_claims is None:
        reasons.append(REASON_LIFECYCLE_METRICS_UNAVAILABLE)
    else:
        if overdue_armed > 0:
            if oldest_overdue_armed_age_seconds is None:
                reasons.append(REASON_LIFECYCLE_METRICS_UNAVAILABLE)
            elif oldest_overdue_armed_age_seconds >= lifecycle_reaper_grace_seconds:
                reasons.append(REASON_OVERDUE_ARMED)
        if expired_claims > 0:
            if oldest_expired_claim_age_seconds is None:
                reasons.append(REASON_LIFECYCLE_METRICS_UNAVAILABLE)
            elif oldest_expired_claim_age_seconds >= lifecycle_reaper_grace_seconds:
                reasons.append(REASON_EXPIRED_CLAIMS)

    if (
        quality_window_rewarming
        and 0 < consecutive_zero_quality_ready_ticks <= quality_window_rewarming_max_ticks
    ):
        reasons.append(REASON_QUALITY_WINDOW_REWARMING)
    elif consecutive_zero_quality_ready_ticks >= zero_quality_ready_error_threshold:
        reasons.append(REASON_QUALITY_READY_ZERO_SUSTAINED)
    elif consecutive_zero_quality_ready_ticks > 0:
        reasons.append(REASON_QUALITY_READY_ZERO)

    if not identity_health:
        reasons.append(REASON_IDENTITY_HEALTH_UNAVAILABLE)
    elif any(data.get("stale", True) for data in identity_health.values()):
        reasons.append(REASON_IDENTITY_CATALOG_STALE)

    reasons = list(dict.fromkeys(reasons))  # dedupe, preserve order

    if not reasons:
        return "ok", ()
    if any(reason in _ERROR_REASONS for reason in reasons):
        return "error", tuple(reasons)
    return "degraded", tuple(reasons)


__all__ = [
    "REASON_EXPIRED_CLAIMS",
    "REASON_IDENTITY_CATALOG_STALE",
    "REASON_IDENTITY_HEALTH_UNAVAILABLE",
    "REASON_LIFECYCLE_METRICS_UNAVAILABLE",
    "REASON_OVERDUE_ARMED",
    "REASON_QUALITY_READY_ZERO",
    "REASON_QUALITY_READY_ZERO_SUSTAINED",
    "REASON_QUALITY_WINDOW_REWARMING",
    "REASON_SCANNER_HEARTBEAT_MISSING",
    "REASON_SCANNER_HEARTBEAT_STALE",
    "REASON_SCANNER_TICK_FAILED",
    "REASON_SOURCE_BARS_STALE",
    "REASON_TRIGGER_HEARTBEAT_MISSING",
    "REASON_TRIGGER_HEARTBEAT_STALE",
    "REASON_TRIGGER_TICK_FAILED",
    "Status",
    "compute_status",
]
