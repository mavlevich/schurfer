import importlib.util
import subprocess
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
        cutoff: datetime,
        root: Path,
        report_dir: Path,
    ) -> tuple[dict[str, object], Path, str]:
        del now, cutoff, root
        calls.append(spec.key)
        path = report_dir / "report.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        return {"readiness": "discovery_ready"}, path, "a" * 64

    monkeypatch.setattr(
        checkpoints,
        "CHECKPOINTS",
        (replace(checkpoints.CHECKPOINTS[0], retired_verdict=None),),
    )
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
    monkeypatch.setattr(checkpoints, "CHECKPOINTS", (checkpoints.CHECKPOINTS[0],))
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
    monkeypatch.setattr(checkpoints, "CHECKPOINTS", (checkpoints.CHECKPOINTS[0],))

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


def test_orderflow_checkpoint_is_registered_retired_no_go() -> None:
    orderflow_spec = next(spec for spec in checkpoints.CHECKPOINTS if spec.key == "orderflow")
    assert orderflow_spec.retired_verdict == "no_go"


def test_validate_checkpoints_rejects_a_non_terminal_retired_verdict() -> None:
    bogus = replace(checkpoints.CHECKPOINTS[0], retired_verdict="collecting")
    with pytest.raises(ValueError, match="not a terminal state"):
        checkpoints._validate_checkpoints((bogus,))


def test_retired_checkpoint_closes_out_without_running_its_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retired_spec = replace(checkpoints.CHECKPOINTS[0], retired_verdict="no_go")
    monkeypatch.setattr(checkpoints, "CHECKPOINTS", (retired_spec,))
    monkeypatch.setattr(
        checkpoints,
        "_run_report",
        lambda *_args, **_kwargs: pytest.fail("retired checkpoint must not run its report"),
    )
    messages: list[str] = []
    monkeypatch.setattr(checkpoints, "_notify", lambda message: messages.append(message))
    snapshot = tmp_path / "research.json"
    row = checkpoints._default_row(retired_spec)
    row.update(state="collecting", verdict="withheld", notified_state="collecting")
    checkpoints._atomic_json(
        snapshot,
        {
            "version": checkpoints.SNAPSHOT_VERSION,
            "generated_at": checkpoints._utc(datetime(2026, 8, 6, tzinfo=UTC)),
            "runner_state": "idle",
            "checkpoints": [row],
        },
    )

    payload = checkpoints.run_once(
        now=datetime(2026, 8, 7, 19, 0, tzinfo=UTC),
        root=tmp_path,
        snapshot_path=snapshot,
        report_dir=tmp_path / "reports",
    )

    orderflow = payload["checkpoints"][0]
    assert orderflow["state"] == "no_go"
    assert orderflow["verdict"] == "no_go"
    assert orderflow["report_file"] is None
    assert orderflow["notified_state"] == "no_go"
    assert len(messages) == 1
    assert "🛑" in messages[0]
    assert "no_go" in messages[0]


def test_retired_checkpoint_stays_terminal_and_silent_on_later_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retired_spec = replace(checkpoints.CHECKPOINTS[0], retired_verdict="no_go")
    monkeypatch.setattr(checkpoints, "CHECKPOINTS", (retired_spec,))
    monkeypatch.setattr(
        checkpoints,
        "_run_report",
        lambda *_args, **_kwargs: pytest.fail("retired checkpoint must not run its report"),
    )
    monkeypatch.setattr(
        checkpoints,
        "_notify",
        lambda _message: pytest.fail("an already-notified terminal state must not re-notify"),
    )
    snapshot = tmp_path / "research.json"
    row = checkpoints._default_row(retired_spec)
    row.update(state="no_go", verdict="no_go", notified_state="no_go")
    checkpoints._atomic_json(
        snapshot,
        {
            "version": checkpoints.SNAPSHOT_VERSION,
            "generated_at": checkpoints._utc(datetime(2026, 8, 7, tzinfo=UTC)),
            "runner_state": "idle",
            "checkpoints": [row],
        },
    )

    payload = checkpoints.run_once(
        now=datetime(2026, 9, 1, tzinfo=UTC),
        root=tmp_path,
        snapshot_path=snapshot,
        report_dir=tmp_path / "reports",
    )

    orderflow = payload["checkpoints"][0]
    assert orderflow["state"] == "no_go"


