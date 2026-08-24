from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_execution import incidents
from schurfer_execution.incident_worker import MAX_RESOLUTION_ATTEMPTS, _process_one, _tick
from schurfer_execution.incidents import Incident
from schurfer_execution.symbols import ExecutionInstrument


@pytest.fixture(autouse=True)
def mock_resolve_execution_instrument(monkeypatch):
    def dummy_resolve(ex, base, *args, **kwargs):
        return ExecutionInstrument(
            exchange=ex.id if hasattr(ex, "id") else "bybit",
            symbol=f"{base.upper()}/USDT:USDT",
            native_market_id=f"{base.upper()}USDT",
            base=base.upper(),
            quote="USDT",
            settle="USDT",
            market_type="swap",
        )

    monkeypatch.setattr("schurfer_execution.symbols.resolve_execution_instrument", dummy_resolve)


def _cfg(db_url: str | None = "postgresql://x") -> MagicMock:
    cfg = MagicMock()
    cfg.db_url = db_url
    cfg.telegram_bot_token = "tok"  # noqa: S105
    cfg.telegram_chat_id = "chat"
    return cfg


def _open_incident(**overrides: Any) -> Incident:
    base = {
        "id": 7,
        "exchange": "bybit",
        "base": "BEAT",
        "operation": "open",
        "order_id": "ord-1",
        "trade_id": None,
        "status": "pending",
        "attempt_count": 0,
        "context": {
            "side": "short",
            "size_usd": 50.0,
            "leverage": 3,
            "setup_context": {"pump_pct": 40.0},
        },
    }
    base.update(overrides)
    return Incident(**base)


def _close_incident(**overrides: Any) -> Incident:
    base = {
        "id": 9,
        "exchange": "bybit",
        "base": "BEAT",
        "operation": "close",
        "order_id": "ord-2",
        "trade_id": 42,
        "status": "pending",
        "attempt_count": 0,
        "context": {"reason": "trailing_stop"},
    }
    base.update(overrides)
    return Incident(**base)


def _exchange_confirming(price: float = 1.5) -> MagicMock:
    ex = MagicMock()
    ex.fetch_order = AsyncMock(return_value={"id": "ord-1", "average": price})
    return ex


def _exchange_unresolved() -> MagicMock:
    ex = MagicMock()
    ex.has = {"fetchOrderTrades": False, "fetchMyTrades": False}
    ex.fetch_order = AsyncMock(return_value={"id": "ord-1"})
    return ex


async def test_process_one_completes_a_resolved_open() -> None:
    incident = _open_incident()
    rdb = MagicMock()
    rdb.set = AsyncMock()

    with (
        patch(
            "schurfer_execution.incident_worker.incidents.mark_resolved",
            AsyncMock(return_value=True),
        ) as mock_resolved,
        patch(
            "schurfer_execution.incident_worker.incidents.claim_recovery_notification",
            AsyncMock(return_value=True),
        ),
        patch("schurfer_execution.incident_worker.notify.notify_alert", AsyncMock()) as mock_alert,
        patch(
            "schurfer_execution.incident_worker.journal.open_trade",
            AsyncMock(return_value=101),
        ) as mock_open_trade,
    ):
        await _process_one(incident, {"bybit": _exchange_confirming(1.5)}, rdb, _cfg())

    mock_resolved.assert_called_once_with(
        "postgresql://x", 7, price=1.5, source="refetch.order.average"
    )
    mock_open_trade.assert_called_once()
    assert mock_open_trade.call_args.kwargs["entry_price"] == 1.5
    assert mock_open_trade.call_args.kwargs["size_usd"] == 50.0
    assert mock_open_trade.call_args.kwargs["leverage"] == 3
    rdb.set.assert_any_call("trade:id:bybit:BEAT", "101", ex=86400)
    rdb.set.assert_any_call("position:entry:bybit:BEAT", "1.5", ex=86400)
    rdb.set.assert_any_call("position:side:bybit:BEAT", "short", ex=86400)
    mock_alert.assert_awaited_once()


async def test_process_one_open_not_marked_resolved_when_journal_write_fails() -> None:
    """Regression (colleague review, P0): an earlier draft called
    mark_resolved BEFORE attempting the journal write, so a DB hiccup
    inside open_trade right after the price was confirmed left the
    incident permanently terminal (load_open_incidents only loads pending/
    resolving) with the write never having happened and nothing left to
    retry it. mark_resolved must only fire once the write actually
    succeeded; otherwise this incident must stay retryable."""
    incident = _open_incident()
    rdb = MagicMock()
    rdb.set = AsyncMock()

    with (
        patch(
            "schurfer_execution.incident_worker.incidents.mark_resolved",
            AsyncMock(return_value=True),
        ) as mock_resolved,
        patch(
            "schurfer_execution.incident_worker.incidents.mark_attempt", AsyncMock()
        ) as mock_attempt,
        patch("schurfer_execution.incident_worker.notify.notify_alert", AsyncMock()),
        patch(
            "schurfer_execution.incident_worker.journal.open_trade",
            AsyncMock(return_value=None),
        ),
    ):
        await _process_one(incident, {"bybit": _exchange_confirming(1.5)}, rdb, _cfg())

    mock_resolved.assert_not_called()
    mock_attempt.assert_awaited_once()
    assert mock_attempt.call_args.kwargs["status"] == incidents.STATUS_RESOLVING


