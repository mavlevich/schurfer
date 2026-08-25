"""Tests for early_momentum_health.py's compute_status -- the one place
that decides ok/degraded/error, shared by the HTTP endpoint and the
independent monitor task."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from schurfer_execution.early_momentum_health import (
    REASON_EXPIRED_CLAIMS,
    REASON_IDENTITY_CATALOG_STALE,
    REASON_IDENTITY_HEALTH_UNAVAILABLE,
    REASON_LIFECYCLE_METRICS_UNAVAILABLE,
    REASON_OVERDUE_ARMED,
    REASON_QUALITY_READY_ZERO,
    REASON_QUALITY_READY_ZERO_SUSTAINED,
    REASON_QUALITY_WINDOW_REWARMING,
    REASON_SCANNER_HEARTBEAT_MISSING,
    REASON_SCANNER_HEARTBEAT_STALE,
    REASON_SCANNER_TICK_FAILED,
    REASON_SOURCE_BARS_STALE,
    REASON_TRIGGER_HEARTBEAT_MISSING,
    REASON_TRIGGER_HEARTBEAT_STALE,
    compute_status,
)
from schurfer_execution.worker_health import STATE_COMPLETED, STATE_FAILED, WorkerHeartbeat

NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)
STARTUP_LONG_AGO = NOW - timedelta(hours=1)


def _heartbeat(
    *, state: str = STATE_COMPLETED, completed_at: datetime | None = None
) -> WorkerHeartbeat:
    return WorkerHeartbeat(
        worker_name="scanner",
        worker_version="v4",
        state=state,
        started_at=(completed_at or NOW) - timedelta(seconds=1),
        completed_at=completed_at or NOW,
        duration_ms=1000.0,
        counters={},
    )


_LIFECYCLE_REAPER_GRACE_SECONDS = 120


def _call(**overrides: object) -> tuple[str, tuple[str, ...]]:
    fields: dict[str, object] = {
        "now": NOW,
        "startup_at": STARTUP_LONG_AGO,
        "grace_period_seconds": 180,
        "scanner_heartbeat": _heartbeat(),
        "trigger_heartbeat": _heartbeat(),
        "heartbeat_ttl_seconds": 360,
        "source_max_lag_seconds": {"bybit": 65.0, "binance": 70.0},
        "source_lag_limit_seconds": 180,
        "overdue_armed": 0,
        "oldest_overdue_armed_age_seconds": None,
        "expired_claims": 0,
        "oldest_expired_claim_age_seconds": None,
        "lifecycle_reaper_grace_seconds": _LIFECYCLE_REAPER_GRACE_SECONDS,
        "consecutive_zero_quality_ready_ticks": 0,
        "zero_quality_ready_error_threshold": 3,
        "quality_window_rewarming": False,
        "quality_window_rewarming_max_ticks": 141,
        "identity_health": {
            "binance": {"age_seconds": 60.0, "stale": False},
            "bybit": {"age_seconds": 60.0, "stale": False},
        },
    }
    fields.update(overrides)
    return compute_status(**fields)  # type: ignore[arg-type]


def test_healthy_inputs_report_ok_with_no_reasons() -> None:
    status, reasons = _call()
    assert status == "ok"
    assert reasons == ()


def test_missing_scanner_heartbeat_past_startup_grace_is_error() -> None:
    status, reasons = _call(scanner_heartbeat=None)
    assert status == "error"
    assert REASON_SCANNER_HEARTBEAT_MISSING in reasons


def test_missing_heartbeat_within_startup_grace_is_ok() -> None:
    status, reasons = _call(
        startup_at=NOW - timedelta(seconds=30), scanner_heartbeat=None, trigger_heartbeat=None
    )
    assert status == "ok"
    assert reasons == ()


def test_stale_scanner_heartbeat_is_error() -> None:
    status, reasons = _call(scanner_heartbeat=_heartbeat(completed_at=NOW - timedelta(seconds=500)))
    assert status == "error"
    assert REASON_SCANNER_HEARTBEAT_STALE in reasons


def test_trigger_heartbeat_missing_is_error() -> None:
    status, reasons = _call(trigger_heartbeat=None)
    assert status == "error"
    assert REASON_TRIGGER_HEARTBEAT_MISSING in reasons


def test_scanner_tick_failed_state_is_degraded_when_heartbeat_still_fresh() -> None:
    status, reasons = _call(scanner_heartbeat=_heartbeat(state=STATE_FAILED))
    assert status == "degraded"
    assert REASON_SCANNER_TICK_FAILED in reasons


def test_source_bars_stale_is_degraded() -> None:
    status, reasons = _call(source_max_lag_seconds={"bybit": 400.0, "binance": 70.0})
    assert status == "degraded"
    assert REASON_SOURCE_BARS_STALE in reasons


def test_source_lag_unknown_counts_as_stale() -> None:
    status, reasons = _call(source_max_lag_seconds={"bybit": None, "binance": 70.0})
    assert status == "degraded"
    assert REASON_SOURCE_BARS_STALE in reasons


def test_source_staleness_ignored_within_startup_grace() -> None:
    status, _reasons = _call(
        startup_at=NOW - timedelta(seconds=30), source_max_lag_seconds={"bybit": None}
    )
    assert status == "ok"


def test_lifecycle_metrics_unavailable_is_error() -> None:
    status, reasons = _call(overdue_armed=None, expired_claims=None)
    assert status == "error"
    assert REASON_LIFECYCLE_METRICS_UNAVAILABLE in reasons


def test_overdue_armed_episodes_past_grace_is_degraded() -> None:
    status, reasons = _call(overdue_armed=2, oldest_overdue_armed_age_seconds=200.0)
    assert status == "degraded"
    assert REASON_OVERDUE_ARMED in reasons


def test_expired_claims_past_grace_is_degraded() -> None:
    status, reasons = _call(expired_claims=1, oldest_expired_claim_age_seconds=200.0)
    assert status == "degraded"
    assert REASON_EXPIRED_CLAIMS in reasons


# ---- reaper-grace boundary behavior (this PR) ----


def test_overdue_armed_at_age_zero_is_ok() -> None:
    status, reasons = _call(overdue_armed=1, oldest_overdue_armed_age_seconds=0.0)
    assert status == "ok"
    assert REASON_OVERDUE_ARMED not in reasons


def test_overdue_armed_just_under_grace_is_ok() -> None:
    status, reasons = _call(overdue_armed=1, oldest_overdue_armed_age_seconds=119.999)
    assert status == "ok"
    assert REASON_OVERDUE_ARMED not in reasons


def test_overdue_armed_at_grace_boundary_is_degraded() -> None:
    status, reasons = _call(overdue_armed=1, oldest_overdue_armed_age_seconds=120.0)
    assert status == "degraded"
    assert REASON_OVERDUE_ARMED in reasons


def test_overdue_armed_past_grace_boundary_is_degraded() -> None:
    status, reasons = _call(overdue_armed=1, oldest_overdue_armed_age_seconds=121.0)
    assert status == "degraded"
    assert REASON_OVERDUE_ARMED in reasons


def test_expired_claims_at_age_zero_is_ok() -> None:
    status, reasons = _call(expired_claims=1, oldest_expired_claim_age_seconds=0.0)
    assert status == "ok"
    assert REASON_EXPIRED_CLAIMS not in reasons


def test_expired_claims_just_under_grace_is_ok() -> None:
    status, reasons = _call(expired_claims=1, oldest_expired_claim_age_seconds=119.999)
    assert status == "ok"
    assert REASON_EXPIRED_CLAIMS not in reasons


def test_expired_claims_at_grace_boundary_is_degraded() -> None:
    status, reasons = _call(expired_claims=1, oldest_expired_claim_age_seconds=120.0)
    assert status == "degraded"
    assert REASON_EXPIRED_CLAIMS in reasons


def test_expired_claims_past_grace_boundary_is_degraded() -> None:
    status, reasons = _call(expired_claims=1, oldest_expired_claim_age_seconds=121.0)
    assert status == "degraded"
    assert REASON_EXPIRED_CLAIMS in reasons


def test_overdue_armed_zero_count_ignores_a_stale_looking_age() -> None:
    """count == 0 means "nothing to measure" -- an age value must never be
    consulted when its own count says there's nothing overdue."""
    status, reasons = _call(overdue_armed=0, oldest_overdue_armed_age_seconds=999_999.0)
    assert status == "ok"
    assert REASON_OVERDUE_ARMED not in reasons


