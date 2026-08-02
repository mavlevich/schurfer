import importlib.util
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load_checkpoint_module() -> ModuleType:
    path = Path(__file__).parents[3] / "infra" / "scripts" / "research_checkpoints.py"
    spec = importlib.util.spec_from_file_location("research_checkpoints_under_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load checkpoint runner from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checkpoints = _load_checkpoint_module()


def test_report_outcome_keeps_discovery_separate_from_strategy_promotion() -> None:
    assert checkpoints._report_outcome(
        "orderflow",
        {"readiness": "discovery_ready"},
    ) == ("discovery_ready", "discovery_ready", "discovery_ready")
    assert checkpoints._report_outcome(
        "liquid_taker",
        {"formal_inference": {"status": "ready", "verdict": "do_not_promote"}},
    ) == ("ready", "do_not_promote", "no_go")
    assert checkpoints._report_outcome(
        "open_ended_margin",
        {"capital_efficiency_gate": {"state": "boundary_only_ready"}},
    ) == ("boundary_only_ready", "boundary_only_ready", "boundary_only_ready")


def test_report_outcome_rejects_unknown_state() -> None:
    with pytest.raises(ValueError, match="unsupported checkpoint state"):
        checkpoints._report_outcome("orderflow", {"readiness": "surprise"})


def test_runner_executes_only_one_due_checkpoint_and_writes_sanitized_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_run_report(
        spec: Any,
        *,
        now: datetime,
        root: Path,
        report_dir: Path,
    ) -> tuple[dict[str, object], Path, str]:
        del now, root
        calls.append(spec.key)
        path = report_dir / "report.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        return {"readiness": "discovery_ready"}, path, "a" * 64

    monkeypatch.setattr(checkpoints, "_run_report", fake_run_report)
    monkeypatch.setattr(checkpoints, "_headroom_mb", lambda: 4096)
    monkeypatch.setattr(checkpoints, "_disk_free_gb", lambda _path: 100)
    monkeypatch.setattr(checkpoints, "_notify", lambda _message: None)
    now = datetime(2026, 8, 30, tzinfo=UTC)
    snapshot = tmp_path / "runtime" / "research.json"

    payload = checkpoints.run_once(
        now=now,
        root=tmp_path,
        snapshot_path=snapshot,
        report_dir=tmp_path / "reports",
    )

    assert calls == ["orderflow"]
    assert payload["version"] == checkpoints.SNAPSHOT_VERSION
    row = payload["checkpoints"][0]
    assert row["state"] == "discovery_ready"
    assert row["report_sha256"] == "a" * 64
    assert "TELEGRAM" not in snapshot.read_text()


def test_terminal_checkpoint_is_not_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        checkpoints,
        "_run_report",
        lambda *_args, **_kwargs: pytest.fail("terminal checkpoint must not rerun"),
    )
    snapshot = tmp_path / "research.json"
    now = datetime(2026, 8, 7, tzinfo=UTC)
    row = checkpoints._default_row(checkpoints.CHECKPOINTS[0])
    row.update(state="discovery_ready", last_attempt_at=checkpoints._utc(now))
    checkpoints._atomic_json(
        snapshot,
        {
            "version": checkpoints.SNAPSHOT_VERSION,
            "generated_at": checkpoints._utc(now),
            "runner_state": "idle",
            "checkpoints": [row],
        },
    )

    payload = checkpoints.run_once(
        now=now + timedelta(days=2),
        root=tmp_path,
        snapshot_path=snapshot,
        report_dir=tmp_path / "reports",
    )

    orderflow = next(row for row in payload["checkpoints"] if row["key"] == "orderflow")
    assert orderflow["state"] == "discovery_ready"


