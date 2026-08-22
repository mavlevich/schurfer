from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from .. import episodes

router = APIRouter()

# Realistic source exchanges for early_momentum candidates today (binance
# leads, bybit is the execution venue itself -- also worth its own
# freshness reading). Not a dynamic query against recent episodes: keeping
# this fixed and small is simpler and cheap enough for a health check.
_IDENTITY_HEALTH_EXCHANGES = ["binance", "bybit"]


@router.get("/health/early-momentum")
async def get_early_momentum_health(request: Request) -> dict[str, Any]:
    """Episode-lifecycle observability: overdue armed episodes and expired
    claims should both stay near zero between reaper runs -- a sustained
    non-zero reading means the trigger loop isn't keeping up or has stalled.

    identity_health surfaces exactly what the ARM-time staleness gate
    itself checks, so a misconfigured/too-tight threshold silently
    producing zero trades is visible here instead of only as "no trades,
    no obvious reason" (colleague review).
    """
    cfg = request.app.state.cfg
    if not cfg.db_url:
        return {"status": "disabled", "reason": "no db_url"}

    metrics = await episodes.health_metrics(cfg.db_url)
    identity = await episodes.identity_health(
        cfg.db_url,
        exchanges=_IDENTITY_HEALTH_EXCHANGES,
        max_age_hours=cfg.identity_snapshot_max_age_hours,
    )
    # A DB error inside health_metrics comes back as all-None fields -- that
    # must read as "error", never as a healthy "ok" with blank numbers.
    status = "error" if metrics.get("overdue_armed") is None else "ok"
    return {"status": status, **metrics, "identity_health": identity}
