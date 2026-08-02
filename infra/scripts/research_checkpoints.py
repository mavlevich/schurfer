#!/usr/bin/env python3
"""Run bounded production research checkpoints and publish sanitized status."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SNAPSHOT_VERSION = "research_checkpoints_v1"
TERMINAL_STATES = {
    "decision_ready",
    "discovery_ready",
    "no_go",
    "shadow_candidate",
    "boundary_only_ready",
}
NOTIFIABLE_STATES = TERMINAL_STATES | {
    "collecting",
    "awaiting_complete_resolution",
    "directional",
    "blocked_resources",
    "error",
}
CHECKPOINT_STATES = NOTIFIABLE_STATES | {"scheduled", "due"}


@dataclass(frozen=True)
class CheckpointSpec:
    key: str
    title: str
    contract: str
    due_at: datetime
    make_target: str
    cadence_hours: int
    timeout_minutes: int
    min_headroom_mb: int
    min_disk_gb: int = 5


CHECKPOINTS = (
    CheckpointSpec(
        "orderflow",
        "Bybit order-flow discovery",
        "bybit_orderflow_pilot_v1",
        datetime(2026, 8, 6, 18, 15, tzinfo=UTC),
        "prod-orderflow-pilot-report",
        24,
        20,
        512,
    ),
    CheckpointSpec(
        "liquid_taker",
        "Liquid-taker candidate",
        "liquid_taker_candidate_v1",
        datetime(2026, 8, 27, tzinfo=UTC),
        "prod-liquid-taker-report",
        168,
        60,
        1280,
    ),
    CheckpointSpec(
        "liquid_taker_wider",
        "Liquid-taker wider-stop shadow",
        "liquid_taker_wider_stop_shadow_v1",
        datetime(2026, 8, 29, tzinfo=UTC),
        "prod-liquid-taker-wider-stop-report",
        168,
        60,
        1280,
    ),
    CheckpointSpec(
        "open_ended_margin",
        "Open-ended margin boundary",
        "prospective_no_time_exit_margin_buffer_v1",
        datetime(2026, 8, 17, tzinfo=UTC),
        "prod-open-ended-margin-report",
        24,
        60,
        1280,
    ),
    CheckpointSpec(
        "exit_liquidity",
        "Exit quote calibration",
        "exit_liquidity_calibration_v1",
        datetime(2026, 7, 30, tzinfo=UTC),
        "prod-exit-liquidity-calibration-report",
        24,
        15,
        512,
    ),
)
logger = logging.getLogger("research-checkpoints")


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("checkpoint timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, payload: dict[str, Any], *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _default_row(spec: CheckpointSpec) -> dict[str, Any]:
    return {
        "key": spec.key,
        "title": spec.title,
        "contract": spec.contract,
        "due_at": _utc(spec.due_at),
        "state": "scheduled",
        "next_attempt_at": _utc(spec.due_at),
        "last_attempt_at": None,
        "last_success_at": None,
        "report_status": None,
        "verdict": None,
        "report_file": None,
        "report_sha256": None,
        "error": None,
        "notified_state": None,
    }


def _validated_outcome(status: str, verdict: str | None, state: str) -> tuple[str, str | None, str]:
    if state not in CHECKPOINT_STATES:
        raise ValueError(f"report produced unsupported checkpoint state: {state}")
    return status, verdict, state


def _report_outcome(key: str, report: dict[str, Any]) -> tuple[str, str | None, str]:
    if key == "orderflow":
        status = str(report.get("readiness", "invalid"))
        verdict = "discovery_ready" if status == "discovery_ready" else "withheld"
        return _validated_outcome(status, verdict, status)
    if key == "exit_liquidity":
        readiness = report.get("readiness")
        if not isinstance(readiness, dict):
            raise ValueError("exit-liquidity report has no readiness object")
        status = str(readiness.get("state", "invalid"))
        verdict = status if status != "collecting" else "withheld"
        return _validated_outcome(status, verdict, status)
    if key in {"liquid_taker", "liquid_taker_wider"}:
        inference = report.get("formal_inference")
        if not isinstance(inference, dict):
            raise ValueError("liquid-taker report has no formal_inference object")
        status = str(inference.get("status", "invalid"))
        verdict = str(inference.get("verdict", "withheld"))
        state = verdict if verdict != "withheld" else status
        if verdict == "do_not_promote":
            state = "no_go"
        return _validated_outcome(status, verdict, state)
    if key == "open_ended_margin":
        gate = report.get("capital_efficiency_gate")
        if not isinstance(gate, dict):
            raise ValueError("open-ended report has no capital_efficiency_gate object")
        status = str(gate.get("state", "invalid"))
        state = "no_go" if status == "no_go_capital_efficiency" else status
        return _validated_outcome(status, status, state)
    raise ValueError(f"unknown checkpoint key: {key}")


def _headroom_mb(meminfo_path: Path = Path("/proc/meminfo")) -> int:
    values: dict[str, int] = {}
    for line in meminfo_path.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in {"MemAvailable:", "SwapFree:"}:
            values[parts[0]] = int(parts[1])
    if "MemAvailable:" not in values or "SwapFree:" not in values:
        raise ValueError("MemAvailable or SwapFree missing from /proc/meminfo")
    return (values["MemAvailable:"] + values["SwapFree:"]) // 1024


def _disk_free_gb(path: Path) -> int:
    return shutil.disk_usage(path).free // (1024**3)


def _notify(message: str) -> str | None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return "telegram credentials are missing"
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            if response.status >= 300:
                return f"telegram status {response.status}"
    except OSError as exc:
        return f"telegram delivery failed ({type(exc).__name__})"
    return None


def _notification(row: dict[str, Any], previous_state: str | None) -> str | None:
    state = str(row["state"])
    if state not in NOTIFIABLE_STATES or state == previous_state:
        return None
    if state in {"decision_ready", "shadow_candidate"}:
        prefix = "✅"
    elif state == "no_go":
        prefix = "🛑"
    elif state in {"discovery_ready", "boundary_only_ready"}:
        prefix = "🔬"
    elif state in {"blocked_resources", "error"}:
        prefix = "⚠️"
    else:
        prefix = "⏳"
    return (
        f"{prefix} Research checkpoint: {row['title']}\n"
        f"State: {state}\nVerdict: {row.get('verdict') or 'n/a'}\n"
        f"Report: {row.get('report_file') or 'not produced'}"
    )


def _run_report(
    spec: CheckpointSpec,
    *,
    now: datetime,
    root: Path,
    report_dir: Path,
) -> tuple[dict[str, Any], Path, str]:
    cutoff = _utc(now)
    command = [
        "make",
        spec.make_target,
        f"ARGS=--until {cutoff} --format json",
    ]
    docker_config = root / "runtime" / "checkpoint-docker-config"
    docker_config.mkdir(parents=True, exist_ok=True)
    docker_config.chmod(0o700)
    logger.info("running %s with cutoff %s", spec.make_target, cutoff)
    process = subprocess.Popen(  # noqa: S603 -- fixed registered make targets
        command,
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        env={**os.environ, "DOCKER_CONFIG": str(docker_config)},
        start_new_session=True,
    )
    try:
        stdout, _ = process.communicate(timeout=spec.timeout_minutes * 60)
    except subprocess.TimeoutExpired as exc:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise RuntimeError(
            f"{spec.make_target} exceeded {spec.timeout_minutes} minute limit"
        ) from exc
    if process.returncode != 0:
        raise RuntimeError(
            f"{spec.make_target} exited {process.returncode}; inspect systemd journal"
        )
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{spec.make_target} did not produce JSON: {exc}") from exc
    if not isinstance(report, dict):
        raise RuntimeError(f"{spec.make_target} produced a non-object JSON report")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_dir.chmod(0o700)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    report_path = report_dir / f"{spec.key}-{stamp}.json"
    _atomic_json(report_path, report, mode=0o600)
    digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    logger.info("archived %s as %s", spec.make_target, report_path.name)
    return report, report_path, digest


def run_once(
    *,
    now: datetime,
    root: Path,
    snapshot_path: Path,
    report_dir: Path,
    force_key: str | None = None,
) -> dict[str, Any]:
    previous = _read_json(snapshot_path)
    if previous.get("version") != SNAPSHOT_VERSION:
        previous = {}
    old_rows = {
        row.get("key"): row
        for row in previous.get("checkpoints", [])
        if isinstance(row, dict) and isinstance(row.get("key"), str)
    }
    rows = []
    for spec in CHECKPOINTS:
        old_row = old_rows.get(spec.key, {})
        if old_row.get("contract") != spec.contract:
            old_row = {}
        row = {**_default_row(spec), **old_row}
        row.update({"title": spec.title, "contract": spec.contract, "due_at": _utc(spec.due_at)})
        if now < spec.due_at and row["state"] not in TERMINAL_STATES:
            row.update(state="scheduled", next_attempt_at=_utc(spec.due_at), error=None)
        elif row["state"] == "scheduled":
            row.update(state="due", next_attempt_at=_utc(now), error=None)
        rows.append(row)

    selected: tuple[CheckpointSpec, dict[str, Any]] | None = None
    for spec, row in zip(CHECKPOINTS, rows, strict=True):
        if force_key is not None and spec.key != force_key:
            continue
        if row["state"] in TERMINAL_STATES and force_key is None:
            continue
        next_attempt = _parse_utc(row.get("next_attempt_at"))
        cadence_due = next_attempt is None or now >= next_attempt
        if now >= spec.due_at and (cadence_due or force_key == spec.key):
            selected = (spec, row)
            break

    if selected is not None:
        spec, row = selected
        row["last_attempt_at"] = _utc(now)
        row["next_attempt_at"] = _utc(now + timedelta(hours=spec.cadence_hours))
        try:
            headroom_mb = _headroom_mb()
            disk_free_gb = _disk_free_gb(root)
            if headroom_mb < spec.min_headroom_mb or disk_free_gb < spec.min_disk_gb:
                row.update(
                    state="blocked_resources",
                    error=(
                        f"requires {spec.min_headroom_mb} MiB headroom and "
                        f"{spec.min_disk_gb} GiB disk; observed {headroom_mb} MiB and "
                        f"{disk_free_gb} GiB"
                    ),
                    next_attempt_at=_utc(now + timedelta(hours=1)),
                )
            else:
                report, report_path, digest = _run_report(
                    spec,
                    now=now,
                    root=root,
                    report_dir=report_dir,
                )
                report_status, verdict, state = _report_outcome(spec.key, report)
                row.update(
                    state=state,
                    report_status=report_status,
                    verdict=verdict,
                    report_file=report_path.name,
                    report_sha256=digest,
                    last_success_at=_utc(now),
                    error=None,
                )
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            row.update(state="error", error=str(exc))

    for row in rows:
        previous_state = str(row.get("notified_state") or "") or None
        message = _notification(row, previous_state)
        if message is not None:
            alert_error = _notify(message)
            if alert_error is None:
                row["notified_state"] = row["state"]
                row.pop("alert_error", None)
            else:
                row["alert_error"] = alert_error

    payload = {
        "version": SNAPSHOT_VERSION,
        "generated_at": _utc(now),
        "runner_state": "idle",
        "checkpoints": rows,
    }
    _atomic_json(snapshot_path, payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/opt/schurfer"))
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=Path("/opt/schurfer/runtime/research-checkpoints.json"),
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path("/opt/schurfer/backups/reports/automated"),
    )
    parser.add_argument("--force", choices=tuple(spec.key for spec in CHECKPOINTS))
    parser.add_argument("--now", type=lambda value: _parse_utc(value), help=argparse.SUPPRESS)
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args()
    lock_path = args.snapshot.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("research checkpoint runner is already active") from None
        run_once(
            now=args.now or datetime.now(UTC),
            root=args.root,
            snapshot_path=args.snapshot,
            report_dir=args.report_dir,
            force_key=args.force,
        )


if __name__ == "__main__":
    main()
