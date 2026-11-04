"""Real-Postgres tests of LIVE_PROBE authorization records and reservations
(docs/research/live-probe-step2-design-v1.md, sections 2 and 4;
docs/engineering/live-probe-loss-threshold-design-v1.md, sections 3 and 5)."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
import structlog.testing
from psycopg.rows import dict_row
from schurfer_execution import live_probe as lp
from schurfer_execution import probe_loss as pl
from schurfer_journal.testing_database import (
    assert_active_test_database_url,
    integration_database_url,
)
from schurfer_performance import LIVE_EXCHANGE_COSTS_ACCOUNTING_VERSION

TEST_DATABASE_URL = integration_database_url()
D = Decimal
ACCOUNT = "probe-sub-1"
INSTRUMENT = "BTCUSDT"
# 10 x (0.05 + 0.0011) + 10 x 0.0001 x 3 = 0.514
INPUTS = pl.ReserveInputs(
    initial_sl_pct=D(5),
    taker_rate=D("0.00055"),
    funding_rate=D("0.0001"),
    settlements_in_max_hold=3,
)
RESERVE = D("0.514")
PREFIX = "tlp-"


async def _connect_or_skip() -> psycopg.AsyncConnection[dict[str, Any]]:
    assert_active_test_database_url(TEST_DATABASE_URL)
    try:
        connection = await psycopg.AsyncConnection.connect(
            TEST_DATABASE_URL, autocommit=True, row_factory=dict_row
        )
    except Exception as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise RuntimeError(
                f"REQUIRE_INTEGRATION_DB=1 but PostgreSQL/head is unavailable: {exc}"
            ) from exc
        pytest.skip(f"no local postgres/head reachable: {exc}")
    async with connection.cursor() as cursor:
        await cursor.execute("SELECT to_regclass('app.live_probe_authorizations') AS t")
        row = await cursor.fetchone()
    if row is None or row["t"] is None:
        await connection.close()
        pytest.skip("migration 0063 is not applied")
    return connection


async def _query(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    connection = await _connect_or_skip()
    try:
        async with connection.cursor() as cursor:
            await cursor.execute(sql, params)
            return await cursor.fetchall() if cursor.description else []
    finally:
        await connection.close()


def _probe_id() -> str:
    return f"{PREFIX}{uuid.uuid4().hex[:12]}"


@pytest.fixture(autouse=True)
def _cleanup() -> Any:
    """Test-only cleanup on the disposable database (the guards forbid deletes). A
    record left behind would refuse every later downgrade past 0063 in the same run."""
    yield
    try:
        conn = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    except psycopg.OperationalError:
        return
    with conn:
        if conn.execute("SELECT to_regclass('app.live_probe_authorizations')").fetchone() == (
            None,
        ):
            return
        with conn.transaction():
            conn.execute("SET LOCAL session_replication_role = replica")
            like = (PREFIX.replace("-", "\\-") + "%",)
            for table in (
                "live_probe_refusals",
                "live_probe_reservations",
                "live_probe_authorization_events",
                "live_probe_authorizations",
            ):
                conn.execute(f"DELETE FROM app.{table} WHERE probe_id LIKE %s", like)  # noqa: S608
            conn.execute(
                "DELETE FROM app.live_order_attempts WHERE setup_context @> "
                "'{\"live_probe_test\": true}'::jsonb"
            )
            conn.execute("DELETE FROM app.trades WHERE exchange = 'test_probe'")


def _terms(probe_id: str, **overrides: Any) -> lp.Terms:
    now = datetime.now(UTC)
    fields: dict[str, Any] = {
        "probe_id": probe_id,
        "environment": "testnet",
        "account_key": ACCOUNT,
        "strategy": "momentum_flow_hold12h",
        "contract_version": "hold12h_v1",
        "contract_sha256": "a" * 64,
        "instruments": (INSTRUMENT, "ETHUSDT"),
        "max_entries": 3,
        "notional_usd": D(10),
        "loss_threshold_usd": D(5),
        "entries_until": now + timedelta(days=1),
        "flat_by": now + timedelta(days=2),
    }
    fields.update(overrides)
    return lp.Terms(**fields)


async def _issue(**overrides: Any) -> str:
    await _connect_or_skip()
    probe_id = overrides.pop("probe_id", _probe_id())
    await lp.issue(TEST_DATABASE_URL, _terms(probe_id, **overrides), actor="owner")
    return probe_id


def _request(probe_id: str, decision_id: str | None = None, **overrides: Any) -> lp.EntryRequest:
    fields: dict[str, Any] = {
        "probe_id": probe_id,
        "decision_id": decision_id or uuid.uuid4().hex,
        "environment": "testnet",
        "account_key": ACCOUNT,
        "native_market_id": INSTRUMENT,
        "side": "long",
        "reserve_inputs": INPUTS,
    }
    fields.update(overrides)
    return lp.EntryRequest(**fields)


async def _reserve(probe_id: str, **overrides: Any) -> lp.Reservation:
    result = await lp.reserve(TEST_DATABASE_URL, _request(probe_id, **overrides))
    assert isinstance(result, lp.Reservation), result
    return result


async def _refusals(probe_id: str) -> list[str]:
    rows = await _query(
        "SELECT reason FROM app.live_probe_refusals WHERE probe_id = %s ORDER BY id", (probe_id,)
    )
    return [r["reason"] for r in rows]


async def _ledger(probe_id: str) -> lp.Ledger:
    connection = await _connect_or_skip()
    try:
        async with connection.cursor() as cursor:
            return await lp.ledger(cursor, probe_id)
    finally:
        await connection.close()


async def _attempt(
    reservation: lp.Reservation,
    *,
    client_order_id: str | None = None,
    exchange: str = "bybit",
    trade_id: int | None = None,
) -> int:
    """The entry attempt written for a reservation (its own clientOrderId by default)."""
    rows = await _query(
        "INSERT INTO app.live_order_attempts ("
        "operation, client_order_id, exchange, base, symbol, native_market_id, market_type, "
        "side, size_usd, requested_amount, leverage, exit_params, setup_context, status, "
        "trade_id) VALUES ('entry', %s, %s, 'BTC', 'BTC/USDT:USDT', %s, 'swap', %s, 10, "
        "0.0001, 1, '{}'::jsonb, '{\"live_probe_test\": true}'::jsonb, 'completed', %s) "
        "RETURNING id",
        (
            client_order_id or reservation.client_order_id,
            exchange,
            reservation.native_market_id,
            reservation.side,
            trade_id,
        ),
    )
    return int(rows[0]["id"])


async def _trade(*, status: str, pnl: str | None, accounting: str = "pending") -> int:
    strategy = await _query(
        "INSERT INTO app.strategies (name, version, description) VALUES ('probe-test', '1', 't') "
        "ON CONFLICT (name, version) DO UPDATE SET updated_at = now() RETURNING id"
    )
    rows = await _query(
        "INSERT INTO app.trades (strategy_id, symbol, exchange, market_type, side, size_usd, "
        "entry_price, entry_at, status, setup_context, pnl_usd, exit_at, accounting_version, "
        "accounting_status) VALUES (%s, 'BTC/USDT:USDT', 'test_probe', 'perp', 'long', 10, 1, "
        "now(), %s, '{}'::jsonb, %s, CASE WHEN %s = 'closed' THEN now() END, %s, %s) RETURNING id",
        (
            strategy[0]["id"],
            status,
            pnl,
            status,
            LIVE_EXCHANGE_COSTS_ACCOUNTING_VERSION,
            accounting,
        ),
    )
    return int(rows[0]["id"])


async def _placed(probe_id: str, *, trade_id: int | None) -> lp.Reservation:
    """A reservation sent, filled and (when given) linked to its trade."""
    reservation = await _reserve(probe_id)
    assert isinstance(await lp.begin_sending(TEST_DATABASE_URL, reservation.id), lp.Reservation)
    await lp.link_attempt(
        TEST_DATABASE_URL, reservation.id, await _attempt(reservation, trade_id=trade_id)
    )
    await lp.mark_placed(TEST_DATABASE_URL, reservation.id)
    if trade_id is not None:
        await lp.link_trade(TEST_DATABASE_URL, reservation.id, trade_id)
    return reservation


async def _close_and_settle(trade_id: int, pnl: str) -> None:
    await _query(
        "UPDATE app.trades SET status = 'closed', exit_at = now(), pnl_usd = %s, "
        "accounting_status = 'complete' WHERE id = %s",
        (pnl, trade_id),
    )


# --- The record ---------------------------------------------------------------------


async def test_an_issued_record_is_written_with_its_event() -> None:
    probe_id = await _issue()

    rows = await _query(
        "SELECT event, actor FROM app.live_probe_authorization_events WHERE probe_id = %s",
        (probe_id,),
    )
    assert rows == [{"event": "issued", "actor": "owner"}]


async def test_a_record_whose_entries_already_ended_is_not_issued() -> None:
    await _connect_or_skip()
    past = datetime.now(UTC) - timedelta(minutes=1)
    with pytest.raises(ValueError, match="database clock"):
        await lp.issue(TEST_DATABASE_URL, _terms(_probe_id(), entries_until=past), actor="owner")


def test_terms_above_usd_50_or_without_l_are_refused_before_the_database() -> None:
    with pytest.raises(ValueError, match="notional"):
        _terms("t-x", notional_usd=D("50.01")).validate()
    with pytest.raises(ValueError, match="L must be positive"):
        _terms("t-x", loss_threshold_usd=D(0)).validate()
    with pytest.raises(ValueError, match="flat_by"):
        now = datetime.now(UTC)
        _terms("t-x", entries_until=now + timedelta(days=2), flat_by=now).validate()


async def test_the_database_refuses_a_notional_above_usd_50() -> None:
    probe_id = await _issue()
    with pytest.raises(psycopg.errors.CheckViolation):
        await _query(
            "INSERT INTO app.live_probe_authorizations (probe_id, environment, account_key, "
            "exchange, strategy, contract_version, contract_sha256, instruments, max_entries, "
            "notional_usd, loss_threshold_usd, entries_until, flat_by, issued_by) "
            "SELECT probe_id || '-b', environment, account_key, exchange, strategy, "
            "contract_version, contract_sha256, instruments, max_entries, 51, "
            "loss_threshold_usd, entries_until, flat_by, issued_by "
            "FROM app.live_probe_authorizations WHERE probe_id = %s",
            (probe_id,),
        )


async def test_the_terms_never_change_and_a_record_is_never_deleted() -> None:
    probe_id = await _issue()

    for sql in (
        "UPDATE app.live_probe_authorizations SET loss_threshold_usd = 50 WHERE probe_id = %s",
        "UPDATE app.live_probe_authorizations SET entries_until = entries_until + interval '1 day'"
        " WHERE probe_id = %s",
        "UPDATE app.live_probe_authorizations SET instruments = '{SOLUSDT}' WHERE probe_id = %s",
        "DELETE FROM app.live_probe_authorizations WHERE probe_id = %s",
        "DELETE FROM app.live_probe_authorization_events WHERE probe_id = %s",
        "UPDATE app.live_probe_authorization_events SET actor = 'x' WHERE probe_id = %s",
    ):
        with pytest.raises(psycopg.errors.RaiseException):
            await _query(sql, (probe_id,))


async def test_a_revoke_is_never_undone() -> None:
    probe_id = await _issue()

    assert await lp.revoke(TEST_DATABASE_URL, probe_id, actor="owner", reason="done")
    assert not await lp.revoke(TEST_DATABASE_URL, probe_id, actor="owner", reason="again")
    with pytest.raises(psycopg.errors.RaiseException):
        await _query(
            "UPDATE app.live_probe_authorizations SET revoked_at = NULL, revoked_by = NULL "
            "WHERE probe_id = %s",
            (probe_id,),
        )
    events = await _query(
        "SELECT event FROM app.live_probe_authorization_events WHERE probe_id = %s ORDER BY id",
        (probe_id,),
    )
    assert [e["event"] for e in events] == ["issued", "revoked"]


# --- Admission ----------------------------------------------------------------------


async def test_a_reservation_holds_the_slot_n_and_its_reserve() -> None:
    probe_id = await _issue()

    reservation = await _reserve(probe_id)

    assert reservation.state == lp.STATE_RESERVED
    assert reservation.reserve_usd == RESERVE
    assert reservation.notional_usd == D(10)
    assert len(reservation.client_order_id) == 32
    held = await _ledger(probe_id)
    assert (held.slots_held, held.entries_spent, held.reserved_open) == (1, 1, RESERVE)


async def test_a_repeated_delivery_returns_the_reservation_and_holds_nothing_more() -> None:
    probe_id = await _issue()
    decision = uuid.uuid4().hex

    first = await _reserve(probe_id, decision_id=decision)
    again = await _reserve(probe_id, decision_id=decision)

    assert again.id == first.id and again.client_order_id == first.client_order_id
    assert first.created and not again.created
    held = await _ledger(probe_id)
    assert (held.slots_held, held.entries_spent) == (1, 1)
    assert await _refusals(probe_id) == []


async def test_a_repeated_delivery_after_the_send_returns_the_sending_reservation() -> None:
    probe_id = await _issue()
    decision = uuid.uuid4().hex
    first = await _reserve(probe_id, decision_id=decision)
    await lp.begin_sending(TEST_DATABASE_URL, first.id)
    await lp.revoke(TEST_DATABASE_URL, probe_id, actor="owner", reason="stop")

    again = await _reserve(probe_id, decision_id=decision)

    assert again.id == first.id and again.state == lp.STATE_SENDING and not again.created


async def test_a_missing_record_refuses_and_is_recorded() -> None:
    await _connect_or_skip()
    probe_id = _probe_id()

    result = await lp.reserve(TEST_DATABASE_URL, _request(probe_id))

    assert isinstance(result, lp.Refused) and result.reason == lp.REFUSED_NO_AUTHORIZATION
    assert await _refusals(probe_id) == [lp.REFUSED_NO_AUTHORIZATION]


@pytest.mark.parametrize(
    "overrides",
    [{"account_key": "someone-else"}, {"environment": "mainnet"}],
)
async def test_a_record_of_another_account_or_environment_refuses(overrides: Any) -> None:
    probe_id = await _issue()

    result = await lp.reserve(TEST_DATABASE_URL, _request(probe_id, **overrides))

    assert isinstance(result, lp.Refused) and result.reason == lp.REFUSED_FOREIGN_ACCOUNT


async def test_a_revoked_record_refuses() -> None:
    probe_id = await _issue()
    await lp.revoke(TEST_DATABASE_URL, probe_id, actor="owner", reason="stop")

    result = await lp.reserve(TEST_DATABASE_URL, _request(probe_id))

    assert isinstance(result, lp.Refused) and result.reason == lp.REFUSED_REVOKED


async def test_an_expired_record_refuses() -> None:
    now = datetime.now(UTC)
    probe_id = await _issue(
        entries_until=now + timedelta(seconds=1), flat_by=now + timedelta(hours=1)
    )
    await asyncio.sleep(1.2)

    result = await lp.reserve(TEST_DATABASE_URL, _request(probe_id))

    assert isinstance(result, lp.Refused) and result.reason == lp.REFUSED_ENTRIES_CLOSED


async def test_an_instrument_outside_the_record_refuses() -> None:
    probe_id = await _issue()

    result = await lp.reserve(TEST_DATABASE_URL, _request(probe_id, native_market_id="SOLUSDT"))

    assert isinstance(result, lp.Refused) and result.reason == lp.REFUSED_INSTRUMENT


async def test_one_slot() -> None:
    probe_id = await _issue()
    await _reserve(probe_id)

    result = await lp.reserve(TEST_DATABASE_URL, _request(probe_id))

    assert isinstance(result, lp.Refused) and result.reason == lp.REFUSED_SLOT


async def test_concurrent_decisions_take_one_slot() -> None:
    probe_id = await _issue()

    results = await asyncio.gather(
        *(lp.reserve(TEST_DATABASE_URL, _request(probe_id)) for _ in range(5))
    )

    assert sum(isinstance(r, lp.Reservation) for r in results) == 1
    assert sorted(r.reason for r in results if isinstance(r, lp.Refused)) == [lp.REFUSED_SLOT] * 4


async def test_n_is_spent_by_fills_and_the_slot_freed_by_the_close() -> None:
    probe_id = await _issue(max_entries=1, loss_threshold_usd=D(50))
    trade_id = await _trade(status="open", pnl=None)
    await _placed(probe_id, trade_id=trade_id)
    await _close_and_settle(trade_id, "0.10")

    held = await _ledger(probe_id)
    result = await lp.reserve(TEST_DATABASE_URL, _request(probe_id))

    assert (held.slots_held, held.entries_spent, held.reserved_open) == (0, 1, 0)
    assert held.realized_closed == D("0.10")
    assert isinstance(result, lp.Refused) and result.reason == lp.REFUSED_MAX_ENTRIES


async def test_a_placed_reservation_without_its_trade_holds_the_slot_and_reserve() -> None:
    probe_id = await _issue()
    await _placed(probe_id, trade_id=None)

    held = await _ledger(probe_id)

    assert (held.slots_held, held.entries_spent, held.reserved_open) == (1, 1, RESERVE)


async def test_a_reserve_that_does_not_fit_under_l_is_refused_before_it_is_written() -> None:
    probe_id = await _issue(loss_threshold_usd=D("0.5"))

    result = await lp.reserve(TEST_DATABASE_URL, _request(probe_id))

    assert isinstance(result, lp.Refused) and result.reason == lp.REFUSED_L_HEADROOM
    assert D(result.detail["reserve"]) == RESERVE
    assert (
        await _query("SELECT id FROM app.live_probe_reservations WHERE probe_id = %s", (probe_id,))
        == []
    )


async def test_a_realized_loss_at_l_writes_l_reached_and_refuses() -> None:
    probe_id = await _issue(loss_threshold_usd=D(1))
    trade_id = await _trade(status="open", pnl=None)
    await _placed(probe_id, trade_id=trade_id)
    await _close_and_settle(trade_id, "-1.0000")

    result = await lp.reserve(TEST_DATABASE_URL, _request(probe_id))
    again = await lp.reserve(TEST_DATABASE_URL, _request(probe_id))

    assert isinstance(result, lp.Refused) and result.reason == lp.REFUSED_L_REACHED
    assert isinstance(again, lp.Refused) and again.reason == lp.REFUSED_L_REACHED
    record = (
        await _query(
            "SELECT l_reached_at, l_reached_loss_usd, l_reached_inputs "
            "FROM app.live_probe_authorizations WHERE probe_id = %s",
            (probe_id,),
        )
    )[0]
    assert record["l_reached_at"] is not None
    assert record["l_reached_loss_usd"] == D(1)
    assert record["l_reached_inputs"]["source"] == "admission"
    events = await _query(
        "SELECT event FROM app.live_probe_authorization_events WHERE probe_id = %s ORDER BY id",
        (probe_id,),
    )
    assert [e["event"] for e in events].count("l_reached") == 1
    with pytest.raises(psycopg.errors.RaiseException):
        await _query(
            "UPDATE app.live_probe_authorizations SET l_reached_at = NULL, "
            "l_reached_loss_usd = NULL, l_reached_inputs = NULL WHERE probe_id = %s",
            (probe_id,),
        )


async def test_a_closed_probe_trade_with_pending_costs_blocks_entries() -> None:
    probe_id = await _issue(loss_threshold_usd=D(50))
    trade_id = await _trade(status="open", pnl=None)
    await _placed(probe_id, trade_id=trade_id)
    await _query(
        "UPDATE app.trades SET status = 'closed', exit_at = now(), pnl_usd = 0.2 WHERE id = %s",
        (trade_id,),
    )

    held = await _ledger(probe_id)
    result = await lp.reserve(TEST_DATABASE_URL, _request(probe_id))

    assert held.closed_unsettled == 1 and held.reserved_open == RESERVE
    assert held.realized_closed == 0, "a pending result is not yet realized"
    assert isinstance(result, lp.Refused) and result.reason == lp.REFUSED_COSTS_PENDING


async def test_a_submission_unknown_reservation_keeps_its_reserve() -> None:
    probe_id = await _issue()
    reservation = await _reserve(probe_id)
    await lp.begin_sending(TEST_DATABASE_URL, reservation.id)

    held = await _ledger(probe_id)
    assert (held.slots_held, held.reserved_open) == (1, RESERVE)

    await lp.mark_released(TEST_DATABASE_URL, reservation.id, reason="never placed")
    released = await _ledger(probe_id)
    assert (released.slots_held, released.entries_spent, released.reserved_open) == (0, 0, 0)


# --- reserved -> sending ------------------------------------------------------------


async def test_revoke_before_the_transition_sends_nothing() -> None:
    probe_id = await _issue()
    reservation = await _reserve(probe_id)
    await lp.revoke(TEST_DATABASE_URL, probe_id, actor="owner", reason="stop")

    result = await lp.begin_sending(TEST_DATABASE_URL, reservation.id)

    assert isinstance(result, lp.Refused) and result.reason == lp.REFUSED_REVOKED
    row = (
        await _query(
            "SELECT state, state_reason FROM app.live_probe_reservations WHERE id = %s",
            (reservation.id,),
        )
    )[0]
    assert row == {"state": lp.STATE_REFUSED_BEFORE_SEND, "state_reason": lp.REFUSED_REVOKED}
    held = await _ledger(probe_id)
    assert (held.slots_held, held.entries_spent, held.reserved_open) == (0, 0, 0)
    refusal = await _query(
        "SELECT reason, reservation_id FROM app.live_probe_refusals WHERE probe_id = %s",
        (probe_id,),
    )
    assert refusal == [{"reason": lp.REFUSED_REVOKED, "reservation_id": reservation.id}]
    again = await lp.begin_sending(TEST_DATABASE_URL, reservation.id)
    assert isinstance(again, lp.Refused)


async def test_revoke_after_the_transition_keeps_the_exposure_held() -> None:
    probe_id = await _issue()
    reservation = await _reserve(probe_id)

    sending = await lp.begin_sending(TEST_DATABASE_URL, reservation.id)
    await lp.revoke(TEST_DATABASE_URL, probe_id, actor="owner", reason="stop")

    assert isinstance(sending, lp.Reservation) and sending.state == lp.STATE_SENDING
    held = await _ledger(probe_id)
    assert (held.slots_held, held.entries_spent, held.reserved_open) == (1, 1, RESERVE)
    again = await lp.begin_sending(TEST_DATABASE_URL, reservation.id)
    assert isinstance(again, lp.Reservation) and again.state == lp.STATE_SENDING
    assert not again.created


async def test_a_revoke_holding_the_lock_orders_the_transition_after_it() -> None:
    probe_id = await _issue()
    reservation = await _reserve(probe_id)
    revoker = await psycopg.AsyncConnection.connect(TEST_DATABASE_URL)
    try:
        async with revoker.transaction():
            await revoker.execute(
                "SELECT 1 FROM app.live_probe_authorizations WHERE probe_id = %s FOR UPDATE",
                (probe_id,),
            )
            await revoker.execute(
                "UPDATE app.live_probe_authorizations SET revoked_at = clock_timestamp(), "
                "revoked_by = 'owner', revoke_reason = 'race' WHERE probe_id = %s",
                (probe_id,),
            )
            transition = asyncio.create_task(lp.begin_sending(TEST_DATABASE_URL, reservation.id))
            await asyncio.sleep(0.3)
            assert not transition.done(), "the transition waits for the revoke's row lock"
        result = await transition
    finally:
        await revoker.close()

    assert isinstance(result, lp.Refused) and result.reason == lp.REFUSED_REVOKED


async def test_entries_until_passing_during_the_preflight_sends_nothing() -> None:
    now = datetime.now(UTC)
    probe_id = await _issue(
        entries_until=now + timedelta(seconds=1), flat_by=now + timedelta(hours=1)
    )
    reservation = await _reserve(probe_id)
    await asyncio.sleep(1.2)

    result = await lp.begin_sending(TEST_DATABASE_URL, reservation.id)

    assert isinstance(result, lp.Refused) and result.reason == lp.REFUSED_ENTRIES_CLOSED


async def test_a_failed_preflight_releases_the_reservation() -> None:
    probe_id = await _issue()
    reservation = await _reserve(probe_id)

    refused = await lp.refuse_before_send(
        TEST_DATABASE_URL, reservation.id, reason="preflight:account_not_empty"
    )

    assert refused.reason == "preflight:account_not_empty"
    held = await _ledger(probe_id)
    assert (held.slots_held, held.entries_spent, held.reserved_open) == (0, 0, 0)
    assert isinstance(await lp.reserve(TEST_DATABASE_URL, _request(probe_id)), lp.Reservation)


async def test_a_reservation_moves_only_forward_and_its_chain_never_changes() -> None:
    probe_id = await _issue()
    reservation = await _reserve(probe_id)

    for sql in (
        "UPDATE app.live_probe_reservations SET state = 'placed', sending_at = now(), "
        "finished_at = now(), attempt_id = NULL WHERE id = %s",
        "UPDATE app.live_probe_reservations SET client_order_id = md5('x') WHERE id = %s",
        "UPDATE app.live_probe_reservations SET reserve_usd = 0 WHERE id = %s",
        "DELETE FROM app.live_probe_reservations WHERE id = %s",
    ):
        with pytest.raises((psycopg.errors.RaiseException, psycopg.errors.CheckViolation)):
            await _query(sql, (reservation.id,))
    await lp.refuse_before_send(TEST_DATABASE_URL, reservation.id, reason="preflight")
    with pytest.raises(psycopg.errors.RaiseException):
        await _query(
            "UPDATE app.live_probe_reservations SET state = 'sending' WHERE id = %s",
            (reservation.id,),
        )


# --- Suspension (section 5) ---------------------------------------------------------


async def test_a_start_suspends_active_records_until_the_owner_resumes() -> None:
    probe_id = await _issue()
    revoked = await _issue()
    await lp.revoke(TEST_DATABASE_URL, revoked, actor="owner", reason="stop")

    suspended = await lp.suspend_active(TEST_DATABASE_URL, reason="service start")
    refused = await lp.reserve(TEST_DATABASE_URL, _request(probe_id))
    assert await lp.suspend_active(TEST_DATABASE_URL, reason="again") == [], "suspended once"
    assert await lp.resume(TEST_DATABASE_URL, probe_id, actor="owner")
    admitted = await lp.reserve(TEST_DATABASE_URL, _request(probe_id))

    assert probe_id in suspended and revoked not in suspended
    assert isinstance(refused, lp.Refused) and refused.reason == lp.REFUSED_SUSPENDED
    assert isinstance(admitted, lp.Reservation)
    events = await _query(
        "SELECT event, actor FROM app.live_probe_authorization_events "
        "WHERE probe_id = %s ORDER BY id",
        (probe_id,),
    )
    assert [(e["event"], e["actor"]) for e in events] == [
        ("issued", "owner"),
        ("suspended", lp.SERVICE_ACTOR),
        ("resumed", "owner"),
    ]


async def test_a_costs_incident_blocks_entries_until_the_owner_clears_it() -> None:
    probe_id = await _issue()
    await _query(
        "UPDATE app.live_probe_authorizations SET costs_incident_at = now(), "
        "costs_incident_reason = 'collector incomplete' WHERE probe_id = %s",
        (probe_id,),
    )

    refused = await lp.reserve(TEST_DATABASE_URL, _request(probe_id))
    assert await lp.clear_costs_incident(TEST_DATABASE_URL, probe_id, actor="owner", note="ok")
    admitted = await lp.reserve(TEST_DATABASE_URL, _request(probe_id))

    assert isinstance(refused, lp.Refused) and refused.reason == lp.REFUSED_COSTS_INCIDENT
    assert isinstance(admitted, lp.Reservation)


# --- CLI ----------------------------------------------------------------------------


async def test_the_cli_issues_shows_and_exports(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    await _connect_or_skip()
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    probe_id = _probe_id()
    now = datetime.now(UTC)

    def run(*argv: str) -> dict[str, Any]:
        # Logs are captured, not printed: how they render depends on what configured
        # structlog before, and stdout must hold only the result.
        with structlog.testing.capture_logs():
            lp.main(list(argv))
        return json.loads(capsys.readouterr().out)

    await asyncio.to_thread(
        run,
        "issue",
        f"--probe-id={probe_id}",
        "--environment=testnet",
        f"--account-key={ACCOUNT}",
        "--strategy=momentum_flow_hold12h",
        "--contract-version=hold12h_v1",
        f"--contract-sha256={'b' * 64}",
        f"--instrument={INSTRUMENT}",
        "--max-entries=2",
        "--notional-usd=5",
        "--loss-threshold-usd=3",
        f"--entries-until={(now + timedelta(days=1)).isoformat()}",
        f"--flat-by={(now + timedelta(days=2)).isoformat()}",
        "--by=owner",
    )
    shown = await asyncio.to_thread(run, "show", f"--probe-id={probe_id}")
    out = tmp_path / "probe.json"
    exported = await asyncio.to_thread(run, "export", f"--probe-id={probe_id}", f"--out={out}")

    assert shown["authorization"]["loss_threshold_usd"] == "3.00000000"
    assert shown["ledger"]["entries_spent"] == 0
    assert exported["sha256"] in (tmp_path / "probe.json.sha256").read_text()
    assert json.loads(out.read_text())["events"][0]["event"] == "issued"


async def test_the_steps_after_the_send_repeat_safely_and_refuse_out_of_order() -> None:
    probe_id = await _issue()
    reservation = await _reserve(probe_id)

    with pytest.raises(ValueError, match="step refused"):
        await lp.mark_placed(TEST_DATABASE_URL, reservation.id)
    await lp.begin_sending(TEST_DATABASE_URL, reservation.id)
    with pytest.raises(ValueError, match="step refused"):
        await lp.mark_placed(TEST_DATABASE_URL, reservation.id)
    attempt_id = await _attempt(reservation)
    await lp.link_attempt(TEST_DATABASE_URL, reservation.id, attempt_id)
    await lp.link_attempt(TEST_DATABASE_URL, reservation.id, attempt_id)
    with pytest.raises(ValueError, match="step refused"):
        await lp.link_attempt(
            TEST_DATABASE_URL,
            reservation.id,
            await _attempt(reservation, client_order_id=uuid.uuid4().hex),
        )
    await lp.mark_placed(TEST_DATABASE_URL, reservation.id)
    await lp.mark_placed(TEST_DATABASE_URL, reservation.id)
    with pytest.raises(ValueError, match="step refused"):
        await lp.mark_released(TEST_DATABASE_URL, reservation.id, reason="late")


async def test_a_decision_reused_for_another_instrument_raises() -> None:
    probe_id = await _issue()
    decision = uuid.uuid4().hex
    await _reserve(probe_id, decision_id=decision)

    with pytest.raises(ValueError, match="another instrument or side"):
        await lp.reserve(
            TEST_DATABASE_URL, _request(probe_id, decision_id=decision, native_market_id="ETHUSDT")
        )


# --- Review round 1 -----------------------------------------------------------------


async def _hold_record_lock_past(probe_id: str, deadline: datetime, call: Any) -> Any:
    """Hold the record's row lock until after ``deadline`` while ``call`` waits for it."""
    holder = await psycopg.AsyncConnection.connect(TEST_DATABASE_URL)
    try:
        async with holder.transaction():
            await holder.execute(
                "SELECT 1 FROM app.live_probe_authorizations WHERE probe_id = %s FOR UPDATE",
                (probe_id,),
            )
            waiting = asyncio.create_task(call)
            await asyncio.sleep((deadline - datetime.now(UTC)).total_seconds() + 0.3)
            assert not waiting.done(), "the call waits for the row lock"
        return await waiting
    finally:
        await holder.close()


