"""Regression for accidental production DB and notification I/O in tests."""

from __future__ import annotations

import socket

import psycopg
import pytest
from schurfer_execution.notify import notify_alert


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
    with pytest.raises(pytest.fail.Exception, match="outside local schurfer Postgres"):
        psycopg.connect("postgresql://schurfer@db.example.invalid:5432/schurfer")


def test_sqlalchemy_database_path_fails_before_contact() -> None:
    with pytest.raises(pytest.fail.Exception, match="outside local schurfer Postgres"):
        psycopg.Connection.connect("postgresql://schurfer@db.example.invalid:5432/schurfer")


async def test_async_production_database_fails_before_contact() -> None:
    with pytest.raises(pytest.fail.Exception, match="outside local schurfer Postgres"):
        await psycopg.AsyncConnection.connect("postgresql://schurfer@127.0.0.1:15432/schurfer")


async def test_unmocked_notification_fails_loudly() -> None:
    with pytest.raises(pytest.fail.Exception, match="external network connection"):
        await notify_alert("test-token", "test-chat", text="test")
