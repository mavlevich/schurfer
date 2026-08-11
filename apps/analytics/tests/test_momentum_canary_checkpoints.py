import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest


def _load_module() -> ModuleType:
    path = Path(__file__).parents[3] / "infra" / "scripts" / "momentum_canary_checkpoints.py"
    spec = importlib.util.spec_from_file_location("momentum_canary_checkpoints_under_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load checkpoint runner from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


canary = _load_module()

EPOCH_START = datetime(2026, 8, 10, 19, 5, 41, 810000, tzinfo=UTC)
EPOCH_START_MS = int(EPOCH_START.timestamp() * 1000)


def _health(started_at_ms: int = EPOCH_START_MS) -> dict[str, str]:
    return {
        "started_at_ms": str(started_at_ms),
        "status": "healthy",
        "bars_completed_total": "130787",
        "symbols_missing_ticker_count": "0",
        "symbols_missing_trades_count": "4",
        "persist_retries_total": "0",
        "nats_dropped_total": "0",
        "late_events_total": "0",
        "writer_queue_depth": "0",
    }


def _storage(
    total_bytes: int = 120,
    row_count: int = 130787,
    *,
    after_compression_total_bytes: int = 100,
    before_compression_total_bytes: int = 600,
) -> dict[str, int]:
    return {
        "table_bytes": 100,
        "index_bytes": 20,
        "toast_bytes": 0,
        "total_bytes": total_bytes,
        "total_chunks": 3,
        "compressed_chunks": 1,
        "uncompressed_chunks": 2,
        "before_compression_total_bytes": before_compression_total_bytes,
        "after_compression_total_bytes": after_compression_total_bytes,
        "row_count": row_count,
    }


def _fake_collection(
    monkeypatch: pytest.MonkeyPatch, *, total_bytes: int = 120, row_count: int = 130787
) -> None:
    monkeypatch.setattr(
        canary,
        "_docker_stats",
        lambda *_args, **_kwargs: {"MemUsage": "28MiB / 512MiB", "CPUPerc": "12%"},
    )
    monkeypatch.setattr(
        canary, "_hypertable_storage", lambda timeout=30: _storage(total_bytes, row_count)
    )
    monkeypatch.setattr(canary, "_swap_used_mb", lambda: 0)
    monkeypatch.setattr(canary, "_swap_activity_counters", lambda: {"pswpin": 0, "pswpout": 0})


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now: datetime, **kwargs):
    snapshot = kwargs.pop("snapshot_path", None) or tmp_path / "runtime" / "momentum-canary.json"
    return canary.run_once(now=now, snapshot_path=snapshot, **kwargs), snapshot


# --- _read_momentum_health ---


def test_read_momentum_health_rejects_odd_hgetall_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(canary, "_run", lambda *_args, **_kwargs: "a\n1\nb\n")
    with pytest.raises(RuntimeError, match="odd number"):
        canary._read_momentum_health()


def test_read_momentum_health_rejects_empty_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(canary, "_run", lambda *_args, **_kwargs: "")
    with pytest.raises(RuntimeError, match="empty or missing"):
        canary._read_momentum_health()


def test_read_momentum_health_pairs_lines_into_a_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        canary,
        "_run",
        lambda *_args, **_kwargs: "started_at_ms\n123\nbars_completed_total\n5\n",
    )
    assert canary._read_momentum_health() == {"started_at_ms": "123", "bars_completed_total": "5"}


# --- storage: chunk-aware, not pg_relation_size ---


def test_hypertable_storage_uses_chunk_aware_functions_not_pg_relation_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []

    def fake_psql_csv(query: str, *, timeout: int) -> list[str]:
        queries.append(query)
        if "hypertable_detailed_size" in query:
            return ["100", "20", "0", "120"]
        if "hypertable_compression_stats" in query:
            return ["3", "1", "600", "100"]
        return ["130787"]

    monkeypatch.setattr(canary, "_psql_csv", fake_psql_csv)
    storage = canary._hypertable_storage()

    assert storage["total_bytes"] == 120
    assert storage["row_count"] == 130787
    assert not any("pg_relation_size" in q for q in queries)
    assert any("hypertable_detailed_size" in q for q in queries)
    assert any("hypertable_compression_stats" in q for q in queries)


