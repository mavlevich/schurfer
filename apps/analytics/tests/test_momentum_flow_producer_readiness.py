from __future__ import annotations

from datetime import UTC, datetime, timedelta

from schurfer_analytics.momentum_flow_producer_readiness import upstream_health_is_ready

NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)


def test_fresh_ok_is_ready() -> None:
    generated_at = (NOW - timedelta(seconds=5)).isoformat()
    assert upstream_health_is_ready(status="ok", generated_at=generated_at, now=NOW) is True


def test_exactly_at_max_age_is_still_ready() -> None:
    generated_at = (NOW - timedelta(seconds=60)).isoformat()
    assert (
        upstream_health_is_ready(
            status="ok", generated_at=generated_at, now=NOW, max_age_seconds=60.0
        )
        is True
    )


def test_stale_ok_is_not_ready() -> None:
    """The exact colleague-review finding: a hard-crashed process leaves
    its last "ok" sitting in Redis forever otherwise."""
    generated_at = (NOW - timedelta(minutes=10)).isoformat()
    assert upstream_health_is_ready(status="ok", generated_at=generated_at, now=NOW) is False


def test_non_ok_status_is_not_ready_even_if_fresh() -> None:
    generated_at = NOW.isoformat()
    for status in ("degraded", "starting", "blocked_upstream_incompatible", ""):
        assert upstream_health_is_ready(status=status, generated_at=generated_at, now=NOW) is False


def test_missing_status_is_not_ready() -> None:
    assert upstream_health_is_ready(status=None, generated_at=NOW.isoformat(), now=NOW) is False


def test_missing_generated_at_is_not_ready() -> None:
    assert upstream_health_is_ready(status="ok", generated_at=None, now=NOW) is False
    assert upstream_health_is_ready(status="ok", generated_at="", now=NOW) is False


def test_unparseable_generated_at_is_not_ready() -> None:
    assert upstream_health_is_ready(status="ok", generated_at="not-a-timestamp", now=NOW) is False


def test_generated_at_slightly_in_the_future_is_ready() -> None:
    """Ordinary clock jitter between containers on the same host, not
    evidence of anything stale -- only the upper bound matters."""
    generated_at = (NOW + timedelta(seconds=2)).isoformat()
    assert upstream_health_is_ready(status="ok", generated_at=generated_at, now=NOW) is True


def test_naive_generated_at_is_treated_as_utc() -> None:
    generated_at = (NOW - timedelta(seconds=5)).replace(tzinfo=None).isoformat()
    assert upstream_health_is_ready(status="ok", generated_at=generated_at, now=NOW) is True


def test_defaults_to_the_real_clock_when_now_is_not_supplied() -> None:
    generated_at = datetime.now(UTC).isoformat()
    assert upstream_health_is_ready(status="ok", generated_at=generated_at) is True
