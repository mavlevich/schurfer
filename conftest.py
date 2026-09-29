"""Fail fast if a Python test accidentally reaches an external service.

Integration tests use the local PostgreSQL service on port 5432. Everything
else should be replaced with a test double. In particular, a truthy MagicMock
config must never turn a unit test into a real database or Telegram request.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

if TYPE_CHECKING:
    from collections.abc import Iterator


def _check_test_database(conninfo: str, kwargs: dict[str, Any]) -> None:
    try:
        parts = conninfo_to_dict(conninfo, **kwargs)
    except Exception as exc:
        pytest.fail(f"test attempted a database connection with invalid conninfo: {exc}")
    host = parts.get("host")
    port = str(parts.get("port", "5432"))
    dbname = parts.get("dbname")
    if host not in {"localhost", "127.0.0.1"} or port != "5432" or dbname != "schurfer":
        pytest.fail(
            "test attempted a database connection outside local schurfer Postgres "
            "(localhost:5432/schurfer); patch the call or use the integration database",
        )


def _check_test_host(host: object) -> None:
    if host is None:
        return  # Local bind/address discovery.
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    if not isinstance(host, str):
        pytest.fail("test attempted a socket connection with an unknown host")
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host == "localhost"
    if not is_loopback:
        pytest.fail(
            "test attempted an external network connection; mock the client in this test",
        )


def _check_test_socket(address: object) -> None:
    if isinstance(address, tuple) and len(address) >= 2:
        _check_test_host(address[0])


@pytest.fixture(autouse=True)
def block_unexpected_external_io(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep accidental outbound I/O from making verify slow or unsafe."""
    for proxy_name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(proxy_name, raising=False)
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto
    original_getaddrinfo = socket.getaddrinfo
    original_psycopg_connect = psycopg.connect
    original_sync_connect = psycopg.Connection.connect
    original_async_connect = psycopg.AsyncConnection.connect

    def connect(sock: socket.socket, address: object) -> None:
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            _check_test_socket(address)
        original_connect(sock, address)

    def connect_ex(sock: socket.socket, address: object) -> int:
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            _check_test_socket(address)
        return original_connect_ex(sock, address)

    def sendto(sock: socket.socket, *args: Any) -> int:
        if sock.family in {socket.AF_INET, socket.AF_INET6} and args:
            _check_test_socket(args[-1])
        return original_sendto(sock, *args)

    def getaddrinfo(host: object, *args: Any, **kwargs: Any) -> Any:
        _check_test_host(host)
        return original_getaddrinfo(host, *args, **kwargs)

    def pg_connect(conninfo: str = "", **kwargs: Any) -> Any:
        _check_test_database(conninfo, kwargs)
        return original_psycopg_connect(conninfo, **kwargs)

    def pg_sync_connect(
        cls: type[psycopg.Connection[Any]], conninfo: str = "", **kwargs: Any
    ) -> Any:
        _check_test_database(conninfo, kwargs)
        return original_sync_connect(conninfo, **kwargs)

    async def pg_async_connect(
        cls: type[psycopg.AsyncConnection[Any]], conninfo: str = "", **kwargs: Any
    ) -> Any:
        _check_test_database(conninfo, kwargs)
        return await original_async_connect(conninfo, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(psycopg, "connect", pg_connect)
    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(pg_sync_connect))
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", classmethod(pg_async_connect))
    yield