# --- basic firing ---


def test_no_checkpoint_fires_before_24h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    _fake_collection(monkeypatch)
    notified: list[str] = []
    monkeypatch.setattr(canary, "_notify", lambda message: notified.append(message) or None)

    payload, _ = _run(tmp_path, monkeypatch, EPOCH_START + timedelta(hours=10))

    assert payload["active_epoch"]["checkpoints"]["24"]["state"] == "pending"
    # baseline is captured immediately, so exactly one notify-free collection happens
    assert notified == []


def test_24h_checkpoint_fires_once_and_is_not_refired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    _fake_collection(monkeypatch)
    notified: list[str] = []
    monkeypatch.setattr(canary, "_notify", lambda message: notified.append(message) or None)

    payload, snapshot = _run(tmp_path, monkeypatch, EPOCH_START + timedelta(hours=24, minutes=5))
    assert payload["active_epoch"]["checkpoints"]["24"]["state"] == "notified"
    assert len(notified) == 1
    assert "24h" in notified[0]

    payload2, _ = _run(
        tmp_path, monkeypatch, EPOCH_START + timedelta(hours=24, minutes=20), snapshot_path=snapshot
    )
    assert payload2["active_epoch"]["checkpoints"]["24"]["state"] == "notified"
    assert len(notified) == 1


def test_all_three_checkpoints_fire_in_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    _fake_collection(monkeypatch)
    notified: list[str] = []
    monkeypatch.setattr(canary, "_notify", lambda message: notified.append(message) or None)

    snapshot = tmp_path / "runtime" / "momentum-canary.json"
    canary.run_once(now=EPOCH_START + timedelta(hours=25), snapshot_path=snapshot)
    canary.run_once(now=EPOCH_START + timedelta(hours=49), snapshot_path=snapshot)
    final = canary.run_once(now=EPOCH_START + timedelta(hours=73), snapshot_path=snapshot)

    states = {k: v["state"] for k, v in final["active_epoch"]["checkpoints"].items()}
    assert states == {"24": "notified", "48": "notified", "72": "notified"}
    assert len(notified) == 3


# --- fix 1: restart archives, does not wipe ---


def test_restart_archives_previous_epoch_instead_of_wiping_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_collection(monkeypatch)
    notified: list[str] = []
    monkeypatch.setattr(canary, "_notify", lambda message: notified.append(message) or None)

    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    snapshot = tmp_path / "runtime" / "momentum-canary.json"
    canary.run_once(now=EPOCH_START + timedelta(hours=25), snapshot_path=snapshot)

    new_start = EPOCH_START + timedelta(hours=70)
    new_start_ms = int(new_start.timestamp() * 1000)
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health(new_start_ms))
    payload = canary.run_once(now=new_start + timedelta(hours=1), snapshot_path=snapshot)

    assert payload["active_epoch"]["started_at_ms"] == new_start_ms
    assert payload["active_epoch"]["checkpoints"]["24"]["state"] == "pending"
    assert len(payload["archived_epochs"]) == 1
    archived = payload["archived_epochs"][0]
    assert archived["archived_reason"] == "restart_detected"
    assert archived["checkpoints"]["24"]["state"] == "notified"  # evidence preserved
    assert any("restarted mid-canary" in message for message in notified)


# --- fix 2: telegram failure does not permanently lose a checkpoint ---


