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
        "bars_completed_total": "130787",
        "symbols_missing_ticker_count": "0",
        "symbols_missing_trades_count": "4",
        "persist_retries_total": "0",
        "nats_dropped_total": "0",
        "late_events_total": "0",
        "writer_queue_depth": "0",
    }


def _fake_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        canary,
        "_docker_stats",
        lambda container, timeout=15: {"MemUsage": "28MiB / 512MiB", "CPUPerc": "12%"},
    )
    monkeypatch.setattr(
        canary,
        "_hypertable_storage",
        lambda timeout=30: {
            "table_bytes": 100,
            "index_bytes": 20,
            "toast_bytes": 0,
            "total_bytes": 120,
            "total_chunks": 3,
            "compressed_chunks": 1,
            "uncompressed_chunks": 2,
            "before_compression_total_bytes": 600,
            "after_compression_total_bytes": 100,
            "row_count": 130787,
        },
    )
    monkeypatch.setattr(canary, "_swap_used_mb", lambda: 0)
    monkeypatch.setattr(canary, "_swap_activity_counters", lambda: {"pswpin": 0, "pswpout": 0})


def test_read_momentum_health_rejects_odd_hgetall_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(canary, "_run", lambda command, timeout: "a\n1\nb\n")
    with pytest.raises(RuntimeError, match="odd number"):
        canary._read_momentum_health()


def test_read_momentum_health_rejects_empty_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(canary, "_run", lambda command, timeout: "")
    with pytest.raises(RuntimeError, match="empty or missing"):
        canary._read_momentum_health()


def test_read_momentum_health_pairs_lines_into_a_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        canary, "_run", lambda command, timeout: "started_at_ms\n123\nbars_completed_total\n5\n"
    )
    assert canary._read_momentum_health() == {"started_at_ms": "123", "bars_completed_total": "5"}


def test_no_checkpoint_fires_before_24h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    _fake_checkpoint(monkeypatch)
    notified: list[str] = []
    monkeypatch.setattr(canary, "_notify", lambda message: notified.append(message) or None)

    snapshot = tmp_path / "runtime" / "momentum-canary.json"
    payload = canary.run_once(now=EPOCH_START + timedelta(hours=10), snapshot_path=snapshot)

    assert payload["fired_offsets_hours"] == []
    assert notified == []


def test_24h_checkpoint_fires_once_and_is_not_refired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    _fake_checkpoint(monkeypatch)
    notified: list[str] = []
    monkeypatch.setattr(canary, "_notify", lambda message: notified.append(message) or None)

    snapshot = tmp_path / "runtime" / "momentum-canary.json"
    first = canary.run_once(
        now=EPOCH_START + timedelta(hours=24, minutes=5), snapshot_path=snapshot
    )
    assert first["fired_offsets_hours"] == [24]
    assert len(notified) == 1
    assert "24h" in notified[0]

    # A second run shortly after must not re-fire or re-notify the 24h checkpoint.
    second = canary.run_once(
        now=EPOCH_START + timedelta(hours=24, minutes=20), snapshot_path=snapshot
    )
    assert second["fired_offsets_hours"] == [24]
    assert len(notified) == 1


def test_all_three_checkpoints_fire_in_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    _fake_checkpoint(monkeypatch)
    notified: list[str] = []
    monkeypatch.setattr(canary, "_notify", lambda message: notified.append(message) or None)

    snapshot = tmp_path / "runtime" / "momentum-canary.json"
    canary.run_once(now=EPOCH_START + timedelta(hours=25), snapshot_path=snapshot)
    canary.run_once(now=EPOCH_START + timedelta(hours=49), snapshot_path=snapshot)
    final = canary.run_once(now=EPOCH_START + timedelta(hours=73), snapshot_path=snapshot)

    assert final["fired_offsets_hours"] == [24, 48, 72]
    assert len(notified) == 3
    assert [entry["offset_hours"] for entry in final["history"]] == [24, 48, 72]