async def test_process_one_completes_a_resolved_close() -> None:
    incident = _close_incident()
    rdb = MagicMock()

    with (
        patch(
            "schurfer_execution.incident_worker.incidents.has_pending_open",
            AsyncMock(return_value=False),
        ),
        patch(
            "schurfer_execution.incident_worker.incidents.mark_resolved",
            AsyncMock(return_value=True),
        ),
        patch(
            "schurfer_execution.incident_worker.incidents.claim_recovery_notification",
            AsyncMock(return_value=True),
        ),
        patch("schurfer_execution.incident_worker.notify.notify_alert", AsyncMock()),
        patch(
            "schurfer_execution.incident_worker.journal.try_commit_close",
            AsyncMock(return_value=True),
        ) as mock_commit,
        patch(
            "schurfer_execution.incident_worker.journal.delete_trade_id_if_matches",
            AsyncMock(),
        ) as mock_cas_delete,
    ):
        await _process_one(incident, {"bybit": _exchange_confirming(2.0)}, rdb, _cfg())

    mock_commit.assert_called_once()
    assert mock_commit.call_args.kwargs["trade_id"] == 42
    assert mock_commit.call_args.kwargs["exit_price"] == 2.0
    assert mock_commit.call_args.kwargs["reason"] == "trailing_stop"
    mock_cas_delete.assert_called_once_with(rdb, "trade:id:bybit:BEAT", 42)


async def test_process_one_close_waits_when_matching_open_still_pending() -> None:
    """Regression: a close created before its matching open resolved must not
    be resolved yet — resolving it now, with trade_id still None, risks losing
    the journal link permanently once the incident is marked 'resolved'."""
    incident = _close_incident(trade_id=None)

    with (
        patch(
            "schurfer_execution.incident_worker.incidents.has_pending_open",
            AsyncMock(return_value=True),
        ) as mock_pending,
        patch(
            "schurfer_execution.incident_worker.incidents.mark_resolved", AsyncMock()
        ) as mock_resolved,
    ):
        await _process_one(incident, {"bybit": _exchange_confirming(2.0)}, MagicMock(), _cfg())

    mock_pending.assert_called_once_with("postgresql://x", exchange="bybit", base="BEAT")
    mock_resolved.assert_not_called()


async def test_process_one_close_with_missing_trade_id_falls_back_to_db_lookup() -> None:
    """The matching open has since completed elsewhere (e.g. it went to
    manual_required rather than resolving normally) — the trade now exists in
    the journal even though it wasn't known when this incident was created."""
    incident = _close_incident(trade_id=None)
    rdb = MagicMock()

    with (
        patch(
            "schurfer_execution.incident_worker.incidents.has_pending_open",
            AsyncMock(return_value=False),
        ),
        patch(
            "schurfer_execution.incident_worker.incidents.mark_resolved",
            AsyncMock(return_value=True),
        ),
        patch(
            "schurfer_execution.incident_worker.incidents.claim_recovery_notification",
            AsyncMock(return_value=False),
        ),
        patch(
            "schurfer_execution.incident_worker.journal.find_open_trade_id",
            AsyncMock(return_value=55),
        ) as mock_find,
        patch(
            "schurfer_execution.incident_worker.journal.try_commit_close",
            AsyncMock(return_value=True),
        ) as mock_commit,
        patch("schurfer_execution.incident_worker.journal.delete_trade_id_if_matches", AsyncMock()),
    ):
        await _process_one(incident, {"bybit": _exchange_confirming(2.0)}, rdb, _cfg())

    mock_find.assert_called_once_with(
        "postgresql://x",
        exchange="bybit",
        symbol="BEAT/USDT:USDT",
    )
    mock_commit.assert_called_once()
    assert mock_commit.call_args.kwargs["trade_id"] == 55


