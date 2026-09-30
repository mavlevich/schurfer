"""Database endpoint shared by integration tests and the local verify runner.

The default is the existing CI/local test service. A verify run supplies a
unique database name on a loopback-only Docker port. Ambient DATABASE_URL is
deliberately ignored so a production shell setting cannot redirect tests.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

_DEFAULT_URL = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"
_VERIFY_DB_NAME = re.compile(r"schurfer_verify_[0-9a-f]{12}\Z")


def _endpoint(url: str) -> tuple[str | None, int | None, str, str | None]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"postgresql", "postgresql+psycopg"}:
        raise RuntimeError("test database URL must use PostgreSQL")
    if parsed.query or parsed.fragment:
        raise RuntimeError("test database URL cannot override its endpoint through parameters")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("test database URL has an invalid port") from exc
    database = parsed.path.lstrip("/")
    if "/" in database or not database:
        raise RuntimeError("test database URL has an invalid database name")
    return parsed.hostname, port, database, parsed.username


def assert_active_test_database_url(url: str) -> None:
    """Reject any database except the configured disposable DB or CI default."""
    host, port, database, user = _endpoint(url)
    configured = os.environ.get("SCHURFER_TEST_DATABASE_URL")
    if configured:
        expected_host, expected_port, expected_database, expected_user = _endpoint(configured)
        if (
            expected_host != "127.0.0.1"
            or expected_port is None
            or not _VERIFY_DB_NAME.fullmatch(expected_database)
            or expected_user != "schurfer"
        ):
            raise RuntimeError("configured test database is not a disposable loopback database")
        allowed = (expected_host, expected_port, expected_database, expected_user)
        actual = (host, port, database, user)
    else:
        allowed = ("localhost", 5432, "schurfer", "schurfer")
        actual = ("localhost" if host == "127.0.0.1" else host, port, database, user)
    if actual != allowed:
        raise RuntimeError("database connection is outside the active local test database")


def integration_database_url(*, sqlalchemy: bool = False) -> str:
    url = os.environ.get("SCHURFER_TEST_DATABASE_URL", _DEFAULT_URL)
    assert_active_test_database_url(url)
    return url.replace("postgresql://", "postgresql+psycopg://", 1) if sqlalchemy else url
