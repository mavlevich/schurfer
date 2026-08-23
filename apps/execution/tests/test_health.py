"""Tests for routers/health.py -- episode-lifecycle observability."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from schurfer_execution.routers.health import get_early_momentum_health
from schurfer_execution.worker_health import STATE_COMPLETED, WorkerHeartbeat


def _request(db_url: str | None, *, rdb: MagicMock | None = None) -> MagicMock:
    req = MagicMock()
    req.app.state.cfg = MagicMock(db_url=db_url, identity_snapshot_max_age_hours=720.0)
    req.app.state.rdb = rdb or MagicMock()
    req.app.state.early_momentum_startup_at = datetime(2026, 8, 22, 0, 0, 0, tzinfo=UTC)
    return req


def _heartbeat() -> WorkerHeartbeat:
    now = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)
    return WorkerHeartbeat(
        worker_name="early_momentum_scanner",
        worker_version="4",
        state=STATE_COMPLETED,
        started_at=now,
        completed_at=now,
        duration_ms=500.0,
        counters={"candidates_found": 1},
    )


def _raw_metrics(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "scanner_heartbeat": _heartbeat(),
        "trigger_heartbeat": _heartbeat(),
        "source_freshness": {
            "bybit": {"latest_bucket": None, "lag_seconds": 65.0},
            "binance": {"latest_bucket": None, "lag_seconds": 70.0},
        },
        "identity_health": {
            "binance": {"age_seconds": 3600.0, "stale": False},
            "bybit": {"age_seconds": None, "stale": True},
        },
        "lifecycle_metrics": {
            "overdue_armed": 0,
            "expired_claims": 1,
            "identity_stale_rejections_last_hour": 0,
            "oldest_overdue_age_seconds": 12.5,
        },
        "last_successful_open_at": datetime(2026, 8, 22, 11, 0, 0, tzinfo=UTC),
        "consecutive_zero_quality_ready_ticks": 0,
    }
    fields.update(overrides)
    return fields


async def test_health_reports_disabled_without_db_url() -> None:
    result = await get_early_momentum_health(_request(None))
    assert result == {"status": "disabled", "reason": "no db_url"}


async def test_health_reports_episode_metrics_and_identity_health() -> None:
    raw = _raw_metrics()
    with patch(
        "schurfer_execution.routers.health.early_momentum.gather_health_status",
        AsyncMock(return_value=("ok", (), raw)),
    ) as gather:
        result = await get_early_momentum_health(_request("postgresql://x"))

    gather.assert_awaited_once()
    assert result["status"] == "ok"
    assert result["reasons"] == []
    assert result["overdue_armed"] == 0
    assert result["expired_claims"] == 1
    assert result["identity_health"] == raw["identity_health"]
    assert result["scanner_heartbeat"]["state"] == "completed"
    assert result["scanner_heartbeat"]["counters"] == {"candidates_found": 1}
    assert result["trigger_heartbeat"]["state"] == "completed"
    assert result["source_freshness"]["bybit"]["lag_seconds"] == 65.0
    assert result["last_successful_open_at"] == "2026-08-22T11:00:00+00:00"
    assert result["consecutive_zero_quality_ready_ticks"] == 0


async def test_health_reports_degraded_status_from_gather_health_status() -> None:
    raw = _raw_metrics()
    with patch(
        "schurfer_execution.routers.health.early_momentum.gather_health_status",
        AsyncMock(return_value=("degraded", ("overdue_armed_episodes",), raw)),
    ):
        result = await get_early_momentum_health(_request("postgresql://x"))

    assert result["status"] == "degraded"
    assert result["reasons"] == ["overdue_armed_episodes"]


async def test_health_reports_error_status_when_identity_catalog_is_stale() -> None:
    """Overall status must reflect a stale identity catalog, not just
    surface it in the raw JSON (colleague review) -- this is exercised via
    gather_health_status's own verdict here since routers/health.py just
    relays it verbatim."""
    raw = _raw_metrics()
    with patch(
        "schurfer_execution.routers.health.early_momentum.gather_health_status",
        AsyncMock(return_value=("degraded", ("identity_catalog_stale",), raw)),
    ):
        result = await get_early_momentum_health(_request("postgresql://x"))

    assert result["status"] != "ok"
    assert "identity_catalog_stale" in result["reasons"]


async def test_health_reports_missing_heartbeats_as_none() -> None:
    raw = _raw_metrics(scanner_heartbeat=None, trigger_heartbeat=None)
    with patch(
        "schurfer_execution.routers.health.early_momentum.gather_health_status",
        AsyncMock(return_value=("error", ("scanner_heartbeat_missing",), raw)),
    ):
        result = await get_early_momentum_health(_request("postgresql://x"))

    assert result["status"] == "error"
    assert result["scanner_heartbeat"] is None
    assert result["trigger_heartbeat"] is None


async def test_health_reports_none_last_successful_open_at() -> None:
    raw = _raw_metrics(last_successful_open_at=None)
    with patch(
        "schurfer_execution.routers.health.early_momentum.gather_health_status",
        AsyncMock(return_value=("ok", (), raw)),
    ):
        result = await get_early_momentum_health(_request("postgresql://x"))

    assert result["last_successful_open_at"] is None


async def test_health_passes_startup_at_and_rdb_through_to_gather_health_status() -> None:
    rdb = MagicMock()
    startup_at = datetime(2026, 8, 21, 0, 0, 0, tzinfo=UTC)
    req = _request("postgresql://x", rdb=rdb)
    req.app.state.early_momentum_startup_at = startup_at
    raw = _raw_metrics()

    with patch(
        "schurfer_execution.routers.health.early_momentum.gather_health_status",
        AsyncMock(return_value=("ok", (), raw)),
    ) as gather:
        await get_early_momentum_health(req)

    gather.assert_awaited_once_with(rdb, req.app.state.cfg, startup_at=startup_at)