async def test_a_deadline_passing_while_the_transition_waits_for_the_lock_refuses() -> None:
    now = datetime.now(UTC)
    deadline = now + timedelta(seconds=1)
    probe_id = await _issue(entries_until=deadline, flat_by=now + timedelta(hours=1))
    reservation = await _reserve(probe_id)

    result = await _hold_record_lock_past(
        probe_id, deadline, lp.begin_sending(TEST_DATABASE_URL, reservation.id)
    )

    assert isinstance(result, lp.Refused) and result.reason == lp.REFUSED_ENTRIES_CLOSED
    held = await _ledger(probe_id)
    assert (held.slots_held, held.entries_spent) == (0, 0)


async def test_a_deadline_passing_while_the_reservation_waits_for_the_lock_refuses() -> None:
    now = datetime.now(UTC)
    deadline = now + timedelta(seconds=1)
    probe_id = await _issue(entries_until=deadline, flat_by=now + timedelta(hours=1))

    result = await _hold_record_lock_past(
        probe_id, deadline, lp.reserve(TEST_DATABASE_URL, _request(probe_id))
    )

    assert isinstance(result, lp.Refused) and result.reason == lp.REFUSED_ENTRIES_CLOSED
    assert (
        await _query("SELECT id FROM app.live_probe_reservations WHERE probe_id = %s", (probe_id,))
        == []
    )