def test_restart_with_new_started_at_ms_resets_the_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_checkpoint(monkeypatch)
    monkeypatch.setattr(canary, "_notify", lambda message: None)

    snapshot = tmp_path / "runtime" / "momentum-canary.json"
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    first = canary.run_once(now=EPOCH_START + timedelta(hours=25), snapshot_path=snapshot)
    assert first["fired_offsets_hours"] == [24]

    new_start = EPOCH_START + timedelta(hours=30)
    new_start_ms = int(new_start.timestamp() * 1000)
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health(new_start_ms))
    # Same wall-clock instant that already fired 24h under the OLD epoch must
    # not be considered fired under the NEW one.
    second = canary.run_once(now=new_start + timedelta(hours=1), snapshot_path=snapshot)
    assert second["epoch_started_at_ms"] == new_start_ms
    assert second["fired_offsets_hours"] == []
    assert second["history"] == []


def test_force_collects_immediately_even_if_not_due(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    _fake_checkpoint(monkeypatch)
    notified: list[str] = []
    monkeypatch.setattr(canary, "_notify", lambda message: notified.append(message) or None)

    snapshot = tmp_path / "runtime" / "momentum-canary.json"
    payload = canary.run_once(
        now=EPOCH_START + timedelta(hours=1), snapshot_path=snapshot, force_offset=48
    )

    assert payload["fired_offsets_hours"] == [48]
    assert len(notified) == 1


def test_missing_started_at_ms_records_error_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _broken_health() -> dict[str, str]:
        raise RuntimeError(f"{canary.HEALTH_KEY} is empty or missing in redis")

    monkeypatch.setattr(canary, "_read_momentum_health", _broken_health)
    snapshot = tmp_path / "runtime" / "momentum-canary.json"

    with pytest.raises(RuntimeError, match="empty or missing"):
        canary.run_once(now=EPOCH_START + timedelta(hours=1), snapshot_path=snapshot)

    payload = canary._read_json(snapshot)
    assert "empty or missing" in payload["last_error"]


def test_collection_failure_at_a_due_checkpoint_notifies_once_then_retries_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canary, "_read_momentum_health", lambda: _health())
    monkeypatch.setattr(
        canary,
        "_docker_stats",
        lambda container, timeout=15: (_ for _ in ()).throw(
            RuntimeError("docker daemon unreachable")
        ),
    )
    notified: list[str] = []
    monkeypatch.setattr(canary, "_notify", lambda message: notified.append(message) or None)

    snapshot = tmp_path / "runtime" / "momentum-canary.json"
    first = canary.run_once(now=EPOCH_START + timedelta(hours=25), snapshot_path=snapshot)
    assert first["fired_offsets_hours"] == []
    assert len(notified) == 1
    assert "collection failed" in notified[0]

    # Retrying while still broken must not spam a second alert.
    second = canary.run_once(
        now=EPOCH_START + timedelta(hours=25, minutes=15), snapshot_path=snapshot
    )
    assert second["fired_offsets_hours"] == []
    assert len(notified) == 1

    # Once the underlying failure clears, the checkpoint fires and the error state clears too.
    _fake_checkpoint(monkeypatch)
    third = canary.run_once(
        now=EPOCH_START + timedelta(hours=25, minutes=30), snapshot_path=snapshot
    )
    assert third["fired_offsets_hours"] == [24]
    assert len(notified) == 2


def test_notification_text_includes_key_health_and_storage_fields() -> None:
    checkpoint = {
        "elapsed_hours": 24.08,
        "health": _health(),
        "momentum_capture_container": {"MemUsage": "28MiB / 512MiB", "CPUPerc": "12%"},
        "collector_container": {"MemUsage": "10MiB / 3.7GiB", "CPUPerc": "9%"},
        "host_swap_used_mb": 0,
        "host_swap_counters": {"pswpin": 0, "pswpout": 0},
        "timescale_storage": {
            "total_bytes": 120 * 1024 * 1024,
            "compressed_chunks": 1,
            "total_chunks": 3,
            "before_compression_total_bytes": 600 * 1024 * 1024,
            "after_compression_total_bytes": 100 * 1024 * 1024,
            "row_count": 130787,
        },
    }
    text = canary._notification_text(24, checkpoint)
    assert "24h" in text
    assert "130787" in text
    assert "1/3 chunks compressed" in text
    assert "130,787" in text


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
    assert storage["compressed_chunks"] == 1
    assert storage["uncompressed_chunks"] == 2
    assert storage["row_count"] == 130787
    assert not any("pg_relation_size" in query for query in queries)
    assert any("hypertable_detailed_size" in query for query in queries)
    assert any("hypertable_compression_stats" in query for query in queries)