def test_terminal_checkpoint_survives_due_date_moving_forward(tmp_path: Path) -> None:
    snapshot = tmp_path / "research.json"
    now = datetime(2026, 7, 29, tzinfo=UTC)
    row = checkpoints._default_row(checkpoints.CHECKPOINTS[0])
    row.update(state="no_go", verdict="no_go", notified_state="no_go")
    checkpoints._atomic_json(
        snapshot,
        {
            "version": checkpoints.SNAPSHOT_VERSION,
            "generated_at": checkpoints._utc(now),
            "runner_state": "idle",
            "checkpoints": [row],
        },
    )

    payload = checkpoints.run_once(
        now=now,
        root=tmp_path,
        snapshot_path=snapshot,
        report_dir=tmp_path / "reports",
    )

    orderflow = next(row for row in payload["checkpoints"] if row["key"] == "orderflow")
    assert orderflow["state"] == "no_go"
    assert orderflow["verdict"] == "no_go"


def test_contract_change_resets_old_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / "research.json"
    now = datetime(2026, 7, 29, tzinfo=UTC)
    old_spec = checkpoints.CHECKPOINTS[0]
    row = checkpoints._default_row(old_spec)
    row.update(state="no_go", verdict="no_go", notified_state="no_go")
    checkpoints._atomic_json(
        snapshot,
        {
            "version": checkpoints.SNAPSHOT_VERSION,
            "generated_at": checkpoints._utc(now),
            "runner_state": "idle",
            "checkpoints": [row],
        },
    )
    changed_spec = replace(old_spec, contract="bybit_orderflow_pilot_v2")
    monkeypatch.setattr(checkpoints, "CHECKPOINTS", (changed_spec,))

    payload = checkpoints.run_once(
        now=now,
        root=tmp_path,
        snapshot_path=snapshot,
        report_dir=tmp_path / "reports",
    )

    orderflow = payload["checkpoints"][0]
    assert orderflow["state"] == "scheduled"
    assert orderflow["contract"] == "bybit_orderflow_pilot_v2"
    assert orderflow["verdict"] is None


def test_terminal_checkpoint_retries_failed_notification_without_rerunning_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages: list[str] = []

    def fake_notify(message: str) -> None:
        messages.append(message)

    monkeypatch.setattr(
        checkpoints,
        "_run_report",
        lambda *_args, **_kwargs: pytest.fail("terminal checkpoint must not rerun"),
    )
    monkeypatch.setattr(checkpoints, "_notify", fake_notify)
    snapshot = tmp_path / "research.json"
    now = datetime(2026, 8, 7, tzinfo=UTC)
    row = checkpoints._default_row(checkpoints.CHECKPOINTS[0])
    row.update(state="discovery_ready", alert_error="telegram unavailable")
    checkpoints._atomic_json(
        snapshot,
        {
            "version": checkpoints.SNAPSHOT_VERSION,
            "generated_at": checkpoints._utc(now),
            "runner_state": "idle",
            "checkpoints": [row],
        },
    )

    payload = checkpoints.run_once(
        now=now + timedelta(hours=1),
        root=tmp_path,
        snapshot_path=snapshot,
        report_dir=tmp_path / "reports",
    )

    orderflow = next(row for row in payload["checkpoints"] if row["key"] == "orderflow")
    assert messages and "Bybit order-flow discovery" in messages[0]
    assert orderflow["notified_state"] == "discovery_ready"
    assert "alert_error" not in orderflow


def test_resource_gate_blocks_without_invoking_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkpoints, "_headroom_mb", lambda: 100)
    monkeypatch.setattr(checkpoints, "_disk_free_gb", lambda _path: 100)
    monkeypatch.setattr(
        checkpoints,
        "_run_report",
        lambda *_args, **_kwargs: pytest.fail("resource-blocked report must not run"),
    )
    monkeypatch.setattr(checkpoints, "_notify", lambda _message: None)

    payload = checkpoints.run_once(
        now=datetime(2026, 8, 7, tzinfo=UTC),
        root=tmp_path,
        snapshot_path=tmp_path / "research.json",
        report_dir=tmp_path / "reports",
    )

    row = payload["checkpoints"][0]
    assert row["state"] == "blocked_resources"
    assert "observed 100 MiB" in row["error"]
