from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, Response

from .. import early_momentum

router = APIRouter()


def _heartbeat_to_json(heartbeat: Any) -> dict[str, Any] | None:
    if heartbeat is None:
        return None
    return {
        "worker_name": heartbeat.worker_name,
        "worker_version": heartbeat.worker_version,
        "state": heartbeat.state,
        "started_at": heartbeat.started_at.isoformat(),
        "completed_at": heartbeat.completed_at.isoformat() if heartbeat.completed_at else None,
        "duration_ms": heartbeat.duration_ms,
        "counters": heartbeat.counters,
        "last_error": heartbeat.last_error,
    }


@router.get("/health/early-momentum")
async def get_early_momentum_health(request: Request) -> dict[str, Any]:
    """Episode-lifecycle observability: overdue armed episodes and expired
    claims should both stay near zero between reaper runs -- a sustained
    non-zero reading means the trigger loop isn't keeping up or has stalled.

    identity_health surfaces exactly what the ARM-time staleness gate
    itself checks, so a misconfigured/too-tight threshold silently
    producing zero trades is visible here instead of only as "no trades,
    no obvious reason" (colleague review) -- and it now also feeds the
    overall status/reasons verdict itself (see
    early_momentum_health.compute_status), not just the raw JSON: a stale
    catalog rejecting every candidate at ARM time used to leave `status`
    reading "ok" with zero explanation.

    v4 additionally surfaces the scanner/trigger worker heartbeats,
    per-exchange source-bar freshness, the last scanner tick's
    quality-gate counters, and an overall status/reasons verdict computed
    by the exact same `early_momentum.gather_health_status` the
    independent health monitor task uses for alerting -- the read path and
    the alerting path can never disagree about what the status is.
    """
    cfg = request.app.state.cfg
    if not cfg.db_url:
        return {"status": "disabled", "reason": "no db_url"}

    rdb = request.app.state.rdb
    startup_at = getattr(request.app.state, "early_momentum_startup_at", None) or datetime.now(
        tz=UTC
    )

    # gather_health_status already queries episodes.health_metrics and
    # episodes.identity_health once each as part of computing the verdict
    # -- reuse those same reads (raw["lifecycle_metrics"]/raw["identity_health"])
    # rather than querying either a second time, so the reported numbers
    # and the status they produced can never come from two different reads.
    status, reasons, raw = await early_momentum.gather_health_status(
        rdb, cfg, startup_at=startup_at
    )

    return {
        "status": status,
        "reasons": list(reasons),
        **raw["lifecycle_metrics"],
        "identity_health": raw["identity_health"],
        "scanner_heartbeat": _heartbeat_to_json(raw["scanner_heartbeat"]),
        "trigger_heartbeat": _heartbeat_to_json(raw["trigger_heartbeat"]),
        "source_freshness": raw["source_freshness"],
        "lifecycle_reaper_grace_seconds": raw["lifecycle_reaper_grace_seconds"],
        "last_successful_open_at": (
            raw["last_successful_open_at"].isoformat() if raw["last_successful_open_at"] else None
        ),
        "consecutive_zero_quality_ready_ticks": raw["consecutive_zero_quality_ready_ticks"],
        "quality_window_rewarming": raw["quality_window_rewarming"],
    }


@router.get("/health/workers")
async def get_workers_health(request: Request, response: Response) -> dict[str, Any]:
    supervisor = getattr(request.app.state, "supervisor", None)
    if not supervisor:
        response.status_code = 503
        return {"status": "unknown", "reason": "supervisor not initialized"}

    is_open, gen = supervisor.gate.is_open()
    status = "ok" if is_open else "failed"
    now = time.monotonic()

    workers = {}
    for name, w in supervisor.workers.items():
        w_state = w.state.value
        if w.spec.is_critical and (
            w_state in ("failed", "restart_exhausted") or w.tracker.consecutive_failures > 0
        ):
            status = "failed"
        elif (
            w_state in ("failed", "restart_exhausted") or w.tracker.consecutive_failures > 0
        ) and status != "failed":
            status = "degraded"

        workers[name] = {
            "state": w_state,
            "enabled": w.spec.enabled,
            "policy": w.spec.policy.value,
            "restarts": len(w.restarts),
            "last_started_at": w.tracker.last_started_at_utc.isoformat()
            if w.tracker.last_started_at_utc
            else None,
            "last_success_at": w.tracker.last_success_at_utc.isoformat()
            if w.tracker.last_success_at_utc
            else None,
            "last_error_at": w.tracker.last_error_at_utc.isoformat()
            if w.tracker.last_error_at_utc
            else None,
            "last_error": w.tracker.last_error,
            "consecutive_failures": w.tracker.consecutive_failures,
            "last_success_age_seconds": (
                max(0.0, now - w.tracker.last_success_at)
                if w.tracker.last_success_at is not None
                else None
            ),
        }

    if status == "failed":
        response.status_code = 503

    return {
        "status": status,
        "gate_open": is_open,
        "gate_generation": gen,
        "gate_reasons": supervisor.gate.get_reasons(),
        "workers": workers,
    }