async def test_the_database_refuses_a_reservation_or_a_send_the_record_does_not_allow() -> None:
    probe_id = await _issue()
    reservation = await _reserve(probe_id)
    await lp.revoke(TEST_DATABASE_URL, probe_id, actor="owner", reason="stop")

    with pytest.raises(psycopg.errors.RaiseException, match="admits no entry"):
        await _query(
            "UPDATE app.live_probe_reservations SET state = 'sending', sending_at = now() "
            "WHERE id = %s",
            (reservation.id,),
        )
    with pytest.raises(psycopg.errors.RaiseException, match="admits no entry"):
        await _query(
            "INSERT INTO app.live_probe_reservations (probe_id, idempotency_key, decision_id, "
            "client_order_id, native_market_id, side, notional_usd, reserve_usd, "
            "reserve_inputs, state) VALUES (%s, 'k', 'd', md5(random()::text), 'BTCUSDT', "
            "'long', 10, 1, '{}'::jsonb, 'reserved')",
            (probe_id,),
        )


@pytest.mark.parametrize("overrides", [{"client_order_id": "f" * 32}, {"exchange": "binance"}])
async def test_an_attempt_of_another_order_is_never_linked(overrides: Any) -> None:
    probe_id = await _issue()
    reservation = await _reserve(probe_id)
    await lp.begin_sending(TEST_DATABASE_URL, reservation.id)
    overrides = {**overrides}
    if "client_order_id" in overrides:
        overrides["client_order_id"] = uuid.uuid4().hex
    attempt_id = await _attempt(reservation, **overrides)

    with pytest.raises(ValueError, match="step refused"):
        await lp.link_attempt(TEST_DATABASE_URL, reservation.id, attempt_id)
    with pytest.raises(psycopg.errors.RaiseException, match="not the entry attempt"):
        await _query(
            "UPDATE app.live_probe_reservations SET attempt_id = %s WHERE id = %s",
            (attempt_id, reservation.id),
        )


