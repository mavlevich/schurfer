from unittest.mock import AsyncMock, MagicMock, patch

from schurfer_execution.tracker import _tick


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


def _patch_no_pending(return_value: bool = False):  # type: ignore[no-untyped-def]
    return patch(
        "schurfer_execution.tracker.journal.any_pending_closes",
        AsyncMock(return_value=return_value),
    )


async def test_tick_combines_realized_and_unrealized() -> None:
    rdb = _mock_rdb()
    with (
        patch(
            "schurfer_execution.tracker.journal.realized_pnl_today", AsyncMock(return_value=-20.0)
        ),
        _patch_no_pending(),
    ):
        await _tick({"bybit": _mock_exchange(-5.0)}, rdb, db_url="postgresql://x")

    rdb.set.assert_any_call("trading:daily_pnl", "-25.0")
    rdb.set.assert_any_call("risk:pnl_ready", "1", ex=120)


async def test_tick_recomputes_from_source_of_truth_every_call() -> None:
    """Regression: daily_pnl must not depend on in-process state that resets
    on restart — every tick recomputes realized PnL from the journal (DB),
    so a process restart mid-day can't silently wipe out today's tracked loss."""
    rdb = _mock_rdb()
    with (
        patch(
            "schurfer_execution.tracker.journal.realized_pnl_today",
            AsyncMock(return_value=-100.0),
        ),
        _patch_no_pending(),
    ):
        await _tick({"bybit": _mock_exchange(0.0)}, rdb, db_url="postgresql://x")
        await _tick({"bybit": _mock_exchange(0.0)}, rdb, db_url="postgresql://x")

    # Both ticks reflect the same DB-sourced realized loss — no reset, no drift.
    daily_pnl_calls = [c for c in rdb.set.call_args_list if c.args[0] == "trading:daily_pnl"]
    assert daily_pnl_calls[0].args == ("trading:daily_pnl", "-100.0")
    assert daily_pnl_calls[1].args == ("trading:daily_pnl", "-100.0")


async def test_tick_skips_realized_lookup_when_db_not_configured() -> None:
    rdb = _mock_rdb()
    with patch(
        "schurfer_execution.tracker.journal.realized_pnl_today", AsyncMock(return_value=-999.0)
    ) as mock_realized:
        await _tick({"bybit": _mock_exchange(-12.5)}, rdb, db_url=None)

    mock_realized.assert_not_called()
    rdb.set.assert_any_call("trading:daily_pnl", "-12.5")
    rdb.set.assert_any_call("risk:pnl_ready", "1", ex=120)


async def test_tick_revokes_existing_lease_when_realized_pnl_unavailable() -> None:
    """Regression (P0): journal.realized_pnl_today() returning None (DB error)
    must NOT be treated as '$0 realized today', and must actively revoke any
    already-valid lease — not just skip renewing it. A lease set on an earlier
    tick would otherwise keep permitting trades for up to its remaining TTL
    even though we just discovered we can't verify today's real loss."""
    rdb = _mock_rdb()
    with patch(
        "schurfer_execution.tracker.journal.realized_pnl_today", AsyncMock(return_value=None)
    ):
        await _tick({"bybit": _mock_exchange(-5.0)}, rdb, db_url="postgresql://x")

    rdb.set.assert_not_called()
    rdb.delete.assert_called_once_with("risk:pnl_ready")


async def test_tick_revokes_existing_lease_on_partial_exchange_failure() -> None:
    """Regression (P0): an exchange fetch failure means current exposure can't
    be verified either — must actively revoke any existing lease, not just
    withhold renewal and wait for it to expire on its own."""
    rdb = _mock_rdb()
    exchanges = {"bybit": _mock_exchange(-20.0), "bingx": _mock_exchange(0.0, fail=True)}
    with patch(
        "schurfer_execution.tracker.journal.realized_pnl_today", AsyncMock(return_value=0.0)
    ) as mock_realized:
        await _tick(exchanges, rdb, db_url="postgresql://x")

    # Fail-closed: don't even query realized PnL if unrealized data is incomplete.
    mock_realized.assert_not_called()
    rdb.set.assert_not_called()
    rdb.delete.assert_called_once_with("risk:pnl_ready")


async def test_tick_revokes_existing_lease_while_close_pending() -> None:
    """Regression (P0): a close confirmed on the exchange but not yet
    committed to the journal isn't reflected in realized_pnl_today() yet —
    its trades row is still 'open'. An existing lease must be actively
    revoked in this window, not just left to expire — otherwise new trades
    stay permitted for up to its remaining TTL despite the understated PnL."""
    rdb = _mock_rdb()
    with (
        patch(
            "schurfer_execution.tracker.journal.realized_pnl_today", AsyncMock(return_value=-20.0)
        ),
        _patch_no_pending(return_value=True) as mock_pending,
    ):
        await _tick({"bybit": _mock_exchange(-5.0)}, rdb, db_url="postgresql://x")

    mock_pending.assert_called_once()
    rdb.set.assert_not_called()
    rdb.delete.assert_called_once_with("risk:pnl_ready")


async def test_tick_revokes_lease_if_pending_close_lands_between_check_and_publish() -> None:
    """Regression (P0, race): run_pnl_tracker and run_position_monitor are
    concurrent asyncio tasks. A close's journal write (and its pending-close
    marker) can land in the gap between this tick's own any_pending_closes()
    check and its SET of the readiness lease. Simulated here via a two-value
    side_effect: the first check (before publishing) sees no pending close,
    but a second one appears by the time the tick re-checks right after
    publishing — the lease must be revoked, not left standing."""
    rdb = _mock_rdb()
    with (
        patch(
            "schurfer_execution.tracker.journal.realized_pnl_today", AsyncMock(return_value=-10.0)
        ),
        patch(
            "schurfer_execution.tracker.journal.any_pending_closes",
            AsyncMock(side_effect=[False, True]),
        ),
    ):
        await _tick({"bybit": _mock_exchange(-5.0)}, rdb, db_url="postgresql://x")

    # The lease was published, then immediately revoked once the race was detected.
    set_keys = [c.args[0] for c in rdb.set.call_args_list]
    assert "risk:pnl_ready" in set_keys
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
    with (
        patch("schurfer_execution.tracker.journal.realized_pnl_today", AsyncMock(return_value=0.0)),
        _patch_no_pending(),
    ):
        await _tick({"bybit": ex1, "bingx": ex2}, rdb, db_url="postgresql://x")

    rdb.set.assert_any_call("trading:daily_pnl", "-25.5")