def test_telegram_failure_keeps_checkpoint_collected_and_retries_without_recollecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    collection_calls = {"n": 0}
    real_collect = canary.collect_checkpoint

    def counting_collect(*args, **kwargs):
        collection_calls["n"] += 1
        return real_collect(*args, **kwargs)

    monkeypatch.setattr(canary, "collect_checkpoint", counting_collect)
    _fake_collection(monkeypatch)

    notify_should_fail = {"value": True}
    monkeypatch.setattr(
        canary,
        "_notify",
        lambda message: "telegram delivery failed" if notify_should_fail["value"] else None,
    )

    snapshot = tmp_path / "runtime" / "momentum-canary.json"
    payload = canary.run_once(now=EPOCH_START + timedelta(hours=25), snapshot_path=snapshot)
    # baseline collection (1) + 24h checkpoint collection (1) = 2 so far
    assert payload["active_epoch"]["checkpoints"]["24"]["state"] == "collected"
    assert payload["active_epoch"]["checkpoints"]["24"]["notify_error"] is not None
    calls_after_first_failure = collection_calls["n"]

    # Telegram still down: retry must not re-collect.
    payload = canary.run_once(
        now=EPOCH_START + timedelta(hours=25, minutes=10), snapshot_path=snapshot
    )
    assert payload["active_epoch"]["checkpoints"]["24"]["state"] == "collected"
    assert collection_calls["n"] == calls_after_first_failure

    # Telegram recovers: retry succeeds, state promotes to notified, still no re-collection.
    notify_should_fail["value"] = False
    payload = canary.run_once(
        now=EPOCH_START + timedelta(hours=25, minutes=20), snapshot_path=snapshot
    )
    assert payload["active_epoch"]["checkpoints"]["24"]["state"] == "notified"
    assert collection_calls["n"] == calls_after_first_failure


def test_collection_failure_alert_itself_retries_on_telegram_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    monkeypatch.setattr(canary, "_swap_used_mb", lambda: 0)
    monkeypatch.setattr(canary, "_swap_activity_counters", lambda: {"pswpin": 0, "pswpout": 0})
    monkeypatch.setattr(canary, "_hypertable_storage", lambda timeout=30: _storage())
    monkeypatch.setattr(
        canary,
        "_docker_stats",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("docker daemon unreachable")),
    )
    sent: list[str] = []
    fail_notify = {"value": True}
    monkeypatch.setattr(
        canary,
        "_notify",
        lambda message: (sent.append(message), "fail" if fail_notify["value"] else None)[1],
    )

    snapshot = tmp_path / "runtime" / "momentum-canary.json"
    payload = canary.run_once(now=EPOCH_START + timedelta(hours=25), snapshot_path=snapshot)
    assert payload["active_epoch"]["checkpoints"]["24"]["state"] == "pending"
    assert payload["active_epoch"]["checkpoints"]["24"]["collection_error_notified"] is False
    first_len = len(sent)

    # Still failing to collect AND to notify: must retry the alert, not give up silently.
    canary.run_once(now=EPOCH_START + timedelta(hours=25, minutes=10), snapshot_path=snapshot)
    assert len(sent) > first_len

    fail_notify["value"] = False
    payload = canary.run_once(
        now=EPOCH_START + timedelta(hours=25, minutes=20), snapshot_path=snapshot
    )
    assert payload["active_epoch"]["checkpoints"]["24"]["collection_error_notified"] is True


# --- fix 3: health-read failure during an active epoch alerts once, recovers once ---


def test_health_unreadable_during_active_epoch_alerts_and_marks_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_collection(monkeypatch)
    notified: list[str] = []
    monkeypatch.setattr(canary, "_notify", lambda message: notified.append(message) or None)

    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    snapshot = tmp_path / "runtime" / "momentum-canary.json"
    canary.run_once(now=EPOCH_START + timedelta(hours=1), snapshot_path=snapshot)
    assert notified == []  # nothing due yet, baseline collection is silent

    def broken_health() -> dict[str, str]:
        raise RuntimeError("market:momentumcapture:health is empty or missing in redis")

    monkeypatch.setattr(canary, "_read_momentum_health", broken_health)
    with pytest.raises(RuntimeError):
        canary.run_once(now=EPOCH_START + timedelta(hours=2), snapshot_path=snapshot)
    assert len(notified) == 1
    assert "unreadable" in notified[0]
    payload = canary._read_json(snapshot)
    assert payload["active_epoch"]["status"] == "interrupted"

    # Still broken: must not spam a second alert.
    with pytest.raises(RuntimeError):
        canary.run_once(now=EPOCH_START + timedelta(hours=2, minutes=15), snapshot_path=snapshot)
    assert len(notified) == 1

    # Recovers: exactly one recovery alert, status back to running.
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    payload = canary.run_once(
        now=EPOCH_START + timedelta(hours=2, minutes=30), snapshot_path=snapshot
    )
    assert payload["active_epoch"]["status"] == "running"
    assert len(notified) == 2
    assert "again after an interruption" in notified[1]


