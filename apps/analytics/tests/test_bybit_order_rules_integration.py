"""Real PostgreSQL checks for the Bybit order-rule capture (migration 0060).

Runs on the disposable database of `make verify`. Every row it writes carries the `RLX`
marker and is removed before and after each test.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import ccxt
import psycopg
import pytest
from schurfer_analytics import bybit_order_rules as rules
from schurfer_journal.testing_database import integration_database_url
from sqlalchemy.ext.asyncio import create_async_engine

DSN = integration_database_url()
T0 = datetime(2026, 10, 11, 12, tzinfo=UTC)


def _connect() -> psycopg.Connection[Any]:
    try:
        conn = psycopg.connect(DSN, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise
        pytest.skip(f"no local postgres reachable: {exc}")
    if conn.execute("SELECT to_regclass('app.bybit_order_rule_captures')").fetchone() == (None,):
        conn.close()
        pytest.skip("migration 0060 is not applied")
    return conn


def _clean(conn: psycopg.Connection[Any]) -> None:
    conn.execute("DELETE FROM app.bybit_order_rule_captures WHERE source_version = %s",
                 (rules.SOURCE_VERSION,))  # fmt: skip
    conn.execute("DELETE FROM app.bybit_order_rule_defs WHERE symbol LIKE 'RLX%'")


@pytest.fixture
def db() -> Any:
    conn = _connect()
    _clean(conn)
    yield conn
    _clean(conn)
    conn.close()


def _item(symbol: str, step: str = "0.1") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "status": "Trading",
        "lotSizeFilter": {
            "qtyStep": step,
            "minOrderQty": step,
            "minNotionalValue": "5",
            "maxMktOrderQty": "100000",
        },
        "priceFilter": {"tickSize": "0.001"},
    }


class _Venue:
    def __init__(self, items: list[dict[str, Any]] | None) -> None:
        self.items = items

    async def public_get_v5_market_instruments_info(self, _params: dict[str, Any]) -> Any:
        if self.items is None:
            return {"retCode": 10006, "retMsg": "Too many visits"}
        return {"retCode": 0, "result": {"list": self.items, "nextPageCursor": ""}}


def _capture(items: list[dict[str, Any]] | None, at: datetime) -> dict[str, Any]:
    return asyncio.run(rules.capture(DSN, _Venue(items), now=at))


def _rule_at(symbol: str, at: datetime) -> rules.OrderRule | None:
    from schurfer_analytics.outcome_repository import async_database_url

    async def run() -> rules.OrderRule | None:
        engine = create_async_engine(async_database_url(DSN))
        try:
            async with engine.connect() as conn:
                return await rules.rule_at(conn, symbol, at)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_an_unchanged_instrument_is_stored_once(db: Any) -> None:
    first = _capture([_item("RLXA"), _item("RLXB")], T0)
    second = _capture([_item("RLXA"), _item("RLXB")], T0 + timedelta(hours=1))
    assert (first["status"], first["new_rules"], first["instruments"]) == ("complete", 2, 2)
    assert (second["status"], second["new_rules"]) == ("complete", 0)
    assert db.execute(
        "SELECT count(*) FROM app.bybit_order_rule_defs WHERE symbol LIKE 'RLX%'"
    ).fetchone() == (2,)
    members = db.execute(
        "SELECT members FROM app.bybit_order_rule_captures ORDER BY captured_at DESC LIMIT 1"
    ).fetchone()[0]
    assert members == {s: rules.rule_sha256(_item(s)) for s in ("RLXA", "RLXB")}


def test_a_decision_gets_the_rules_valid_at_its_time(db: Any) -> None:
    _capture([_item("RLXA", "0.1")], T0)
    _capture([_item("RLXA", "1")], T0 + timedelta(hours=1))
    before = _rule_at("RLXA", T0 + timedelta(minutes=30))
    after = _rule_at("RLXA", T0 + timedelta(hours=1, minutes=5))
    assert before is not None and before.qty_step == Decimal("0.1")
    assert after is not None and after.qty_step == Decimal("1")
    assert rules.size_order(after, price=Decimal("3.7")).qty == Decimal(13)


def test_no_fresh_complete_capture_means_no_rule(db: Any) -> None:
    assert _rule_at("RLXA", T0) is None  # nothing captured yet
    _capture([_item("RLXA")], T0)
    assert _rule_at("RLXA", T0 - timedelta(seconds=1)) is None  # before the capture
    assert _rule_at("RLXZ", T0 + timedelta(minutes=1)) is None  # not listed
    stale = T0 + rules.MAX_RULE_AGE + timedelta(seconds=1)
    assert _rule_at("RLXA", stale) is None
    failed = _capture(None, T0 + timedelta(hours=1))
    assert failed["status"] == "failed" and "Too many visits" in failed["error"]
    assert _rule_at("RLXA", stale) is None  # a failed pass refreshes nothing
    assert db.execute(
        "SELECT members FROM app.bybit_order_rule_captures WHERE status = 'failed'"
    ).fetchone() == ({},)


class _Broken:
    def __init__(self, error: BaseException | None = None, *, delay: float = 0.0) -> None:
        self.error, self.delay = error, delay

    async def public_get_v5_market_instruments_info(self, _params: dict[str, Any]) -> Any:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}


@pytest.mark.parametrize(
    ("venue", "kind"),
    [
        (_Broken(ccxt.RateLimitExceeded("bybit 10006 Too many visits")), "RateLimitExceeded"),
        (_Broken(ccxt.NetworkError("connection reset")), "NetworkError"),
        (_Broken(OSError("unreachable")), "OSError"),
        (_Broken(delay=1.0), "TimeoutError"),
    ],
)
def test_a_client_failure_is_recorded_as_a_failed_pass(
    db: Any, monkeypatch: pytest.MonkeyPatch, venue: _Broken, kind: str
) -> None:
    monkeypatch.setattr(rules, "REQUEST_TIMEOUT_SECONDS", 0.05)
    summary = asyncio.run(rules.capture(DSN, venue, now=T0))
    assert summary["status"] == "failed"
    assert summary["error"].startswith(kind)
    assert db.execute(
        "SELECT status, members, error LIKE %s FROM app.bybit_order_rule_captures",
        (f"{kind}%",),
    ).fetchall() == [("failed", {}, True)]


def _fresh(at: datetime) -> dict[str, Any]:
    return asyncio.run(rules.check_fresh(DSN, now=at))


def test_health_needs_a_complete_capture_within_two_hours(db: Any) -> None:
    assert _fresh(T0) == {
        "healthy": False,
        "problems": ["no complete capture"],
        "last_complete_at": None,
        "last_status": None,
    }
    _capture([_item("RLXA")], T0)
    assert _fresh(T0 + timedelta(hours=1))["healthy"] is True
    _capture(None, T0 + timedelta(hours=1))  # a failed pass after a fresh one
    later = _fresh(T0 + timedelta(hours=1, minutes=30))
    assert (later["healthy"], later["last_status"]) == (True, "failed")
    stale = _fresh(T0 + rules.MAX_RULE_AGE + timedelta(seconds=1))
    assert stale["healthy"] is False and "older than" in stale["problems"][0]