def test_resource_gate_blocks_without_invoking_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        checkpoints,
        "CHECKPOINTS",
        (replace(checkpoints.CHECKPOINTS[0], retired_verdict=None),),
    )
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
    assert row["error_code"] == "insufficient_resources"
    assert "observed 100 MiB" in row["error"]


@pytest.mark.parametrize(
    ("returncode", "stderr", "expected_code"),
    [
        (2, "make: *** [Makefile:1458: prod-liquid-taker-report] Error 137", "report_oom"),
        (137, "", "report_oom"),
        (2, "docker compose failed", "report_failed"),
    ],
)
def test_process_failure_classifies_oom_without_exposing_stderr(
    returncode: int,
    stderr: str,
    expected_code: str,
) -> None:
    spec = next(spec for spec in checkpoints.CHECKPOINTS if spec.key == "liquid_taker")

    failure = checkpoints._process_failure(spec, returncode, stderr)

    assert failure.code == expected_code
    if stderr:
        assert stderr not in str(failure)


def test_stderr_capture_relays_complete_output_but_keeps_a_bounded_tail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    capture = checkpoints._StderrCapture(limit_bytes=24)

    checkpoints._stream_stderr(
        iter(("first diagnostic\n", "OOMKilled\n", "x" * 40 + "\n")),
        capture,
    )

    assert capsys.readouterr().err == "first diagnostic\nOOMKilled\n" + "x" * 40 + "\n"
    assert "first diagnostic" not in capture.text()
    assert len(capture.text().encode()) <= 24
    assert capture.oom_observed is True
    spec = next(spec for spec in checkpoints.CHECKPOINTS if spec.key == "liquid_taker")
    assert (
        checkpoints._process_failure(
            spec,
            2,
            capture.text(),
            oom_observed=capture.oom_observed,
        ).code
        == "report_oom"
    )


def test_run_report_relays_child_stderr_and_archives_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_popen = subprocess.Popen

    def fake_popen(_command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        return real_popen(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "sys.stderr.write('phase-one\\n'); sys.stderr.flush(); "
                    'sys.stdout.write(\'{"readiness":"discovery_ready"}\')'
                ),
            ],
            **kwargs,
        )

    monkeypatch.setattr(checkpoints.subprocess, "Popen", fake_popen)
    spec = replace(
        checkpoints.CHECKPOINTS[0],
        retired_verdict=None,
        timeout_minutes=1,
    )
    now = datetime(2026, 8, 30, tzinfo=UTC)

    report, report_path, digest = checkpoints._run_report(
        spec,
        now=now,
        cutoff=now,
        root=tmp_path,
        report_dir=tmp_path / "reports",
    )

    assert capsys.readouterr().err == "phase-one\n"
    assert report == {"readiness": "discovery_ready"}
    assert report_path.read_text().endswith("\n")
    assert len(digest) == 64


def test_legacy_notification_identity_is_upgraded_without_duplicate_alert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = replace(
        next(spec for spec in checkpoints.CHECKPOINTS if spec.key == "liquid_taker"),
        due_at=datetime(2026, 8, 27, tzinfo=UTC),
    )
    monkeypatch.setattr(checkpoints, "CHECKPOINTS", (spec,))
    monkeypatch.setattr(
        checkpoints,
        "_notify",
        lambda _message: pytest.fail("snapshot migration must not notify"),
    )
    failed_at = datetime(2026, 8, 27, 0, 1, 41, tzinfo=UTC)
    snapshot = tmp_path / "research.json"
    row = checkpoints._default_row(spec)
    row.update(
        state="error",
        last_attempt_at=checkpoints._utc(failed_at),
        next_attempt_at=checkpoints._utc(failed_at + timedelta(hours=spec.cadence_hours)),
        notified_state="error",
        error="legacy failure",
    )
    for key in (
        "report_cutoff_at",
        "error_code",
        "consecutive_errors",
        "error_retry_exhausted",
    ):
        row.pop(key)
    checkpoints._atomic_json(
        snapshot,
        {
            "version": checkpoints.SNAPSHOT_VERSION,
            "generated_at": checkpoints._utc(failed_at),
            "runner_state": "idle",
            "checkpoints": [row],
        },
    )

    payload = checkpoints.run_once(
        now=failed_at + timedelta(minutes=30),
        root=tmp_path,
        snapshot_path=snapshot,
        report_dir=tmp_path / "reports",
    )

    upgraded = payload["checkpoints"][0]
    assert upgraded["notified_state"] == "error:unknown"
    assert upgraded["report_cutoff_at"] == checkpoints._utc(failed_at)
    assert upgraded["consecutive_errors"] == 1
    assert upgraded["error_retry_exhausted"] is False


