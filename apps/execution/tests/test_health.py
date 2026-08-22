"""Tests for routers/health.py -- episode-lifecycle observability."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from schurfer_execution.routers.health import get_early_momentum_health


def _request(db_url: str | None) -> MagicMock:
    req = MagicMock()
    req.app.state.cfg = MagicMock(db_url=db_url, identity_snapshot_max_age_hours=720.0)
    return req


async def test_health_reports_disabled_without_db_url() -> None:
    result = await get_early_momentum_health(_request(None))
    assert result == {"status": "disabled", "reason": "no db_url"}


async def test_health_reports_episode_metrics_and_identity_health() -> None:
    metrics = {
        "overdue_armed": 0,
        "expired_claims": 1,
        "identity_stale_rejections_last_hour": 0,
        "oldest_overdue_age_seconds": 12.5,
    }
    identity = {
        "binance": {"age_seconds": 3600.0, "stale": False},
        "bybit": {"age_seconds": None, "stale": True},
    }
    with (
        patch(
            "schurfer_execution.routers.health.episodes.health_metrics",
            AsyncMock(return_value=metrics),
        ) as health_metrics,
        patch(
            "schurfer_execution.routers.health.episodes.identity_health",
            AsyncMock(return_value=identity),
        ) as identity_health,
    ):
        result = await get_early_momentum_health(_request("postgresql://x"))

    health_metrics.assert_awaited_once_with("postgresql://x")
    identity_health.assert_awaited_once_with(
        "postgresql://x", exchanges=["binance", "bybit"], max_age_hours=720.0
    )
    assert result == {"status": "ok", **metrics, "identity_health": identity}


async def test_health_reports_error_status_when_metrics_are_none() -> None:
    """A DB error inside health_metrics comes back as all-None fields -- that
    must read as 'error', never as a healthy 'ok' with blank numbers."""
    metrics = {
        "overdue_armed": None,
        "expired_claims": None,
        "identity_stale_rejections_last_hour": None,
        "oldest_overdue_age_seconds": None,
    }
    with (
        patch(
            "schurfer_execution.routers.health.episodes.health_metrics",
            AsyncMock(return_value=metrics),
        ),
        patch(
            "schurfer_execution.routers.health.episodes.identity_health",
            AsyncMock(return_value={}),
        ),
    ):
        result = await get_early_momentum_health(_request("postgresql://x"))

    assert result["status"] == "error"