async def test_process_one_close_alerts_when_trade_truly_not_found() -> None:
    incident = _close_incident(trade_id=None)
    rdb = MagicMock()

    with (
        patch(
            "schurfer_execution.incident_worker.incidents.has_pending_open",
            AsyncMock(return_value=False),
        ),
        patch(
            "schurfer_execution.incident_worker.incidents.mark_resolved",
            AsyncMock(return_value=True),
        ) as mock_resolved,
        patch(
            "schurfer_execution.incident_worker.incidents.claim_recovery_notification",
            AsyncMock(return_value=False),
        ),
        patch(
            "schurfer_execution.incident_worker.journal.find_open_trade_id",
            AsyncMock(return_value=None),
        ),
        patch(
            "schurfer_execution.incident_worker.journal.try_commit_close", AsyncMock()
        ) as mock_commit,
        patch("schurfer_execution.incident_worker.notify.notify_alert", AsyncMock()) as mock_alert,
        patch(
            "schurfer_execution.incident_worker.incidents.mark_attempt", AsyncMock()
        ) as mock_attempt,
    ):
        await _process_one(incident, {"bybit": _exchange_confirming(2.0)}, rdb, _cfg())

    mock_commit.assert_not_called()
    mock_alert.assert_awaited_once()
    # _complete_close's own "trade truly not found" branch returns False --
    # mark_resolved must not fire for this incident, and _process_one must
    # instead bump the retry count so a later tick can try again once the
    # matching trade is findable (colleague review: an earlier draft marked
    # every incident resolved as soon as a price was confirmed, regardless
    # of whether the completion that followed actually succeeded).
    mock_attempt.assert_awaited_once()
    mock_resolved.assert_not_called()


async def test_process_one_still_unresolved_marks_resolving_without_alert() -> None:
    incident = _open_incident(attempt_count=1)

    with (
        patch(
            "schurfer_execution.incident_worker.incidents.mark_attempt", AsyncMock()
        ) as mock_attempt,
        patch("schurfer_execution.incident_worker.notify.notify_alert", AsyncMock()) as mock_alert,
    ):
        await _process_one(incident, {"bybit": _exchange_unresolved()}, MagicMock(), _cfg())

    mock_attempt.assert_called_once()
    assert mock_attempt.call_args.kwargs["status"] == "resolving"
    mock_alert.assert_not_awaited()


async def test_process_one_escalates_to_manual_required_after_bound() -> None:
    incident = _open_incident(attempt_count=MAX_RESOLUTION_ATTEMPTS - 1)

    with (
        patch(
            "schurfer_execution.incident_worker.incidents.mark_attempt", AsyncMock()
        ) as mock_attempt,
        patch("schurfer_execution.incident_worker.notify.notify_alert", AsyncMock()) as mock_alert,
    ):
        await _process_one(incident, {"bybit": _exchange_unresolved()}, MagicMock(), _cfg())

    assert mock_attempt.call_args.kwargs["status"] == "manual_required"
    mock_alert.assert_awaited_once()


async def test_process_one_bumps_attempt_when_exchange_not_configured() -> None:
    """Regression: an incident whose exchange was removed from config must
    still count towards MAX_RESOLUTION_ATTEMPTS and eventually escalate —
    otherwise it retries silently forever, never reaching manual_required."""
    incident = _open_incident()
    with patch(
        "schurfer_execution.incident_worker.incidents.mark_attempt", AsyncMock()
    ) as mock_attempt:
        await _process_one(incident, {}, MagicMock(), _cfg())
    mock_attempt.assert_called_once()
    assert mock_attempt.call_args.kwargs["status"] == "resolving"


async def test_process_one_escalates_when_exchange_never_configured() -> None:
    incident = _open_incident(attempt_count=MAX_RESOLUTION_ATTEMPTS - 1)
    with (
        patch(
            "schurfer_execution.incident_worker.incidents.mark_attempt", AsyncMock()
        ) as mock_attempt,
        patch("schurfer_execution.incident_worker.notify.notify_alert", AsyncMock()) as mock_alert,
    ):
        await _process_one(incident, {}, MagicMock(), _cfg())
    assert mock_attempt.call_args.kwargs["status"] == "manual_required"
    mock_alert.assert_awaited_once()


async def test_tick_skips_entirely_without_db_url() -> None:
    with patch(
        "schurfer_execution.incident_worker.incidents.load_open_incidents", AsyncMock()
    ) as mock_load:
        await _tick({}, MagicMock(), _cfg(db_url=None))
    mock_load.assert_not_called()


async def test_tick_continues_after_one_incident_errors() -> None:
    good = _open_incident(id=1)
    bad = _open_incident(id=2)
    processed: list[int] = []

    async def _fake_process(incident: Incident, *_args: object, **_kwargs: object) -> None:
        processed.append(incident.id)
        if incident.id == 2:
            raise RuntimeError("boom")

    with (
        patch(
            "schurfer_execution.incident_worker.incidents.load_open_incidents",
            AsyncMock(return_value=[bad, good]),
        ),
        patch("schurfer_execution.incident_worker._process_one", side_effect=_fake_process),
    ):
        await _tick({}, MagicMock(), _cfg())

    assert processed == [2, 1]
