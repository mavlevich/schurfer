"""Regression for accidental production DB and notification I/O in tests."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from schurfer_execution.notify import notify_alert
from schurfer_journal.testing_database import (
    assert_active_test_database_url,
    integration_database_url,
)

from conftest import _reject_required_database_skip


def test_external_socket_fails_before_contact() -> None:
    with (
        socket.socket() as sock,
        pytest.raises(pytest.fail.Exception, match="external network connection"),
    ):
        sock.connect(("203.0.113.10", 443))


def test_external_connect_ex_fails_before_contact() -> None:
    with (
        socket.socket() as sock,
        pytest.raises(pytest.fail.Exception, match="external network connection"),
    ):
        sock.connect_ex(("203.0.113.10", 443))


def test_external_udp_send_fails_before_contact() -> None:
    with (
        socket.socket(type=socket.SOCK_DGRAM) as sock,
        pytest.raises(pytest.fail.Exception, match="external network connection"),
    ):
        sock.sendto(b"test", ("203.0.113.10", 53))


def test_external_name_fails_before_dns_lookup() -> None:
    with pytest.raises(pytest.fail.Exception, match="external network connection"):
        socket.getaddrinfo("api.telegram.org", 443)


def test_production_database_fails_before_contact() -> None:
    with pytest.raises(pytest.fail.Exception, match="outside the active local test database"):
        psycopg.connect("postgresql://schurfer@db.example.invalid:5432/schurfer")


def test_psycopg_class_connect_fails_before_contact() -> None:
    with pytest.raises(pytest.fail.Exception, match="outside the active local test database"):
        psycopg.Connection.connect("postgresql://schurfer@db.example.invalid:5432/schurfer")


async def test_async_production_database_fails_before_contact() -> None:
    with pytest.raises(pytest.fail.Exception, match="outside the active local test database"):
        await psycopg.AsyncConnection.connect("postgresql://schurfer@127.0.0.1:15432/schurfer")


async def test_unmocked_notification_fails_loudly() -> None:
    with pytest.raises(pytest.fail.Exception, match="external network connection"):
        await notify_alert("test-token", "test-chat", text="test")


def test_disposable_database_rejects_shared_and_tunnel_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disposable = "postgresql://schurfer:x@127.0.0.1:49123/schurfer_verify_abcdef123456"
    monkeypatch.setenv("SCHURFER_TEST_DATABASE_URL", disposable)
    monkeypatch.setenv("DATABASE_URL", "postgresql://schurfer:x@db.example.invalid:5432/live")
    assert integration_database_url() == disposable
    assert integration_database_url(sqlalchemy=True).startswith("postgresql+psycopg://")
    assert_active_test_database_url(disposable)
    for wrong in (
        "postgresql://schurfer:x@localhost:5432/schurfer",
        "postgresql://schurfer:x@127.0.0.1:15432/schurfer",
        "postgresql://schurfer:x@127.0.0.1:49123/other_db",
        "postgresql://schurfer:x@db.example.invalid:49123/schurfer_verify_abcdef123456",
        disposable + "?hostaddr=db.example.invalid",
    ):
        with pytest.raises(RuntimeError):
            assert_active_test_database_url(wrong)


def test_configured_database_must_have_disposable_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SCHURFER_TEST_DATABASE_URL", "postgresql://schurfer:x@127.0.0.1:49123/schurfer"
    )
    with pytest.raises(RuntimeError, match="not a disposable loopback database"):
        integration_database_url()


def test_default_database_ignores_ambient_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCHURFER_TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://schurfer:x@127.0.0.1:15432/production")
    assert integration_database_url() == (
        "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"
    )


def test_required_database_skip_becomes_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUIRE_INTEGRATION_DB", "1")
    report = pytest.TestReport(
        nodeid="test_db.py::test_query",
        location=("test_db.py", 1, "test_query"),
        keywords={},
        outcome="skipped",
        longrepr=("test_db.py", 1, "Skipped: no local postgres reachable"),
        when="call",
    )
    _reject_required_database_skip(report)
    assert report.failed
    assert "forbids skipping" in str(report.longrepr)


def test_unrelated_skip_stays_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUIRE_INTEGRATION_DB", "1")
    report = pytest.TestReport(
        nodeid="test_shell.py::test_gnu_date",
        location=("test_shell.py", 1, "test_gnu_date"),
        keywords={},
        outcome="skipped",
        longrepr=("test_shell.py", 1, "Skipped: GNU date is unavailable on macOS"),
        when="call",
    )
    _reject_required_database_skip(report)
    assert report.skipped


def test_pytest_fails_on_required_database_skip(tmp_path: Path) -> None:
    test_file = tmp_path / "test_required_database_skip.py"
    test_file.write_text(
        "import pytest\n"
        "def test_database(): pytest.skip('no local postgres reachable')\n"
        "def test_optional_tool(): pytest.skip('GNU date is unavailable on macOS')\n"
    )
    repo_root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env["REQUIRE_INTEGRATION_DB"] = "1"
    env["PYTHONPATH"] = str(repo_root)
    result = subprocess.run(  # noqa: S603 - fixed interpreter and generated local test file
        [sys.executable, "-m", "pytest", "-p", "conftest", str(test_file), "-q"],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 failed, 1 skipped" in result.stdout