async def test_a_trade_not_journalled_from_the_attempt_is_never_linked() -> None:
    probe_id = await _issue(loss_threshold_usd=D(50))
    own = await _trade(status="open", pnl=None)
    other = await _trade(status="open", pnl=None)
    reservation = await _placed(probe_id, trade_id=None)
    await _query(
        "UPDATE app.live_order_attempts SET trade_id = %s WHERE client_order_id = %s",
        (own, reservation.client_order_id),
    )

    with pytest.raises(ValueError, match="step refused"):
        await lp.link_trade(TEST_DATABASE_URL, reservation.id, other)
    with pytest.raises(psycopg.errors.RaiseException, match="not journalled from"):
        await _query(
            "UPDATE app.live_probe_reservations SET trade_id = %s WHERE id = %s",
            (other, reservation.id),
        )
    await lp.link_trade(TEST_DATABASE_URL, reservation.id, own)


async def test_one_trade_never_counts_for_two_reservations() -> None:
    """A $1 profit linked twice would read as realized_closed $2 and widen L."""
    probe_id = await _issue(max_entries=2, loss_threshold_usd=D(50))
    trade_id = await _trade(status="open", pnl=None)
    first = await _placed(probe_id, trade_id=trade_id)
    await _close_and_settle(trade_id, "1.0000")
    second = await _placed(probe_id, trade_id=None)

    await _query(
        "UPDATE app.live_order_attempts SET trade_id = %s WHERE client_order_id = %s",
        (trade_id, second.client_order_id),
    )

    with pytest.raises(psycopg.errors.UniqueViolation):
        await _query(
            "UPDATE app.live_probe_reservations SET trade_id = %s WHERE id = %s",
            (trade_id, second.id),
        )
    assert first.id != second.id
    assert (await _ledger(probe_id)).realized_closed == D(1)