def test_steady_state_error_upgrade_preserves_existing_retry_schedule() -> None:
    spec = next(spec for spec in checkpoints.CHECKPOINTS if spec.key == "liquid_taker")
    row = checkpoints._default_row(spec)
    custom_next_attempt = datetime(2026, 9, 3, 12, 34, tzinfo=UTC)
    row.update(
        state="error",
        last_attempt_at=checkpoints._utc(datetime(2026, 8, 27, tzinfo=UTC)),
        report_cutoff_at=checkpoints._utc(datetime(2026, 8, 27, tzinfo=UTC)),
        next_attempt_at=checkpoints._utc(custom_next_attempt),
        error_code="report_oom",
        consecutive_errors=2,
        error_retry_exhausted=False,
        notified_state="error:report_oom",
    )

    upgraded = checkpoints._upgrade_checkpoint_row(spec, row)

    assert upgraded["next_attempt_at"] == checkpoints._utc(custom_next_attempt)


def test_failed_v1_checkpoint_retries_original_cutoff_and_records_oom_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = replace(
        next(spec for spec in checkpoints.CHECKPOINTS if spec.key == "liquid_taker"),
        due_at=datetime(2026, 8, 27, tzinfo=UTC),
    )
    monkeypatch.setattr(checkpoints, "CHECKPOINTS", (spec,))
    monkeypatch.setattr(checkpoints, "_headroom_mb", lambda: 4096)
    monkeypatch.setattr(checkpoints, "_disk_free_gb", lambda _path: 100)
    notifications: list[str] = []
    monkeypatch.setattr(checkpoints, "_notify", lambda message: notifications.append(message))
    attempted_cutoffs: list[datetime] = []

    def fail_report(
        _spec: Any,
        *,
        now: datetime,
        cutoff: datetime,
        root: Path,
        report_dir: Path,
    ) -> None:
        del now, root, report_dir
        attempted_cutoffs.append(cutoff)
        raise checkpoints.ReportExecutionError(
            "report_oom",
            "prod-liquid-taker-report exceeded its container memory limit",
        )

    monkeypatch.setattr(checkpoints, "_run_report", fail_report)
    failed_at = datetime(2026, 8, 27, 0, 1, 41, tzinfo=UTC)
    now = failed_at + timedelta(hours=2)
    snapshot = tmp_path / "research.json"
    legacy_row = checkpoints._default_row(spec)
    legacy_row.update(
        state="error",
        last_attempt_at=checkpoints._utc(failed_at),
        next_attempt_at=checkpoints._utc(failed_at + timedelta(hours=spec.cadence_hours)),
        error="prod-liquid-taker-report exited 2; inspect systemd journal",
    )
    for key in ("report_cutoff_at", "error_code", "consecutive_errors"):
        legacy_row.pop(key)
    checkpoints._atomic_json(
        snapshot,
        {
            "version": checkpoints.SNAPSHOT_VERSION,
            "generated_at": checkpoints._utc(failed_at),
            "runner_state": "idle",
            "checkpoints": [legacy_row],
        },
    )

    payload = checkpoints.run_once(
        now=now,
        root=tmp_path,
        snapshot_path=snapshot,
        report_dir=tmp_path / "reports",
    )

    row = payload["checkpoints"][0]
    assert attempted_cutoffs == [failed_at]
    assert row["report_cutoff_at"] == checkpoints._utc(failed_at)
    assert row["state"] == "error"
    assert row["error_code"] == "report_oom"
    assert row["consecutive_errors"] == 2
    assert row["next_attempt_at"] == checkpoints._utc(now + timedelta(hours=1))
    assert row["notified_state"] == "error:report_oom"
    assert notifications and "Reason: report_oom" in notifications[0]