def test_health_unreadable_with_no_prior_epoch_does_not_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notified: list[str] = []
    monkeypatch.setattr(canary, "_notify", lambda message: notified.append(message) or None)

    def broken_health() -> dict[str, str]:
        raise RuntimeError("market:momentumcapture:health is empty or missing in redis")

    monkeypatch.setattr(canary, "_read_momentum_health", broken_health)
    snapshot = tmp_path / "runtime" / "momentum-canary.json"
    with pytest.raises(RuntimeError):
        canary.run_once(now=EPOCH_START, snapshot_path=snapshot)
    assert notified == []  # nothing was ever running, so nothing "went missing"


# --- fix 7: operational alerts (interrupted/recovered/restart) durably retry too ---


def test_interrupted_alert_survives_a_telegram_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_collection(monkeypatch)
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    snapshot = tmp_path / "runtime" / "momentum-canary.json"
    canary.run_once(now=EPOCH_START + timedelta(hours=1), snapshot_path=snapshot)

    notified: list[str] = []
    fail = {"value": True}
    monkeypatch.setattr(
        canary,
        "_notify",
        lambda message: (notified.append(message), "telegram down" if fail["value"] else None)[1],
    )

    def broken_health() -> dict[str, str]:
        raise RuntimeError("market:momentumcapture:health is empty or missing in redis")

    monkeypatch.setattr(canary, "_read_momentum_health", broken_health)
    with pytest.raises(RuntimeError):
        canary.run_once(now=EPOCH_START + timedelta(hours=2), snapshot_path=snapshot)
    payload = canary._read_json(snapshot)
    assert payload["active_epoch"]["status"] == "interrupted"
    assert len(payload["pending_operational_alerts"]) == 1
    first_attempt_count = len(notified)

    # Telegram recovers, but health is still broken: the queued interrupted
    # alert must be retried and finally delivered without needing a new
    # interruption event.
    fail["value"] = False
    with pytest.raises(RuntimeError):
        canary.run_once(now=EPOCH_START + timedelta(hours=2, minutes=15), snapshot_path=snapshot)
    payload = canary._read_json(snapshot)
    assert payload["pending_operational_alerts"] == []
    assert len(notified) > first_attempt_count


def test_restart_alert_survives_a_telegram_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_collection(monkeypatch)
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    snapshot = tmp_path / "runtime" / "momentum-canary.json"
    canary.run_once(now=EPOCH_START + timedelta(hours=25), snapshot_path=snapshot)

    monkeypatch.setattr(canary, "_notify", lambda message: "telegram down")
    new_start = EPOCH_START + timedelta(hours=70)
    new_start_ms = int(new_start.timestamp() * 1000)
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health(new_start_ms))
    payload = canary.run_once(now=new_start + timedelta(hours=1), snapshot_path=snapshot)
    assert len(payload["pending_operational_alerts"]) == 1
    assert "restarted mid-canary" in payload["pending_operational_alerts"][0]["message"]

    notified: list[str] = []
    monkeypatch.setattr(canary, "_notify", lambda message: notified.append(message) or None)
    payload = canary.run_once(
        now=new_start + timedelta(hours=1, minutes=15), snapshot_path=snapshot
    )
    assert payload["pending_operational_alerts"] == []
    assert any("restarted mid-canary" in message for message in notified)


