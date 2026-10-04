"""infra/scripts/disk_growth_reading.py: the daily read-only disk-growth reading."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
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
