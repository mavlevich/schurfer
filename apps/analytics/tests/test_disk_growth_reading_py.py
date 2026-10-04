"""infra/scripts/disk_growth_reading.py: the daily read-only disk-growth reading."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from schurfer_journal.testing_database import integration_database_url

_SCRIPT = Path(__file__).resolve().parents[3] / "infra/scripts/disk_growth_reading.py"
_spec = importlib.util.spec_from_file_location("disk_growth_reading", _SCRIPT)
assert _spec is not None and _spec.loader is not None
dg = importlib.util.module_from_spec(_spec)
sys.modules["disk_growth_reading"] = dg
_spec.loader.exec_module(dg)


def test_the_sql_reads_only_catalog_and_statistics_views() -> None:
    sources = set(re.findall(r"\b(?:FROM|JOIN)\s+([a-z_.]+)", dg.SQL))
    allowed = ("pg_", "timescaledb_information.", "_timescaledb_catalog.")
    assert sources and all(name.startswith(allowed) for name in sources), sources
    assert not re.search(r"\b(?:app|timeseries)\.[a-z]", dg.SQL)


def test_parsers() -> None:
    docker = '{"Type":"Images","Size":"5.2GB"}\n\n{"Type":"Build Cache","Size":"3.4GB"}\n'
    assert [d["Type"] for d in dg.docker_summary(docker)] == ["Images", "Build Cache"]


def test_unreadable_directories_are_reported_not_fatal(tmp_path: Path) -> None:
    readable = tmp_path / "readable"
    readable.mkdir()
    (readable / "f").write_bytes(b"x" * 10)
    locked = tmp_path / "locked"
    (locked / "inner").mkdir(parents=True)
    locked.chmod(0o000)
    try:
        result = dg._runtime([str(readable), str(locked)])
    finally:
        locked.chmod(0o755)
    assert result["sizes"]["readable"] == 10
    if os.geteuid() != 0:  # root reads everything
        assert any("locked" in path for path in result["unreadable"])


def test_a_reading_is_written_whole_with_its_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dg, "database_reading", lambda: {"database_bytes": 1})
    monkeypatch.setattr(dg, "_run", lambda args, **_k: '{"Type":"Images"}\n')
    (tmp_path / "runtime" / "research").mkdir(parents=True)
    out = tmp_path / "runtime" / "research" / "disk-growth"
    assert dg.main(["--out-dir", str(out), "--runtime-dir", str(tmp_path / "runtime")]) == 0
    (reading,) = out.glob("reading-*.json")
    body = reading.read_bytes()
    assert (out / (reading.name + ".sha256")).read_text().strip() == hashlib.sha256(
        body
    ).hexdigest()
    payload = json.loads(body)
    assert payload["version"] == dg.VERSION and payload["database"] == {"database_bytes": 1}
    assert payload["filesystem"]["free"] > 0 and "research" in payload["runtime_dirs"]["sizes"]
    assert not list(out.glob(".*partial"))


def test_a_failed_reading_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> Any:
        raise dg.ReadingError("psql failed")

    monkeypatch.setattr(dg, "database_reading", broken)
    (tmp_path / "runtime").mkdir()
    out = tmp_path / "out"
    assert dg.main(["--out-dir", str(out), "--runtime-dir", str(tmp_path / "runtime")]) == 1
    assert not out.exists() or not list(out.iterdir())


def test_the_sql_runs_on_the_real_schema() -> None:
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(integration_database_url(), autocommit=True, connect_timeout=2)
    except psycopg.OperationalError as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise
        pytest.skip(f"no local postgres reachable: {exc}")
    with conn:
        row = conn.execute(dg.SQL).fetchone()
    assert row is not None
    payload = row[0]
    tables = {t["table"] for t in payload["tables"]}
    assert "app.momentum_flow_paper_probes" in tables
    probe = next(t for t in payload["tables"] if t["table"] == "app.momentum_flow_paper_probes")
    assert {"heap_bytes", "toast_bytes", "index_bytes", "n_tup_upd", "n_dead_tup"} <= set(probe)
    hypertables = {h["hypertable"] for h in payload["hypertables"]}
    assert "timeseries.momentum_flow_watch_evaluations_1m" in hypertables
    assert payload["database_bytes"] > 0 and "temp_bytes" in payload["temp"]


def test_a_failed_hash_write_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.write_bytes

    def fail_hash(self: Path, data: bytes) -> int:
        if ".sha256" in self.name:
            raise OSError("sidecar write failed: disk full")
        return original(self, data)

    monkeypatch.setattr(Path, "write_bytes", fail_hash)
    monkeypatch.setattr(dg, "take_reading", lambda _r: {"taken_at": "2026-10-04T12:00:00+00:00"})
    assert dg.main(["--out-dir", str(tmp_path)]) == 1
    assert list(tmp_path.iterdir()) == []


def _reading(out: Path, taken: datetime, *, version: str = dg.VERSION) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    written: Path = dg.write_reading(out, {"version": version, "taken_at": taken.isoformat()})
    return written


NOW = datetime(2026, 10, 10, 13, tzinfo=UTC)


def test_check_latest_names_every_problem(tmp_path: Path) -> None:
    out = tmp_path / "dg"
    assert dg.check_latest(out, now=NOW) == [f"no reading in {out}"]
    _reading(out, NOW - timedelta(days=3))
    assert "older than" in dg.check_latest(out, now=NOW)[0]
    latest = _reading(out, NOW - timedelta(hours=1))
    assert dg.check_latest(out, now=NOW) == []
    latest.with_name(latest.name + ".sha256").write_text("0" * 64 + "\n")
    assert "does not match" in dg.check_latest(out, now=NOW)[0]
    latest.with_name(latest.name + ".sha256").unlink()
    assert "has no .sha256" in dg.check_latest(out, now=NOW)[0]
    _reading(out, NOW, version="other")
    assert "is 'other'" in dg.check_latest(out, now=NOW)[0]


ROOT = Path(__file__).resolve().parents[3]
FAKE_SYSTEMCTL = """#!/bin/sh
case "$1" in
  is-active) echo "${TIMER_STATE:-active}" ;;
  show) echo "${RUN_RESULT:-success}" ;;
esac
"""


@pytest.mark.parametrize(
    ("timer", "result", "age_hours", "healthy"),
    [
        ("active", "success", 1, True),
        ("inactive", "success", 1, False),
        ("active", "exit-code", 1, False),
        ("active", "success", 48, False),
    ],
)
def test_the_health_target_fails_unless_everything_holds(
    tmp_path: Path, timer: str, result: str, age_hours: int, healthy: bool
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "systemctl"
    fake.write_text(FAKE_SYSTEMCTL)
    fake.chmod(0o755)
    out = tmp_path / "dg"
    _reading(out, datetime.now(UTC) - timedelta(hours=age_hours))
    run = subprocess.run(  # noqa: S603 -- the repository's own make target
        ["/usr/bin/make", "-s", "prod-disk-growth-health", f"DISK_GROWTH_DIR={out}"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "TIMER_STATE": timer,
            "RUN_RESULT": result,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (run.returncode == 0) is healthy, run.stdout + run.stderr