def test_recovery_alert_survives_a_telegram_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_collection(monkeypatch)
    snapshot = tmp_path / "runtime" / "momentum-canary.json"

    # Get into an interrupted state first (Telegram up for this part).
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    monkeypatch.setattr(canary, "_notify", lambda message: None)
    canary.run_once(now=EPOCH_START + timedelta(hours=1), snapshot_path=snapshot)

    def broken_health() -> dict[str, str]:
        raise RuntimeError("market:momentumcapture:health is empty or missing in redis")

    monkeypatch.setattr(canary, "_read_momentum_health", broken_health)
    with pytest.raises(RuntimeError):
        canary.run_once(now=EPOCH_START + timedelta(hours=2), snapshot_path=snapshot)
    assert canary._read_json(snapshot)["active_epoch"]["status"] == "interrupted"

    # Health comes back, but Telegram is down for the recovery alert itself.
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    monkeypatch.setattr(canary, "_notify", lambda message: "telegram down")
    payload = canary.run_once(
        now=EPOCH_START + timedelta(hours=2, minutes=15), snapshot_path=snapshot
    )
    assert payload["active_epoch"]["status"] == "running"  # state transition is not lost...
    assert len(payload["pending_operational_alerts"]) == 1
    assert "again after an interruption" in payload["pending_operational_alerts"][0]["message"]

    # Telegram recovers on a later run: the queued recovery alert is finally delivered.
    notified: list[str] = []
    monkeypatch.setattr(canary, "_notify", lambda message: notified.append(message) or None)
    payload = canary.run_once(
        now=EPOCH_START + timedelta(hours=2, minutes=30), snapshot_path=snapshot
    )
    assert payload["pending_operational_alerts"] == []
    assert any("again after an interruption" in message for message in notified)


def test_restart_while_interrupted_sends_restart_alert_only_no_false_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_collection(monkeypatch)
    snapshot = tmp_path / "runtime" / "momentum-canary.json"

    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    monkeypatch.setattr(canary, "_notify", lambda message: None)
    canary.run_once(now=EPOCH_START + timedelta(hours=1), snapshot_path=snapshot)

    def broken_health() -> dict[str, str]:
        raise RuntimeError("market:momentumcapture:health is empty or missing in redis")

    monkeypatch.setattr(canary, "_read_momentum_health", broken_health)
    with pytest.raises(RuntimeError):
        canary.run_once(now=EPOCH_START + timedelta(hours=2), snapshot_path=snapshot)
    assert canary._read_json(snapshot)["active_epoch"]["status"] == "interrupted"

    # Health returns, but with a DIFFERENT started_at_ms: the old epoch never
    # actually resumed, it was restarted. Must not also claim "recovered".
    notified: list[str] = []
    monkeypatch.setattr(canary, "_notify", lambda message: notified.append(message) or None)
    new_start = EPOCH_START + timedelta(hours=2, minutes=5)
    new_start_ms = int(new_start.timestamp() * 1000)
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health(new_start_ms))
    payload = canary.run_once(now=new_start + timedelta(minutes=1), snapshot_path=snapshot)

    assert payload["active_epoch"]["started_at_ms"] == new_start_ms
    assert payload["active_epoch"]["status"] == "running"
    assert not any("again after an interruption" in message for message in notified)
    assert any("restarted mid-canary" in message for message in notified)
    assert payload["archived_epochs"][-1]["status"] == "interrupted"
    # The archived epoch was still interrupted at restart time; the restart
    # alert should say so instead of pretending it recovered first.
    restart_message = next(m for m in notified if "restarted mid-canary" in m)
    assert "never actually resumed" in restart_message


# --- fix 4: baseline + delta/rate ---


def test_baseline_is_captured_once_at_epoch_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    _fake_collection(monkeypatch, total_bytes=100)
    monkeypatch.setattr(canary, "_notify", lambda message: None)

    snapshot = tmp_path / "runtime" / "momentum-canary.json"
    payload = canary.run_once(now=EPOCH_START, snapshot_path=snapshot)
    assert payload["active_epoch"]["baseline"]["timescale_storage"]["total_bytes"] == 100
    baseline_collected_at = payload["active_epoch"]["baseline_collected_at"]

    # A later run must not recapture the baseline.
    _fake_collection(monkeypatch, total_bytes=999)
    payload = canary.run_once(now=EPOCH_START + timedelta(hours=1), snapshot_path=snapshot)
    assert payload["active_epoch"]["baseline"]["timescale_storage"]["total_bytes"] == 100
    assert payload["active_epoch"]["baseline_collected_at"] == baseline_collected_at


