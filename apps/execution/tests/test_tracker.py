import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_execution.journal import PnlSnapshot
from schurfer_execution.risk import decode_pnl_lease, pnl_lease_flag
from schurfer_execution.tracker import _tick

_GENERATION = 41


def _snapshot(
    realized: float = 0.0, *, blocker: str | None = None, generation: int = _GENERATION
) -> PnlSnapshot:
    return PnlSnapshot(realized=realized, generation=generation, blocker=blocker)


@pytest.fixture(autouse=True)
def _journal_unchanged_after_snapshot():  # type: ignore[no-untyped-def]
    with patch(
        "schurfer_execution.tracker.journal.pnl_generation",
        AsyncMock(return_value=_GENERATION),
    ):
        yield


def _mock_rdb() -> MagicMock:
    rdb = MagicMock()
    rdb.set = AsyncMock()
    rdb.delete = AsyncMock()
    return rdb


def _mock_exchange(pnl: float, *, fail: bool = False) -> MagicMock:
    ex = MagicMock()
    if fail:
        ex.fetch_positions = AsyncMock(side_effect=Exception("timeout"))
    else:
        ex.fetch_positions = AsyncMock(
            return_value=[{"contracts": 1.0, "unrealizedPnl": pnl, "symbol": "BEAT/USDT:USDT"}]
        )
    return ex


def _patch_snapshot(snapshot: PnlSnapshot | None):  # type: ignore[no-untyped-def]
    return patch(
        "schurfer_execution.tracker.journal.pnl_snapshot", AsyncMock(return_value=snapshot)
    )


def _patch_no_pending(return_value: bool = False):  # type: ignore[no-untyped-def]
    return patch(
        "schurfer_execution.tracker.journal.any_pending_closes",
        AsyncMock(return_value=return_value),
    )


def _published_lease(rdb: MagicMock) -> dict[str, object]:
    calls = [c for c in rdb.set.call_args_list if c.args[0] == "risk:pnl_ready"]
    assert len(calls) == 1
    assert calls[0].kwargs == {"ex": 120}
    payload: dict[str, object] = json.loads(calls[0].args[1])
    return payload


async def test_tick_publishes_daily_pnl_with_the_snapshot_generation() -> None:
    rdb = _mock_rdb()
    with _patch_snapshot(_snapshot(-20.0)), _patch_no_pending():
        await _tick({"bybit": _mock_exchange(-5.0)}, rdb, db_url="postgresql://x")

    rdb.set.assert_any_call("trading:daily_pnl", "-25.0")
    assert _published_lease(rdb) == {"generation": _GENERATION, "daily_pnl": -25.0}
    rdb.delete.assert_not_called()


async def test_tick_recomputes_from_source_of_truth_every_call() -> None:
    """Regression: daily_pnl must not depend on in-process state that resets
    on restart — every tick recomputes realized PnL from the journal (DB),
    so a process restart mid-day can't silently wipe out today's tracked loss."""
    rdb = _mock_rdb()
    with _patch_snapshot(_snapshot(-100.0)), _patch_no_pending():
        await _tick({"bybit": _mock_exchange(0.0)}, rdb, db_url="postgresql://x")
        await _tick({"bybit": _mock_exchange(0.0)}, rdb, db_url="postgresql://x")

    daily_pnl_calls = [c for c in rdb.set.call_args_list if c.args[0] == "trading:daily_pnl"]
    assert daily_pnl_calls[0].args == ("trading:daily_pnl", "-100.0")
    assert daily_pnl_calls[1].args == ("trading:daily_pnl", "-100.0")


async def test_tick_skips_the_journal_when_db_not_configured() -> None:
    rdb = _mock_rdb()
    with _patch_snapshot(_snapshot(-999.0)) as mock_snapshot:
        await _tick({"bybit": _mock_exchange(-12.5)}, rdb, db_url=None)

    mock_snapshot.assert_not_called()
    rdb.set.assert_any_call("trading:daily_pnl", "-12.5")
    assert _published_lease(rdb) == {"generation": None, "daily_pnl": -12.5}


async def test_tick_revokes_existing_lease_when_realized_pnl_unavailable() -> None:
    """Regression (P0): the snapshot returning None (DB error) must NOT be
    treated as '$0 realized today', and must actively revoke any already-valid
    lease, not just skip renewing it."""
    rdb = _mock_rdb()
    with _patch_snapshot(None):
        await _tick({"bybit": _mock_exchange(-5.0)}, rdb, db_url="postgresql://x")

    rdb.set.assert_not_called()
    rdb.delete.assert_called_once_with("risk:pnl_ready")


async def test_tick_revokes_existing_lease_on_partial_exchange_failure() -> None:
    """Regression (P0): an exchange fetch failure means current exposure can't
    be verified either, must actively revoke any existing lease."""
    rdb = _mock_rdb()
    exchanges = {"bybit": _mock_exchange(-20.0), "bingx": _mock_exchange(0.0, fail=True)}
    with _patch_snapshot(_snapshot()) as mock_snapshot:
        await _tick(exchanges, rdb, db_url="postgresql://x")

    # Fail-closed: don't even query realized PnL if unrealized data is incomplete.
    mock_snapshot.assert_not_called()
    rdb.set.assert_not_called()
    rdb.delete.assert_called_once_with("risk:pnl_ready")