async def _bypass_during_revoke(probe_id: str, sql: str, params: tuple[Any, ...]) -> Any:
    """Run ``sql`` outside the module while a revoke holds the record's row lock and
    has written the revoke, then commit the revoke."""
    revoker = await psycopg.AsyncConnection.connect(TEST_DATABASE_URL)
    try:
        async with revoker.transaction():
            await revoker.execute(
                "SELECT 1 FROM app.live_probe_authorizations WHERE probe_id = %s FOR UPDATE",
                (probe_id,),
            )
            await revoker.execute(
                "UPDATE app.live_probe_authorizations SET revoked_at = clock_timestamp(), "
                "revoked_by = 'owner', revoke_reason = 'race' WHERE probe_id = %s",
                (probe_id,),
            )
            bypass = asyncio.create_task(_query(sql, params))
            await asyncio.sleep(0.3)
            assert not bypass.done(), "the guard waits for the revoke in flight"
    finally:
        await revoker.close()
    return bypass


async def test_a_reservation_written_outside_the_module_waits_for_a_revoke() -> None:
    probe_id = await _issue()

    bypass = await _bypass_during_revoke(
        probe_id,
        "INSERT INTO app.live_probe_reservations (probe_id, idempotency_key, decision_id, "
        "client_order_id, native_market_id, side, notional_usd, reserve_usd, "
        "reserve_inputs, state) VALUES (%s, 'k', 'd', md5(random()::text), 'BTCUSDT', "
        "'long', 10, 1, '{}'::jsonb, 'reserved')",
        (probe_id,),
    )

    with pytest.raises(psycopg.errors.RaiseException, match="admits no entry"):
        await bypass


async def test_a_send_started_outside_the_module_waits_for_a_revoke() -> None:
    probe_id = await _issue()
    reservation = await _reserve(probe_id)

    bypass = await _bypass_during_revoke(
        probe_id,
        "UPDATE app.live_probe_reservations SET state = 'sending', sending_at = now() "
        "WHERE id = %s",
        (reservation.id,),
    )

    with pytest.raises(psycopg.errors.RaiseException, match="admits no entry"):
        await bypass