def test_notification_text_reports_delta_and_rate_since_baseline() -> None:
    # after_compression_total_bytes held constant (no chunk newly compressed
    # yet) so the whole 96 MiB growth attributes to the hot bucket -- the same
    # scenario the pre-fix single blended rate used to report.
    baseline = {
        "elapsed_hours": 0.0,
        "timescale_storage": _storage(
            total_bytes=100 * 1024 * 1024, row_count=1000, after_compression_total_bytes=0
        ),
        "host_swap_counters": {"pswpin": 0, "pswpout": 0},
    }
    current = {
        "elapsed_hours": 24.0,
        "health": _health(),
        "momentum_capture_container": {"MemUsage": "28MiB / 512MiB", "CPUPerc": "12%"},
        "collector_container": {"MemUsage": "10MiB / 3.7GiB", "CPUPerc": "9%"},
        "host_swap_used_mb": 0,
        "host_swap_counters": {"pswpin": 5, "pswpout": 3},
        "timescale_storage": _storage(
            total_bytes=196 * 1024 * 1024, row_count=1200000, after_compression_total_bytes=0
        ),
    }
    text = canary._notification_text("24h", current, baseline=baseline)
    assert "Since baseline (24.0h)" in text
    assert "ROADMAP item 6's two separate gates" in text
    assert "hot +96 MiB" in text
    assert "96 MiB/day" in text  # 96 MiB over 24h = 96 MiB/day
    assert "compressed +0 MiB" in text
    assert "pswpin +5 pswpout +3" in text
    assert "Status: healthy" in text


def test_raw_ingest_rate_survives_a_chunk_rotating_out_of_the_hot_bucket() -> None:
    # A naive delta of the hot-chunk inventory alone would miss this
    # scenario entirely: between baseline and this checkpoint, the old hot
    # chunk grew from 1200 to 1400 MiB raw, THEN crossed the 1-day
    # compress_after boundary and compressed down to 200 MiB, while a brand
    # new hot chunk started at 0 and grew back up to 1200 MiB by the time of
    # this checkpoint. Naive hot_now - hot_before = 1200 - 1200 = 0, which
    # would make a full chunk's worth of real ingest invisible -- exactly
    # the false-negative risk against the 1.5 GiB/day gate this fixes.
    baseline = {
        "elapsed_hours": 0.0,
        "timescale_storage": _storage(
            total_bytes=1200 * 1024 * 1024,
            row_count=1_000_000,
            after_compression_total_bytes=0,
            before_compression_total_bytes=0,
        ),
        "host_swap_counters": {"pswpin": 0, "pswpout": 0},
    }
    current = {
        "elapsed_hours": 48.0,
        "health": _health(),
        "momentum_capture_container": {"MemUsage": "28MiB / 512MiB", "CPUPerc": "12%"},
        "collector_container": {"MemUsage": "10MiB / 3.7GiB", "CPUPerc": "9%"},
        "host_swap_used_mb": 0,
        "host_swap_counters": {"pswpin": 0, "pswpout": 0},
        "timescale_storage": _storage(
            # New hot chunk (1200 MiB) + the one now-compressed chunk (200 MiB after).
            total_bytes=1400 * 1024 * 1024,
            row_count=2_000_000,
            after_compression_total_bytes=200 * 1024 * 1024,
            # The compressed chunk's own raw size before it compressed.
            before_compression_total_bytes=1400 * 1024 * 1024,
        ),
    }
    delta = canary._delta_block(current, baseline)
    # (hot_now - hot_before) + (before_compression delta) = 0 + 1400 MiB.
    assert delta["raw_ingest_delta_bytes"] == 1400 * 1024 * 1024
    # after_compression_total_bytes is itself a monotonic counter -- a plain
    # delta is already correct, no rotation correction needed.
    assert delta["compressed_delta_bytes"] == 200 * 1024 * 1024

    text = canary._notification_text("48h", current, baseline=baseline)
    assert "hot +1400 MiB" in text
    assert "700 MiB/day" in text  # 1400 MiB over 48h = 700 MiB/day raw ingest
    assert "compressed +200 MiB" in text
    assert "100 MiB/day" in text  # 200 MiB over 48h = 100 MiB/day compressed