def test_expired_claims_zero_count_ignores_a_stale_looking_age() -> None:
    status, reasons = _call(expired_claims=0, oldest_expired_claim_age_seconds=999_999.0)
    assert status == "ok"
    assert REASON_EXPIRED_CLAIMS not in reasons


def test_overdue_armed_positive_count_with_missing_age_fails_closed() -> None:
    status, reasons = _call(overdue_armed=1, oldest_overdue_armed_age_seconds=None)
    assert status == "error"
    assert REASON_LIFECYCLE_METRICS_UNAVAILABLE in reasons


def test_expired_claims_positive_count_with_missing_age_fails_closed() -> None:
    status, reasons = _call(expired_claims=1, oldest_expired_claim_age_seconds=None)
    assert status == "error"
    assert REASON_LIFECYCLE_METRICS_UNAVAILABLE in reasons


def test_stale_trigger_heartbeat_is_still_an_error_regardless_of_lifecycle_grace() -> None:
    """The reaper grace period is scoped to overdue_armed/expired_claims
    only -- a genuinely stale heartbeat must never be softened by it."""
    status, reasons = _call(
        trigger_heartbeat=_heartbeat(completed_at=NOW - timedelta(seconds=500)),
        overdue_armed=1,
        oldest_overdue_armed_age_seconds=0.0,
    )
    assert status == "error"
    assert REASON_TRIGGER_HEARTBEAT_STALE in reasons