def test_error_short_retries_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = replace(
        next(spec for spec in checkpoints.CHECKPOINTS if spec.key == "liquid_taker"),
        max_consecutive_error_attempts=3,
    )
    monkeypatch.setattr(checkpoints, "CHECKPOINTS", (spec,))
    monkeypatch.setattr(checkpoints, "_headroom_mb", lambda: 4096)
    monkeypatch.setattr(checkpoints, "_disk_free_gb", lambda _path: 100)
    monkeypatch.setattr(checkpoints, "_notify", lambda _message: None)
    monkeypatch.setattr(
        checkpoints,
        "_run_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            checkpoints.ReportExecutionError("report_oom", "memory limit")
        ),
    )
    now = datetime(2026, 8, 28, tzinfo=UTC)
    snapshot = tmp_path / "research.json"
    row = checkpoints._default_row(spec)
    row.update(
        state="error",
        last_attempt_at=checkpoints._utc(now - timedelta(hours=2)),
        report_cutoff_at=checkpoints._utc(datetime(2026, 8, 27, tzinfo=UTC)),
        next_attempt_at=checkpoints._utc(now - timedelta(hours=1)),
        error_code="report_oom",
        consecutive_errors=2,
    )
    checkpoints._atomic_json(
        snapshot,
        {
            "version": checkpoints.SNAPSHOT_VERSION,
            "generated_at": checkpoints._utc(now - timedelta(hours=1)),
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

    failed = payload["checkpoints"][0]
    assert failed["consecutive_errors"] == 3
    assert failed["error_retry_exhausted"] is True
    assert failed["next_attempt_at"] == checkpoints._utc(now + timedelta(hours=spec.cadence_hours))


def test_exhausted_error_alerts_after_each_cadence_attempt_and_caps_counter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = replace(
        next(spec for spec in checkpoints.CHECKPOINTS if spec.key == "liquid_taker"),
        max_consecutive_error_attempts=3,
    )
    monkeypatch.setattr(checkpoints, "CHECKPOINTS", (spec,))
    monkeypatch.setattr(checkpoints, "_headroom_mb", lambda: 4096)
    monkeypatch.setattr(checkpoints, "_disk_free_gb", lambda _path: 100)
    monkeypatch.setattr(
        checkpoints,
        "_run_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            checkpoints.ReportExecutionError("report_oom", "memory limit")
        ),
    )
    notifications: list[str] = []
    monkeypatch.setattr(checkpoints, "_notify", lambda message: notifications.append(message))
    cutoff = datetime(2026, 8, 27, tzinfo=UTC)
    previous_attempt = datetime(2026, 8, 28, tzinfo=UTC)
    first_retry = previous_attempt + timedelta(hours=spec.cadence_hours)
    snapshot = tmp_path / "research.json"
    row = checkpoints._default_row(spec)
    row.update(
        state="error",
        last_attempt_at=checkpoints._utc(previous_attempt),
        report_cutoff_at=checkpoints._utc(cutoff),
        next_attempt_at=checkpoints._utc(first_retry),
        error_code="report_oom",
        consecutive_errors=3,
        error_retry_exhausted=True,
        notified_state=(f"error:report_oom:exhausted:{checkpoints._utc(previous_attempt)}"),
    )
    checkpoints._atomic_json(
        snapshot,
        {
            "version": checkpoints.SNAPSHOT_VERSION,
            "generated_at": checkpoints._utc(previous_attempt),
            "runner_state": "idle",
            "checkpoints": [row],
        },
    )

    first = checkpoints.run_once(
        now=first_retry,
        root=tmp_path,
        snapshot_path=snapshot,
        report_dir=tmp_path / "reports",
    )["checkpoints"][0]
    second_retry = first_retry + timedelta(hours=spec.cadence_hours)
    second = checkpoints.run_once(
        now=second_retry,
        root=tmp_path,
        snapshot_path=snapshot,
        report_dir=tmp_path / "reports",
    )["checkpoints"][0]

    assert len(notifications) == 2
    assert all("short retry budget exhausted" in message for message in notifications)
    assert first["report_cutoff_at"] == checkpoints._utc(cutoff)
    assert second["report_cutoff_at"] == checkpoints._utc(cutoff)
    assert first["consecutive_errors"] == 3
    assert second["consecutive_errors"] == 3
    assert second["next_attempt_at"] == checkpoints._utc(
        second_retry + timedelta(hours=spec.cadence_hours)
    )