# --- fix 5: missed-checkpoint handling for long downtime ---


def test_checkpoint_collected_within_grace_still_counts_as_that_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    _fake_collection(monkeypatch)
    monkeypatch.setattr(canary, "_notify", lambda message: None)

    snapshot = tmp_path / "runtime" / "momentum-canary.json"
    # 1h49m late is within the 2h grace window.
    payload = canary.run_once(
        now=EPOCH_START + timedelta(hours=24) + timedelta(hours=1, minutes=49),
        snapshot_path=snapshot,
    )
    assert payload["active_epoch"]["checkpoints"]["24"]["state"] == "notified"
    assert payload["active_epoch"]["late_snapshots"] == []


def test_checkpoint_missed_after_long_downtime_produces_one_late_snapshot_not_a_fake_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    _fake_collection(monkeypatch)
    notified: list[str] = []
    monkeypatch.setattr(canary, "_notify", lambda message: notified.append(message) or None)

    snapshot = tmp_path / "runtime" / "momentum-canary.json"
    # Host was "down" until hour 51: both 24h (27h late) and 48h (3h late)
    # are found overdue in the same run, well past the 2h grace window.
    payload = canary.run_once(now=EPOCH_START + timedelta(hours=51), snapshot_path=snapshot)

    checkpoints = payload["active_epoch"]["checkpoints"]
    assert checkpoints["24"]["state"] == "missed"
    assert checkpoints["48"]["state"] == "missed"
    assert checkpoints["72"]["state"] == "pending"
    # Exactly one combined late snapshot, not one per missed offset.
    assert len(payload["active_epoch"]["late_snapshots"]) == 1
    assert any("late catch-up" in message for message in notified)


def test_48h_collected_separately_when_it_falls_within_its_own_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    _fake_collection(monkeypatch)
    monkeypatch.setattr(canary, "_notify", lambda message: None)

    snapshot = tmp_path / "runtime" / "momentum-canary.json"
    # Down 20h-50h: 24h is 26h late (missed), 48h is only 2h late -- at the
    # grace boundary, still within window, and must fire as a real 48h.
    payload = canary.run_once(
        now=EPOCH_START + timedelta(hours=49, minutes=59), snapshot_path=snapshot
    )
    checkpoints = payload["active_epoch"]["checkpoints"]
    assert checkpoints["24"]["state"] == "missed"
    assert checkpoints["48"]["state"] == "notified"


# --- fix 6: --sample-now never touches official state ---


def test_sample_now_does_not_consume_the_official_checkpoint_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    _fake_collection(monkeypatch)
    notified: list[str] = []
    monkeypatch.setattr(canary, "_notify", lambda message: notified.append(message) or None)

    snapshot = tmp_path / "runtime" / "momentum-canary.json"
    payload = canary.run_once(
        now=EPOCH_START + timedelta(hours=1), snapshot_path=snapshot, sample_now_offset=48
    )
    assert payload["active_epoch"]["checkpoints"]["48"]["state"] == "pending"
    assert len(payload["active_epoch"]["diagnostic_snapshots"]) == 1
    assert any("diagnostic sample-now" in message for message in notified)

    # The real 48h checkpoint must still fire normally later.
    payload = canary.run_once(now=EPOCH_START + timedelta(hours=49), snapshot_path=snapshot)
    assert payload["active_epoch"]["checkpoints"]["48"]["state"] == "notified"


# --- systemd unit sanity ---


def test_service_unit_sets_docker_config_to_avoid_protecthome_warnings() -> None:
    unit_path = (
        Path(__file__).parents[3]
        / "infra"
        / "systemd"
        / "schurfer-momentum-canary-checkpoints.service"
    )
    content = unit_path.read_text()
    assert "DOCKER_CONFIG=" in content
    assert "ProtectHome=true" in content
