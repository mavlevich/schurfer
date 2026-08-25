"""Tests for early_momentum.py's independent health monitor: heartbeat/DB
gathering, alert transition + cooldown + dedup logic, and the outer loop's
wiring. compute_status's own predicate logic is unit-tested in
test_early_momentum_health.py -- these tests mock it at the boundary and
focus on what gathers its inputs and what happens with its verdict.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fakeredis.aioredis import FakeRedis
from schurfer_execution import early_momentum, worker_health
from schurfer_execution.worker_health import STATE_COMPLETED, STATE_STARTED, WorkerHeartbeat

NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)


def _cfg(**overrides: object) -> MagicMock:
    cfg = MagicMock(
        db_url="postgresql://x",
        early_momentum_health_alert_cooldown_seconds=1800,
        telegram_bot_token="token",  # noqa: S106
        telegram_chat_id="chat",
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _heartbeat(
    *,
    state: str = STATE_COMPLETED,
    completed_at: datetime | None = NOW,
    counters: dict[str, int] | None = None,
) -> WorkerHeartbeat:
    return WorkerHeartbeat(
        worker_name="early_momentum_scanner",
        worker_version="4",
        state=state,
        started_at=NOW - timedelta(seconds=1),
        completed_at=completed_at,
        duration_ms=1000.0,
        counters=counters or {},
    )


# --- _update_zero_quality_ready_counter ---


async def test_zero_quality_ready_counter_increments_on_a_new_completed_bad_tick() -> None:
    rdb = MagicMock()
    rdb.eval = AsyncMock(return_value=1)
    heartbeat = _heartbeat(counters={"symbols_total": 100, "quality_ready": 0})

    result = await early_momentum._update_zero_quality_ready_counter(
        rdb, scanner_heartbeat=heartbeat
    )

    assert result == 1
    rdb.eval.assert_awaited_once()
    args = rdb.eval.call_args.args
    assert args[0] == early_momentum._COUNT_ZERO_QUALITY_READY_TICK_ONCE
    assert args[1] == 3
    assert args[5] == NOW.isoformat()
    assert args[6] == "1"
    assert args[7] == "0"


async def test_zero_quality_ready_counter_resets_on_a_new_completed_good_tick() -> None:
    rdb = MagicMock()
    rdb.eval = AsyncMock(return_value=0)
    heartbeat = _heartbeat(counters={"symbols_total": 100, "quality_ready": 5})

    result = await early_momentum._update_zero_quality_ready_counter(
        rdb, scanner_heartbeat=heartbeat
    )

    assert result == 0
    args = rdb.eval.call_args.args
    assert args[6] == "0"  # not a bad tick


async def test_zero_quality_ready_counter_marks_mixed_universe_rewarming() -> None:
    rdb = MagicMock()
    rdb.eval = AsyncMock(return_value=1)
    heartbeat = _heartbeat(
        counters={
            "symbols_total": 100,
            "quality_ready": 0,
            "rejected_multiple_universe_versions": 100,
        }
    )

    await early_momentum._update_zero_quality_ready_counter(rdb, scanner_heartbeat=heartbeat)

    args = rdb.eval.call_args.args
    assert args[6] == "1"
    assert args[7] == "1"


async def test_zero_quality_ready_rewarming_state_round_trips_through_redis() -> None:
    rdb = FakeRedis()
    heartbeat = _heartbeat(
        counters={
            "symbols_total": 100,
            "quality_ready": 0,
            "rejected_multiple_universe_versions": 100,
        }
    )

    count = await early_momentum._update_zero_quality_ready_counter(
        rdb, scanner_heartbeat=heartbeat
    )

    assert count == 1
    assert await early_momentum._read_quality_window_rewarming(rdb) is True


async def test_zero_quality_ready_counter_treats_zero_symbols_total_as_not_bad() -> None:
    """symbols_total==0 (scanner tick found nothing to evaluate) is not the
    same incident as symbols_total>0-but-quality_ready==0."""
    rdb = MagicMock()
    rdb.eval = AsyncMock(return_value=0)
    heartbeat = _heartbeat(counters={"symbols_total": 0, "quality_ready": 0})

    await early_momentum._update_zero_quality_ready_counter(rdb, scanner_heartbeat=heartbeat)

    args = rdb.eval.call_args.args
    assert args[6] == "0"


async def test_zero_quality_ready_counter_repeated_read_does_not_double_count() -> None:
    """gather_health_status runs on both the ~30s independent monitor tick
    AND every HTTP GET /health/early-momentum -- reading the exact same
    completed heartbeat twice must count it once (colleague review)."""
    counter = {"n": 0}
    last_marker: dict[str, str] = {}

    async def _fake_eval(_script: str, _numkeys: int, *args: object) -> int:
        _marker_key, _counter_key, _rewarming_key, marker, is_bad, _rewarming = args
        if last_marker.get("value") == marker:
            return counter["n"]
        last_marker["value"] = marker  # type: ignore[assignment]
        if is_bad == "1":
            counter["n"] += 1
        else:
            counter["n"] = 0
        return counter["n"]

    rdb = MagicMock()
    rdb.eval = AsyncMock(side_effect=_fake_eval)
    heartbeat = _heartbeat(counters={"symbols_total": 100, "quality_ready": 0})

    first = await early_momentum._update_zero_quality_ready_counter(
        rdb, scanner_heartbeat=heartbeat
    )
    second = await early_momentum._update_zero_quality_ready_counter(
        rdb, scanner_heartbeat=heartbeat
    )

    assert first == 1
    assert second == 1  # same tick re-read -- not incremented again
    assert rdb.eval.await_count == 2


async def test_zero_quality_ready_counter_a_new_completed_heartbeat_advances_it_again() -> None:
    rdb = MagicMock()
    rdb.eval = AsyncMock(side_effect=[1, 2])
    first_tick = _heartbeat(completed_at=NOW, counters={"symbols_total": 100, "quality_ready": 0})
    second_tick = _heartbeat(
        completed_at=NOW + timedelta(minutes=1), counters={"symbols_total": 100, "quality_ready": 0}
    )

    first = await early_momentum._update_zero_quality_ready_counter(
        rdb, scanner_heartbeat=first_tick
    )
    second = await early_momentum._update_zero_quality_ready_counter(
        rdb, scanner_heartbeat=second_tick
    )

    assert first == 1
    assert second == 2
    markers = [call.args[5] for call in rdb.eval.call_args_list]
    assert markers[0] != markers[1]


async def test_zero_quality_ready_counter_started_heartbeat_does_not_reset_progress() -> None:
    """A 'started' (still mid-tick) heartbeat must be ignored entirely --
    it must neither increment nor reset the count from the last completed
    tick."""
    rdb = MagicMock()
    rdb.get = AsyncMock(return_value=b"3")
    started = _heartbeat(state=STATE_STARTED, completed_at=None)

    result = await early_momentum._update_zero_quality_ready_counter(rdb, scanner_heartbeat=started)

    assert result == 3
    rdb.get.assert_awaited_once_with(early_momentum._HEALTH_ZERO_QUALITY_READY_COUNTER_KEY)


async def test_zero_quality_ready_counter_missing_heartbeat_reports_current_value() -> None:
    rdb = MagicMock()
    rdb.get = AsyncMock(return_value=None)

    result = await early_momentum._update_zero_quality_ready_counter(rdb, scanner_heartbeat=None)

    assert result == 0


async def test_zero_quality_ready_counter_fails_open_on_redis_error() -> None:
    rdb = MagicMock()
    rdb.eval = AsyncMock(side_effect=Exception("connection refused"))
    heartbeat = _heartbeat(counters={"symbols_total": 100, "quality_ready": 0})

    result = await early_momentum._update_zero_quality_ready_counter(
        rdb, scanner_heartbeat=heartbeat
    )

    assert result == 0


# --- gather_health_status ---


async def test_gather_health_status_wires_inputs_through_to_compute_status() -> None:
    rdb = MagicMock()
    cfg = _cfg()
    scanner_hb = _heartbeat(counters={"symbols_total": 50, "quality_ready": 10})
    trigger_hb = _heartbeat()
    identity = {
        "binance": {"age_seconds": 60.0, "stale": False},
        "bybit": {"age_seconds": 60.0, "stale": False},
    }

    with (
        patch(
            "schurfer_execution.early_momentum.worker_health.read_heartbeat",
            AsyncMock(side_effect=[scanner_hb, trigger_hb]),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.health_metrics",
            AsyncMock(
                return_value={
                    "overdue_armed": 1,
                    "oldest_overdue_armed_age_seconds": 20.0,
                    "expired_claims": 0,
                    "oldest_expired_claim_age_seconds": None,
                }
            ),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.source_freshness",
            AsyncMock(
                return_value={
                    "bybit": {"latest_bucket": None, "lag_seconds": 65.0},
                    "binance": {"latest_bucket": None, "lag_seconds": 70.0},
                }
            ),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.identity_health",
            AsyncMock(return_value=identity),
        ),
        patch(
            "schurfer_execution.early_momentum.journal.find_strategy_id", AsyncMock(return_value=7)
        ) as find_strategy_id,
        patch(
            "schurfer_execution.early_momentum.episodes.last_successful_open_at",
            AsyncMock(return_value=NOW),
        ),
        patch(
            "schurfer_execution.early_momentum._update_zero_quality_ready_counter",
            AsyncMock(return_value=0),
        ),
        patch(
            "schurfer_execution.early_momentum._read_quality_window_rewarming",
            AsyncMock(return_value=False),
        ),
        patch(
            "schurfer_execution.early_momentum.early_momentum_health.compute_status",
            MagicMock(return_value=("ok", ())),
        ) as compute_status,
    ):
        status, reasons, raw = await early_momentum.gather_health_status(
            rdb, cfg, startup_at=NOW - timedelta(hours=1)
        )

    assert status == "ok"
    assert reasons == ()
    assert raw["scanner_heartbeat"] is scanner_hb
    assert raw["trigger_heartbeat"] is trigger_hb
    assert raw["identity_health"] == identity
    assert raw["last_successful_open_at"] == NOW
    # Read-only lookup only -- never journal.ensure_strategy, which would
    # write updated_at = now() on every single health read.
    find_strategy_id.assert_awaited_once()
    compute_status.assert_called_once()
    kw = compute_status.call_args.kwargs
    assert kw["scanner_heartbeat"] is scanner_hb
    assert kw["trigger_heartbeat"] is trigger_hb
    assert kw["source_max_lag_seconds"] == {"bybit": 65.0, "binance": 70.0}
    assert kw["overdue_armed"] == 1
    assert kw["oldest_overdue_armed_age_seconds"] == 20.0
    assert kw["expired_claims"] == 0
    assert kw["oldest_expired_claim_age_seconds"] is None
    assert kw["lifecycle_reaper_grace_seconds"] == early_momentum._LIFECYCLE_REAPER_GRACE_SECONDS
    assert kw["quality_window_rewarming"] is False
    assert kw["identity_health"] == identity
    assert raw["lifecycle_reaper_grace_seconds"] == early_momentum._LIFECYCLE_REAPER_GRACE_SECONDS


async def test_gather_health_status_handles_strategy_lookup_failure() -> None:
    """find_strategy_id returning None (not registered yet, or a DB error)
    must not crash the gather -- last_successful_open_at is simply
    unavailable that tick."""
    rdb = MagicMock()
    cfg = _cfg()

    with (
        patch(
            "schurfer_execution.early_momentum.worker_health.read_heartbeat",
            AsyncMock(return_value=None),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.health_metrics",
            AsyncMock(return_value={"overdue_armed": None, "expired_claims": None}),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.source_freshness",
            AsyncMock(return_value={}),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.identity_health",
            AsyncMock(return_value={}),
        ),
        patch(
            "schurfer_execution.early_momentum.journal.find_strategy_id",
            AsyncMock(return_value=None),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.last_successful_open_at",
            new_callable=AsyncMock,
        ) as last_open,
        patch(
            "schurfer_execution.early_momentum._update_zero_quality_ready_counter",
            AsyncMock(return_value=0),
        ),
        patch(
            "schurfer_execution.early_momentum._read_quality_window_rewarming",
            AsyncMock(return_value=False),
        ),
    ):
        _status, _reasons, raw = await early_momentum.gather_health_status(
            rdb, cfg, startup_at=NOW - timedelta(hours=1)
        )

    last_open.assert_not_awaited()
    assert raw["last_successful_open_at"] is None


# --- _format_health_alert ---


def test_format_health_alert_recovery_message() -> None:
    text = early_momentum._format_health_alert(status="ok", reasons=(), recovered=True)
    assert "recovered" in text


def test_format_health_alert_includes_reasons() -> None:
    text = early_momentum._format_health_alert(
        status="degraded", reasons=("source_bars_stale",), recovered=False
    )
    assert "degraded" in text
    assert "source_bars_stale" in text


# --- _maybe_alert ---


async def test_maybe_alert_skips_without_telegram_credentials() -> None:
    rdb = MagicMock()
    cfg = _cfg(telegram_bot_token=None, telegram_chat_id=None)
    with patch(
        "schurfer_execution.early_momentum.notify.notify_alert", new_callable=AsyncMock
    ) as notify_alert:
        await early_momentum._maybe_alert(rdb, cfg, status="error", reasons=("x",))
    notify_alert.assert_not_awaited()


async def test_maybe_alert_sends_on_transition_into_degraded() -> None:
    rdb = MagicMock()
    rdb.get = AsyncMock(return_value=b"ok")
    rdb.set = AsyncMock()
    cfg = _cfg()
    with patch(
        "schurfer_execution.early_momentum.notify.notify_alert",
        AsyncMock(return_value=True),
    ) as notify_alert:
        await early_momentum._maybe_alert(rdb, cfg, status="degraded", reasons=("x",))
    notify_alert.assert_awaited_once()
    rdb.set.assert_any_call(early_momentum._HEALTH_ALERT_STATUS_KEY, "degraded")
    rdb.set.assert_any_call(
        f"{early_momentum._HEALTH_ALERT_COOLDOWN_KEY_PREFIX}degraded",
        "1",
        ex=cfg.early_momentum_health_alert_cooldown_seconds,
    )


async def test_maybe_alert_transition_arms_cooldown_before_next_monitor_tick() -> None:
    rdb = FakeRedis()
    await rdb.set(early_momentum._HEALTH_ALERT_STATUS_KEY, "ok")
    cfg = _cfg()
    with patch(
        "schurfer_execution.early_momentum.notify.notify_alert",
        AsyncMock(return_value=True),
    ) as notify_alert:
        await early_momentum._maybe_alert(rdb, cfg, status="degraded", reasons=("x",))
        await early_momentum._maybe_alert(rdb, cfg, status="degraded", reasons=("x",))

    assert notify_alert.await_count == 1


async def test_maybe_alert_sends_recovery_message_on_transition_to_ok() -> None:
    rdb = MagicMock()
    rdb.get = AsyncMock(return_value=b"error")
    rdb.set = AsyncMock()
    cfg = _cfg()
    with patch(
        "schurfer_execution.early_momentum.notify.notify_alert",
        AsyncMock(return_value=True),
    ) as notify_alert:
        await early_momentum._maybe_alert(rdb, cfg, status="ok", reasons=())
    text = notify_alert.call_args.kwargs["text"]
    assert "recovered" in text


async def test_maybe_alert_first_ever_tick_reporting_ok_is_not_a_recovery() -> None:
    """No previously-recorded status (a fresh process, first health tick
    ever) must not be reported as "recovered" -- nothing was broken to
    recover from."""
    rdb = MagicMock()
    rdb.get = AsyncMock(return_value=None)  # never recorded before
    rdb.set = AsyncMock()
    cfg = _cfg()
    with patch(
        "schurfer_execution.early_momentum.notify.notify_alert",
        AsyncMock(return_value=True),
    ) as notify_alert:
        await early_momentum._maybe_alert(rdb, cfg, status="ok", reasons=())
    notify_alert.assert_awaited_once()
    text = notify_alert.call_args.kwargs["text"]
    assert "recovered" not in text


async def test_maybe_alert_does_not_resend_within_cooldown_for_unchanged_bad_status() -> None:
    rdb = MagicMock()
    rdb.get = AsyncMock(return_value=b"degraded")  # same as current status -- no transition
    rdb.set = AsyncMock(return_value=None)  # NX fails: cooldown key already held
    cfg = _cfg()
    with patch(
        "schurfer_execution.early_momentum.notify.notify_alert", new_callable=AsyncMock
    ) as notify_alert:
        await early_momentum._maybe_alert(rdb, cfg, status="degraded", reasons=("x",))
    notify_alert.assert_not_awaited()


async def test_maybe_alert_resends_after_cooldown_expires_for_unchanged_bad_status() -> None:
    rdb = MagicMock()
    rdb.get = AsyncMock(return_value=b"degraded")
    rdb.set = AsyncMock(return_value=True)  # NX succeeds: cooldown had expired
    cfg = _cfg()
    with patch(
        "schurfer_execution.early_momentum.notify.notify_alert",
        AsyncMock(return_value=True),
    ) as notify_alert:
        await early_momentum._maybe_alert(rdb, cfg, status="degraded", reasons=("x",))
    notify_alert.assert_awaited_once()


async def test_maybe_alert_never_reminders_while_status_stays_ok() -> None:
    rdb = MagicMock()
    rdb.get = AsyncMock(return_value=b"ok")
    rdb.set = AsyncMock()
    cfg = _cfg()
    with patch(
        "schurfer_execution.early_momentum.notify.notify_alert", new_callable=AsyncMock
    ) as notify_alert:
        await early_momentum._maybe_alert(rdb, cfg, status="ok", reasons=())
    notify_alert.assert_not_awaited()
    # No transition and status is "ok" -- must never even touch the
    # cooldown key for "ok" (there's nothing to remind about).
    assert all(c.args[0] != early_momentum._HEALTH_ALERT_STATUS_KEY for c in rdb.set.call_args_list)


async def test_maybe_alert_delivery_exception_does_not_raise_and_does_not_record_status() -> None:
    rdb = MagicMock()
    rdb.get = AsyncMock(return_value=b"ok")
    rdb.set = AsyncMock()
    cfg = _cfg()
    with patch(
        "schurfer_execution.early_momentum.notify.notify_alert",
        AsyncMock(side_effect=Exception("telegram down")),
    ):
        # Must not raise -- alert delivery failure can never kill the monitor loop.
        await early_momentum._maybe_alert(rdb, cfg, status="degraded", reasons=("x",))
    # A failed/unconfirmed delivery must never be recorded as delivered --
    # otherwise the next tick sees "already alerted" and drops the alert
    # for good.
    assert all(c.args[0] != early_momentum._HEALTH_ALERT_STATUS_KEY for c in rdb.set.call_args_list)


async def test_maybe_alert_delivery_returning_false_does_not_record_status() -> None:
    rdb = MagicMock()
    rdb.get = AsyncMock(return_value=b"ok")
    rdb.set = AsyncMock()
    cfg = _cfg()
    with patch(
        "schurfer_execution.early_momentum.notify.notify_alert",
        AsyncMock(return_value=False),
    ):
        await early_momentum._maybe_alert(rdb, cfg, status="degraded", reasons=("x",))
    assert all(c.args[0] != early_momentum._HEALTH_ALERT_STATUS_KEY for c in rdb.set.call_args_list)


async def test_maybe_alert_releases_cooldown_reservation_when_delivery_fails() -> None:
    """A reminder's cooldown must not be consumed by a message that never
    actually went out -- otherwise the alert is lost for the entire
    cooldown window instead of being retried on the next tick."""
    rdb = MagicMock()
    rdb.get = AsyncMock(return_value=b"degraded")  # unchanged status -- reminder path
    rdb.set = AsyncMock(return_value=True)  # cooldown NX acquired
    rdb.delete = AsyncMock()
    cfg = _cfg()
    with patch(
        "schurfer_execution.early_momentum.notify.notify_alert",
        AsyncMock(return_value=False),
    ):
        await early_momentum._maybe_alert(rdb, cfg, status="degraded", reasons=("x",))
    cooldown_key = f"{early_momentum._HEALTH_ALERT_COOLDOWN_KEY_PREFIX}degraded"
    rdb.delete.assert_awaited_once_with(cooldown_key)


async def test_maybe_alert_state_read_failure_aborts_without_sending() -> None:
    rdb = MagicMock()
    rdb.get = AsyncMock(side_effect=Exception("connection refused"))
    cfg = _cfg()
    with patch(
        "schurfer_execution.early_momentum.notify.notify_alert", new_callable=AsyncMock
    ) as notify_alert:
        await early_momentum._maybe_alert(rdb, cfg, status="degraded", reasons=("x",))
    notify_alert.assert_not_awaited()


# --- end-to-end: lifecycle grace feeding real alerting (this PR) ---
#
# gather_health_status here runs its real compute_status (not mocked) and
# _maybe_alert runs for real against FakeRedis -- only the Postgres-touching
# episodes/journal calls are stubbed. This is the actual composition a
# reaper tick produces in production: compute_status's grace-boundary logic
# (unit-tested in test_early_momentum_health.py) feeding _maybe_alert's
# send/dedup/recovery logic (unit-tested above) end to end.


@contextlib.contextmanager
def _lifecycle_health_context(
    *, overdue_armed: int, oldest_overdue_armed_age_seconds: float | None
):
    with (
        patch(
            "schurfer_execution.early_momentum.episodes.health_metrics",
            AsyncMock(
                return_value={
                    "overdue_armed": overdue_armed,
                    "oldest_overdue_armed_age_seconds": oldest_overdue_armed_age_seconds,
                    "expired_claims": 0,
                    "oldest_expired_claim_age_seconds": None,
                }
            ),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.source_freshness",
            AsyncMock(return_value={"bybit": {"latest_bucket": None, "lag_seconds": 65.0}}),
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.identity_health",
            AsyncMock(
                return_value={"bybit": {"age_seconds": 60.0, "stale": False}},
            ),
        ),
        patch(
            "schurfer_execution.early_momentum.journal.find_strategy_id", AsyncMock(return_value=7)
        ),
        patch(
            "schurfer_execution.early_momentum.episodes.last_successful_open_at",
            AsyncMock(return_value=None),
        ),
    ):
        yield


async def _write_fresh_heartbeats(rdb: Any) -> None:
    # gather_health_status samples the real wall clock internally (it isn't
    # given `now`) -- these heartbeats must be fresh against *that*, not
    # against this file's fixed NOW constant (used only by the other,
    # compute_status-mocking tests above).
    real_now = datetime.now(tz=UTC)
    hb = WorkerHeartbeat(
        worker_name="early_momentum_scanner",
        worker_version="4",
        state=STATE_COMPLETED,
        started_at=real_now - timedelta(seconds=1),
        completed_at=real_now,
        duration_ms=1000.0,
        counters={"symbols_total": 50, "quality_ready": 10},
    )
    await worker_health.write_heartbeat(
        rdb, key=early_momentum._SCANNER_HEARTBEAT_KEY, heartbeat=hb, ttl_seconds=360
    )
    await worker_health.write_heartbeat(
        rdb, key=early_momentum._TRIGGER_HEARTBEAT_KEY, heartbeat=hb, ttl_seconds=360
    )


async def test_overdue_within_grace_reports_ok_and_sends_no_alert() -> None:
    rdb = FakeRedis()
    await _write_fresh_heartbeats(rdb)
    # An already-running system whose last known status was ok -- the case
    # this test cares about (a routine tick must not alert), distinct from
    # the very-first-tick-ever baseline case covered by
    # test_maybe_alert_first_ever_tick_reporting_ok_is_not_a_recovery.
    await rdb.set(early_momentum._HEALTH_ALERT_STATUS_KEY, "ok")
    cfg = _cfg()

    with (
        _lifecycle_health_context(overdue_armed=1, oldest_overdue_armed_age_seconds=20.0),
        patch(
            "schurfer_execution.early_momentum.notify.notify_alert", new_callable=AsyncMock
        ) as notify_alert,
    ):
        status, reasons, _raw = await early_momentum.gather_health_status(
            rdb, cfg, startup_at=NOW - timedelta(hours=1)
        )
        await early_momentum._maybe_alert(rdb, cfg, status=status, reasons=reasons)

    assert status == "ok"
    notify_alert.assert_not_awaited()


async def test_overdue_past_grace_reports_degraded_and_sends_one_alert() -> None:
    rdb = FakeRedis()
    await _write_fresh_heartbeats(rdb)
    cfg = _cfg()

    with (
        _lifecycle_health_context(overdue_armed=1, oldest_overdue_armed_age_seconds=200.0),
        patch(
            "schurfer_execution.early_momentum.notify.notify_alert",
            AsyncMock(return_value=True),
        ) as notify_alert,
    ):
        status, reasons, _raw = await early_momentum.gather_health_status(
            rdb, cfg, startup_at=NOW - timedelta(hours=1)
        )
        await early_momentum._maybe_alert(rdb, cfg, status=status, reasons=reasons)

    assert status == "degraded"
    assert "overdue_armed_episodes" in reasons
    notify_alert.assert_awaited_once()


async def test_recovery_after_a_real_degraded_phase_alerts_exactly_once() -> None:
    rdb = FakeRedis()
    await _write_fresh_heartbeats(rdb)
    cfg = _cfg()

    with patch(
        "schurfer_execution.early_momentum.notify.notify_alert",
        AsyncMock(return_value=True),
    ) as notify_alert:
        with _lifecycle_health_context(overdue_armed=1, oldest_overdue_armed_age_seconds=200.0):
            status, reasons, _raw = await early_momentum.gather_health_status(
                rdb, cfg, startup_at=NOW - timedelta(hours=1)
            )
            await early_momentum._maybe_alert(rdb, cfg, status=status, reasons=reasons)
        assert status == "degraded"
        assert notify_alert.await_count == 1

        # The reaper has since caught up -- overdue count back to zero.
        with _lifecycle_health_context(overdue_armed=0, oldest_overdue_armed_age_seconds=None):
            status, reasons, _raw = await early_momentum.gather_health_status(
                rdb, cfg, startup_at=NOW - timedelta(hours=1)
            )
            await early_momentum._maybe_alert(rdb, cfg, status=status, reasons=reasons)
        assert status == "ok"
        assert notify_alert.await_count == 2
        recovery_text = notify_alert.call_args.kwargs["text"]
        assert "recovered" in recovery_text

        # A further ok tick must not resend the recovery message again.
        with _lifecycle_health_context(overdue_armed=0, oldest_overdue_armed_age_seconds=None):
            status, reasons, _raw = await early_momentum.gather_health_status(
                rdb, cfg, startup_at=NOW - timedelta(hours=1)
            )
            await early_momentum._maybe_alert(rdb, cfg, status=status, reasons=reasons)
        assert status == "ok"
        assert notify_alert.await_count == 2


async def test_ordinary_overdue_within_grace_ticks_never_pair_degraded_with_recovered() -> None:
    """A normal reaper cadence (always within grace) must never itself
    manufacture a degraded->recovered alert pair -- status stays ok every
    tick, so _maybe_alert never even sees a transition."""
    rdb = FakeRedis()
    await _write_fresh_heartbeats(rdb)
    await rdb.set(early_momentum._HEALTH_ALERT_STATUS_KEY, "ok")
    cfg = _cfg()

    with (
        _lifecycle_health_context(overdue_armed=1, oldest_overdue_armed_age_seconds=20.0),
        patch(
            "schurfer_execution.early_momentum.notify.notify_alert", new_callable=AsyncMock
        ) as notify_alert,
    ):
        for _ in range(3):
            status, reasons, _raw = await early_momentum.gather_health_status(
                rdb, cfg, startup_at=NOW - timedelta(hours=1)
            )
            await early_momentum._maybe_alert(rdb, cfg, status=status, reasons=reasons)
            assert status == "ok"

    notify_alert.assert_not_awaited()


# --- outer loop ---


async def test_health_monitor_disabled_without_db_url() -> None:
    rdb = MagicMock()
    with patch(
        "schurfer_execution.early_momentum._health_monitor_tick", new_callable=AsyncMock
    ) as tick:
        await early_momentum.run_early_momentum_health_monitor(
            rdb, _cfg(db_url=None), startup_at=NOW
        )
    tick.assert_not_awaited()


async def test_health_monitor_ticks_then_sleeps_and_survives_exceptions() -> None:
    rdb = MagicMock()
    with (
        patch(
            "schurfer_execution.early_momentum._health_monitor_tick",
            AsyncMock(side_effect=RuntimeError("boom")),
        ) as tick,
        patch(
            "schurfer_execution.early_momentum.asyncio.sleep",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await early_momentum.run_early_momentum_health_monitor(rdb, _cfg(), startup_at=NOW)

    tick.assert_awaited_once()


async def test_health_monitor_tick_calls_gather_then_maybe_alert() -> None:
    rdb = MagicMock()
    cfg = _cfg()
    with (
        patch(
            "schurfer_execution.early_momentum.gather_health_status",
            AsyncMock(return_value=("degraded", ("x",), {})),
        ) as gather,
        patch(
            "schurfer_execution.early_momentum._maybe_alert", new_callable=AsyncMock
        ) as maybe_alert,
    ):
        await early_momentum._health_monitor_tick(rdb, cfg, startup_at=NOW)

    gather.assert_awaited_once_with(rdb, cfg, startup_at=NOW)
    maybe_alert.assert_awaited_once_with(rdb, cfg, status="degraded", reasons=("x",))