def test_quality_ready_zero_below_threshold_is_degraded_not_error() -> None:
    status, reasons = _call(consecutive_zero_quality_ready_ticks=2)
    assert status == "degraded"
    assert REASON_QUALITY_READY_ZERO in reasons
    assert REASON_QUALITY_READY_ZERO_SUSTAINED not in reasons


def test_quality_ready_zero_at_threshold_is_error() -> None:
    status, reasons = _call(consecutive_zero_quality_ready_ticks=3)
    assert status == "error"
    assert REASON_QUALITY_READY_ZERO_SUSTAINED in reasons
    assert REASON_QUALITY_READY_ZERO not in reasons


def test_mixed_universe_window_rewarming_is_degraded_not_error() -> None:
    status, reasons = _call(
        consecutive_zero_quality_ready_ticks=9,
        quality_window_rewarming=True,
    )
    assert status == "degraded"
    assert REASON_QUALITY_WINDOW_REWARMING in reasons
    assert REASON_QUALITY_READY_ZERO_SUSTAINED not in reasons


def test_window_rewarming_cannot_mask_zero_quality_forever() -> None:
    status, reasons = _call(
        consecutive_zero_quality_ready_ticks=142,
        quality_window_rewarming=True,
    )
    assert status == "error"
    assert REASON_QUALITY_READY_ZERO_SUSTAINED in reasons


def test_multiple_reasons_are_all_reported_and_deduped() -> None:
    status, reasons = _call(
        scanner_heartbeat=None,
        overdue_armed=3,
        oldest_overdue_armed_age_seconds=200.0,
        expired_claims=1,
        oldest_expired_claim_age_seconds=200.0,
    )
    assert status == "error"
    assert REASON_SCANNER_HEARTBEAT_MISSING in reasons
    assert REASON_OVERDUE_ARMED in reasons
    assert REASON_EXPIRED_CLAIMS in reasons
    assert len(reasons) == len(set(reasons))


def test_error_reason_wins_over_a_simultaneous_degraded_reason() -> None:
    status, _reasons = _call(
        scanner_heartbeat=None, overdue_armed=3, oldest_overdue_armed_age_seconds=200.0
    )
    assert status == "error"


def test_stale_bybit_identity_is_not_ok() -> None:
    status, reasons = _call(
        identity_health={
            "binance": {"age_seconds": 60.0, "stale": False},
            "bybit": {"age_seconds": 999_999.0, "stale": True},
        }
    )
    assert status != "ok"
    assert REASON_IDENTITY_CATALOG_STALE in reasons


def test_stale_binance_source_identity_is_not_ok() -> None:
    status, reasons = _call(
        identity_health={
            "binance": {"age_seconds": 999_999.0, "stale": True},
            "bybit": {"age_seconds": 60.0, "stale": False},
        }
    )
    assert status != "ok"
    assert REASON_IDENTITY_CATALOG_STALE in reasons


def test_both_identity_catalogs_fresh_does_not_worsen_status() -> None:
    status, reasons = _call(
        identity_health={
            "binance": {"age_seconds": 60.0, "stale": False},
            "bybit": {"age_seconds": 60.0, "stale": False},
        }
    )
    assert status == "ok"
    assert REASON_IDENTITY_CATALOG_STALE not in reasons


def test_identity_health_unavailable_fails_closed_as_error() -> None:
    status, reasons = _call(identity_health={})
    assert status == "error"
    assert REASON_IDENTITY_HEALTH_UNAVAILABLE in reasons