async def test_tick_revokes_existing_lease_while_close_pending() -> None:
    """Regression (P0): a close confirmed on the exchange but not yet
    committed to the journal isn't in the snapshot yet. An existing lease must
    be actively revoked in this window, not just left to expire."""
    rdb = _mock_rdb()
    with _patch_snapshot(_snapshot(-20.0)), _patch_no_pending(return_value=True) as mock_pending:
        await _tick({"bybit": _mock_exchange(-5.0)}, rdb, db_url="postgresql://x")

    mock_pending.assert_called_once()
    rdb.set.assert_not_called()
    rdb.delete.assert_called_once_with("risk:pnl_ready")


@pytest.mark.parametrize(
    "blocker",
    ["partial_close_outstanding", "fill_incident_outstanding", "live_costs_unsettled"],
)
async def test_tick_revokes_existing_lease_while_the_journal_is_not_final(blocker: str) -> None:
    """A partial close, an unconfirmed fill price, or a closed live trade whose
    exchange costs are not settled: the realized figure is not final."""
    rdb = _mock_rdb()
    with _patch_snapshot(_snapshot(-20.0, blocker=blocker)), _patch_no_pending():
        await _tick({"bybit": _mock_exchange(-5.0)}, rdb, db_url="postgresql://x")

    rdb.set.assert_not_called()
    rdb.delete.assert_called_once_with("risk:pnl_ready")


async def test_tick_revokes_lease_if_pending_close_lands_between_check_and_publish() -> None:
    """Regression (P0, race): a pending-close marker can appear between this
    tick's own any_pending_closes() check and its SET of the readiness lease."""
    rdb = _mock_rdb()
    with (
        _patch_snapshot(_snapshot(-10.0)),
        patch(
            "schurfer_execution.tracker.journal.any_pending_closes",
            AsyncMock(side_effect=[False, True]),
        ),
    ):
        await _tick({"bybit": _mock_exchange(-5.0)}, rdb, db_url="postgresql://x")

    set_keys = [c.args[0] for c in rdb.set.call_args_list]
    assert "risk:pnl_ready" in set_keys
    assert rdb.delete.call_args_list[-1].args == ("risk:pnl_ready",)


async def test_close_committed_between_snapshot_and_publish_never_admits_an_entry() -> None:
    """The colleague's race: the tick reads the journal, then a close commits (its
    trigger moves the generation) and revokes, then the tick publishes from the old
    figures. try_commit_close revokes BEFORE its commit, and a committed close
    leaves no pending-close marker, so the marker recheck alone misses it. The
    lease is revoked after publish, and even a lease left standing is refused at
    entry because its generation is no longer the journal's."""
    rdb = _mock_rdb()
    with (
        _patch_snapshot(_snapshot(-10.0)),
        _patch_no_pending(),
        patch(
            "schurfer_execution.tracker.journal.pnl_generation",
            AsyncMock(return_value=_GENERATION + 1),
        ),
    ):
        await _tick({"bybit": _mock_exchange(-5.0)}, rdb, db_url="postgresql://x")

    lease = decode_pnl_lease(json.dumps(_published_lease(rdb)))
    assert rdb.delete.call_args_list[-1].args == ("risk:pnl_ready",)
    assert pnl_lease_flag(lease, _GENERATION + 1, database=True) is None


async def test_net_settlement_between_snapshot_and_publish_never_admits_an_entry() -> None:
    """The tick read the journal before a live trade closed, so nothing blocked it;
    the close (with its estimate) and the cost worker's net figures then both
    committed before the publish, moving the generation twice. The lease carries
    the old generation and admits nothing."""
    rdb = _mock_rdb()
    with (
        _patch_snapshot(_snapshot(-10.0)),
        _patch_no_pending(),
        patch(
            "schurfer_execution.tracker.journal.pnl_generation",
            AsyncMock(return_value=_GENERATION + 2),
        ),
    ):
        await _tick({"bybit": _mock_exchange(0.0)}, rdb, db_url="postgresql://x")

    lease = decode_pnl_lease(json.dumps(_published_lease(rdb)))
    assert lease is not None and lease.generation == _GENERATION
    assert pnl_lease_flag(lease, _GENERATION + 2, database=True) is None
    assert rdb.delete.call_args_list[-1].args == ("risk:pnl_ready",)


async def test_unreadable_generation_after_publish_revokes() -> None:
    rdb = _mock_rdb()
    with (
        _patch_snapshot(_snapshot()),
        _patch_no_pending(),
        patch("schurfer_execution.tracker.journal.pnl_generation", AsyncMock(return_value=None)),
    ):
        await _tick({"bybit": _mock_exchange(0.0)}, rdb, db_url="postgresql://x")

    assert rdb.delete.call_args_list[-1].args == ("risk:pnl_ready",)


async def test_tick_sums_unrealized_across_exchanges() -> None:
    rdb = _mock_rdb()
    ex1 = MagicMock()
    ex1.fetch_positions = AsyncMock(
        return_value=[
            {"contracts": 1.0, "unrealizedPnl": -30.0, "symbol": "BEAT/USDT:USDT"},
            {"contracts": 1.0, "unrealizedPnl": 10.0, "symbol": "ACT/USDT:USDT"},
        ]
    )
    ex2 = MagicMock()
    ex2.fetch_positions = AsyncMock(
        return_value=[{"contracts": 1.0, "unrealizedPnl": -5.5, "symbol": "SYN/USDT:USDT"}]
    )
    with _patch_snapshot(_snapshot()), _patch_no_pending():
        await _tick({"bybit": ex1, "bingx": ex2}, rdb, db_url="postgresql://x")

    rdb.set.assert_any_call("trading:daily_pnl", "-25.5")
