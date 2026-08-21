import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_execution import journal
from schurfer_execution.monitor import (
    _check_exit,
    _parse_sl_key,
    _reconcile_one,
    _reconcile_vanished_positions,
    _retry_one_pending_close,
    _retry_pending_closes,
)
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


async def _async_iter(items: list) -> object:  # type: ignore[type-arg]
    for item in items:
        yield item


def _mock_cfg(db_url: str | None = "postgresql://x") -> MagicMock:
    cfg = MagicMock()
    cfg.db_url = db_url
    cfg.telegram_bot_token = None
    cfg.telegram_chat_id = None
    return cfg


class TestParseSlKey:
    def test_parses_valid_key(self) -> None:
        assert _parse_sl_key("position:sl_order_id:bingx:BEAT") == ("bingx", "BEAT")

    def test_rejects_malformed_key(self) -> None:
        assert _parse_sl_key("position:sl_order_id:bingx") is None
        assert _parse_sl_key("garbage") is None


class TestReconcileVanishedPositions:
    async def test_skips_pairs_still_live(self) -> None:
        rdb = MagicMock()
        rdb.scan_iter = MagicMock(return_value=_async_iter([b"position:sl_order_id:bingx:BEAT"]))
        cfg = _mock_cfg()

        with patch("schurfer_execution.monitor._reconcile_one", AsyncMock()) as mock_recon:
            await _reconcile_vanished_positions({}, rdb, cfg, live_pairs={("bingx", "BEAT")})

        mock_recon.assert_not_called()

    async def test_reconciles_vanished_pair(self) -> None:
        rdb = MagicMock()
        rdb.scan_iter = MagicMock(return_value=_async_iter([b"position:sl_order_id:bingx:BEAT"]))
        cfg = _mock_cfg()

        with patch("schurfer_execution.monitor._reconcile_one", AsyncMock()) as mock_recon:
            await _reconcile_vanished_positions({}, rdb, cfg, live_pairs=set())

        mock_recon.assert_called_once_with("bingx", "BEAT", {}, rdb, cfg)

    async def test_one_bad_key_does_not_block_others(self) -> None:
        rdb = MagicMock()
        rdb.scan_iter = MagicMock(
            return_value=_async_iter(
                [b"position:sl_order_id:bingx:BEAT", b"position:sl_order_id:okx:ACT"]
            )
        )
        cfg = _mock_cfg()

        async def _boom(*args: object, **kwargs: object) -> None:
            if args[0] == "bingx":
                raise RuntimeError("exchange unreachable")

        with patch("schurfer_execution.monitor._reconcile_one", _boom):
            await _reconcile_vanished_positions({}, rdb, cfg, live_pairs=set())
        # No assertion needed beyond "did not raise" — the second key must
        # still be processed despite the first one's failure.


def _rdb_with(entries: dict[str, bytes | None]) -> MagicMock:
    rdb = MagicMock()

    async def _get(key: str) -> bytes | None:
        return entries.get(key)

    rdb.get = AsyncMock(side_effect=_get)
    rdb.delete = AsyncMock()
    return rdb


class TestCheckExitNotifyPnlUsd:
    async def test_uses_cached_entry_size_not_fetch_positions_live_notional(self) -> None:
        """Regression: fetch_positions' own size_usd on the position dict is
        the CURRENT mark-to-market notional (contracts * mark_price), not
        the entry-time size. For a short in profit, price has dropped, so
        that notional has already shrunk with it -- multiplying the shrunk
        notional by the percent gain understates the real dollar profit
        (10 contracts, $10 -> $8: true profit is $20, but 80 (shrunk
        notional) * 20% = only $16). The entry-time size cached in Redis at
        open (same value notify_open showed) must be used instead."""
        position = {
            "exchange": "bingx",
            "base": "BEAT",
            "symbol": "BEAT/USDT:USDT",
            "side": "short",
            "entry_price": 10.0,
            "mark_price": 8.0,
            # fetch_positions' own (misleading) current notional -- must be
            # ignored as the PnL multiplier.
            "size_usd": 80.0,
        }
        rdb = _rdb_with(
            {
                "position:size_usd:bingx:BEAT": b"100.0",
                "trade:id:bingx:BEAT": b"42",
            }
        )

        with (
            patch(
                "schurfer_execution.monitor.exit_module.check_exit",
                AsyncMock(return_value="max_hold age=180min"),
            ),
            patch(
                "schurfer_execution.monitor.close_position",
                AsyncMock(return_value={"closed": True, "exit_price": 8.0, "order_id": "o1"}),
            ),
            patch(
                "schurfer_execution.monitor.journal.try_commit_close",
                AsyncMock(return_value=True),
            ),
            patch("schurfer_execution.monitor.journal.delete_trade_id_if_matches", AsyncMock()),
            patch(
                "schurfer_execution.monitor.notify.credentials",
                return_value=("tok", "chat"),
            ),
            patch(
                "schurfer_execution.monitor.notify.notify_close", AsyncMock()
            ) as mock_notify_close,
        ):
            await _check_exit(position, rdb, _mock_cfg(), {"bingx": MagicMock()})

        kw = mock_notify_close.call_args.kwargs
        assert kw["pnl_pct"] == pytest.approx(20.0)
        # Correct: 100 (cached entry notional) * 20% = $20, not
        # 80 (fetch_positions' shrunk live notional) * 20% = $16.
        assert kw["pnl_usd"] == pytest.approx(20.0)

    async def test_pnl_usd_is_none_when_size_usd_key_missing(self) -> None:
        position = {
            "exchange": "bingx",
            "base": "BEAT",
            "symbol": "BEAT/USDT:USDT",
            "side": "short",
            "entry_price": 10.0,
            "mark_price": 8.0,
        }
        rdb = _rdb_with({"trade:id:bingx:BEAT": b"42"})

        with (
            patch(
                "schurfer_execution.monitor.exit_module.check_exit",
                AsyncMock(return_value="max_hold age=180min"),
            ),
            patch(
                "schurfer_execution.monitor.close_position",
                AsyncMock(return_value={"closed": True, "exit_price": 8.0, "order_id": "o1"}),
            ),
            patch(
                "schurfer_execution.monitor.journal.try_commit_close",
                AsyncMock(return_value=True),
            ),
            patch("schurfer_execution.monitor.journal.delete_trade_id_if_matches", AsyncMock()),
            patch(
                "schurfer_execution.monitor.notify.credentials",
                return_value=("tok", "chat"),
            ),
            patch(
                "schurfer_execution.monitor.notify.notify_close", AsyncMock()
            ) as mock_notify_close,
        ):
            await _check_exit(position, rdb, _mock_cfg(), {"bingx": MagicMock()})

        assert mock_notify_close.call_args.kwargs["pnl_usd"] is None


class TestReconcileOne:
    async def test_sl_filled_closes_journal_and_cleans_up(self) -> None:
        ex = MagicMock()
        ex.fetch_order = AsyncMock(return_value={"status": "closed", "average": 1.2, "id": "sl-1"})
        rdb = _rdb_with(
            {
                "position:sl_order_id:bingx:BEAT": b"sl-1",
                "position:entry:bingx:BEAT": b"1.0",
                "position:side:bingx:BEAT": b"short",
                "trade:id:bingx:BEAT": b"42",
            }
        )
        cfg = _mock_cfg()

        with (
            patch(
                "schurfer_execution.monitor.journal.try_commit_close",
                AsyncMock(return_value=True),
            ) as mock_close,
            patch(
                "schurfer_execution.monitor.journal.delete_trade_id_if_matches",
                AsyncMock(return_value=True),
            ) as mock_cas_delete,
        ):
            await _reconcile_one("bingx", "BEAT", {"bingx": ex}, rdb, cfg)

        mock_close.assert_called_once()
        call_kwargs = mock_close.call_args.kwargs
        assert call_kwargs["trade_id"] == 42
        assert call_kwargs["exit_price"] == 1.2
        assert call_kwargs["reason"] == "exchange_stop_loss_triggered"
        assert call_kwargs["exchange"] == "bingx"
        assert call_kwargs["base"] == "BEAT"

        # Trade id pointer removed only because the commit succeeded, and via
        # compare-and-delete (not an unconditional delete).
        mock_cas_delete.assert_called_once_with(rdb, "trade:id:bingx:BEAT", 42)
        rdb.delete.assert_any_call("position:sl_order_id:bingx:BEAT")
        rdb.delete.assert_any_call("position:opened_at:bingx:BEAT")

    async def test_sl_not_filled_does_not_touch_state(self) -> None:
        ex = MagicMock()
        ex.fetch_order = AsyncMock(return_value={"status": "open", "id": "sl-1"})

        rdb = MagicMock()
        rdb.get = AsyncMock(return_value=b"sl-1")
        rdb.delete = AsyncMock()
        cfg = _mock_cfg()

        with patch(
            "schurfer_execution.monitor.journal.try_commit_close", AsyncMock()
        ) as mock_close:
            await _reconcile_one("bingx", "BEAT", {"bingx": ex}, rdb, cfg)

        mock_close.assert_not_called()
        rdb.delete.assert_not_called()

    async def test_unresolvable_exit_price_touches_nothing_but_revokes_readiness(self) -> None:
        """Regression: a filled SL order with no average/price and no fetchable
        trades must NOT be treated as exit_price=0 (which would read as a
        false +100% profit on a short) — and must leave position state alone
        so the next tick retries instead of losing the position silently.
        The PnL impact is unknown at this point though, so the readiness
        lease must still be revoked immediately (P0)."""
        ex = MagicMock()
        ex.fetch_order = AsyncMock(return_value={"status": "closed", "id": "sl-1"})
        ex.has = {"fetchOrderTrades": True, "fetchMyTrades": False}
        ex.fetch_order_trades = AsyncMock(return_value=[])

        rdb = _rdb_with({"position:sl_order_id:bingx:BEAT": b"sl-1"})
        # db_url=None: this test is about never fabricating a price and still
        # revoking readiness, not about incident creation (covered separately
        # in test_incidents.py / test_fill_price.py).
        cfg = _mock_cfg(db_url=None)

        with (
            patch("schurfer_execution.monitor.journal.try_commit_close", AsyncMock()) as mock_close,
            patch(
                "schurfer_execution.monitor.journal.revoke_pnl_readiness", AsyncMock()
            ) as mock_revoke,
        ):
            await _reconcile_one("bingx", "BEAT", {"bingx": ex}, rdb, cfg)

        mock_close.assert_not_called()
        mock_revoke.assert_called_once_with(rdb)
        rdb.delete.assert_not_called()  # no position-scoped keys touched

    async def test_falls_back_to_weighted_trades_when_order_fields_missing(self) -> None:
        ex = MagicMock()
        ex.fetch_order = AsyncMock(return_value={"status": "closed", "id": "sl-1"})
        ex.has = {"fetchOrderTrades": True, "fetchMyTrades": False}
        ex.fetch_order_trades = AsyncMock(
            return_value=[{"price": 100.0, "amount": 1.0}, {"price": 110.0, "amount": 3.0}]
        )
        rdb = _rdb_with(
            {
                "position:sl_order_id:bingx:BEAT": b"sl-1",
                "position:entry:bingx:BEAT": b"1.0",
                "position:side:bingx:BEAT": b"short",
                "trade:id:bingx:BEAT": b"42",
            }
        )
        cfg = _mock_cfg()

        with (
            patch(
                "schurfer_execution.monitor.journal.try_commit_close",
                AsyncMock(return_value=True),
            ) as mock_close,
            patch("schurfer_execution.monitor.journal.delete_trade_id_if_matches", AsyncMock()),
        ):
            await _reconcile_one("bingx", "BEAT", {"bingx": ex}, rdb, cfg)

        assert mock_close.call_args.kwargs["exit_price"] == 107.5

    async def test_unresolved_sl_fill_creates_a_durable_incident_and_alerts_once(self) -> None:
        ex = MagicMock()
        ex.fetch_order = AsyncMock(return_value={"status": "closed", "id": "sl-1"})
        ex.has = {"fetchOrderTrades": False, "fetchMyTrades": False}
        rdb = _rdb_with({"position:sl_order_id:bingx:BEAT": b"sl-1", "trade:id:bingx:BEAT": b"42"})
        cfg = _mock_cfg()

        with (
            patch(
                "schurfer_execution.monitor.incidents.create_incident",
                AsyncMock(return_value=7),
            ) as mock_create,
            patch(
                "schurfer_execution.monitor.incidents.claim_creation_notification",
                AsyncMock(return_value=True),
            ),
            patch(
                "schurfer_execution.monitor.notify.credentials",
                return_value=("tok", "chat"),
            ),
            patch("schurfer_execution.monitor.notify.notify_alert", AsyncMock()) as mock_alert,
            patch("schurfer_execution.monitor.journal.revoke_pnl_readiness", AsyncMock()),
        ):
            await _reconcile_one("bingx", "BEAT", {"bingx": ex}, rdb, cfg)

        mock_create.assert_called_once()
        assert mock_create.call_args.kwargs["operation"] == "close"
        assert mock_create.call_args.kwargs["order_id"] == "sl-1"
        assert mock_create.call_args.kwargs["trade_id"] == 42
        mock_alert.assert_awaited_once()

    async def test_journal_close_failure_keeps_trade_id_but_cleans_position_keys(self) -> None:
        """The exchange-side SL fill is already confirmed, so position-monitoring
        keys are safe to clean up regardless of the journal outcome — retry is
        now handled by journal:pending_close (written inside try_commit_close),
        not by preserving these keys. Only the trade-id pointer is conditional,
        and it's only ever removed via compare-and-delete."""
        ex = MagicMock()
        ex.fetch_order = AsyncMock(return_value={"status": "closed", "average": 1.2, "id": "sl-1"})
        rdb = _rdb_with(
            {
                "position:sl_order_id:bingx:BEAT": b"sl-1",
                "position:entry:bingx:BEAT": b"1.0",
                "position:side:bingx:BEAT": b"short",
                "trade:id:bingx:BEAT": b"42",
            }
        )
        cfg = _mock_cfg()

        with (
            patch(
                "schurfer_execution.monitor.journal.try_commit_close",
                AsyncMock(return_value=False),
            ),
            patch(
                "schurfer_execution.monitor.journal.delete_trade_id_if_matches", AsyncMock()
            ) as mock_cas_delete,
        ):
            await _reconcile_one("bingx", "BEAT", {"bingx": ex}, rdb, cfg)

        mock_cas_delete.assert_not_called()
        delete_calls = [c.args[0] for c in rdb.delete.call_args_list]
        assert "position:sl_order_id:bingx:BEAT" in delete_calls
        assert "position:entry:bingx:BEAT" in delete_calls

    async def test_notify_close_includes_pnl_usd_when_size_usd_cached(self) -> None:
        ex = MagicMock()
        ex.fetch_order = AsyncMock(return_value={"status": "closed", "average": 1.2, "id": "sl-1"})
        rdb = _rdb_with(
            {
                "position:sl_order_id:bingx:BEAT": b"sl-1",
                "position:entry:bingx:BEAT": b"1.0",
                "position:side:bingx:BEAT": b"short",
                "position:size_usd:bingx:BEAT": b"50.0",
                "trade:id:bingx:BEAT": b"42",
            }
        )
        cfg = _mock_cfg()

        with (
            patch(
                "schurfer_execution.monitor.journal.try_commit_close",
                AsyncMock(return_value=True),
            ),
            patch("schurfer_execution.monitor.journal.delete_trade_id_if_matches", AsyncMock()),
            patch(
                "schurfer_execution.monitor.notify.credentials",
                return_value=("tok", "chat"),
            ),
            patch(
                "schurfer_execution.monitor.notify.notify_close", AsyncMock()
            ) as mock_notify_close,
        ):
            await _reconcile_one("bingx", "BEAT", {"bingx": ex}, rdb, cfg)

        kw = mock_notify_close.call_args.kwargs
        # entry=1.0, exit=1.2, short -> -20% (a loss), on $50 -> -$10.
        assert kw["pnl_pct"] == pytest.approx(-20.0)
        assert kw["pnl_usd"] == pytest.approx(-10.0)
        rdb.delete.assert_any_call("position:size_usd:bingx:BEAT")

    async def test_notify_close_pnl_usd_is_none_when_size_usd_key_missing(self) -> None:
        # No position:size_usd:* entry cached -- an older position or an
        # evicted key. Must show percent alone, never fabricate a figure.
        ex = MagicMock()
        ex.fetch_order = AsyncMock(return_value={"status": "closed", "average": 1.2, "id": "sl-1"})
        rdb = _rdb_with(
            {
                "position:sl_order_id:bingx:BEAT": b"sl-1",
                "position:entry:bingx:BEAT": b"1.0",
                "position:side:bingx:BEAT": b"short",
                "trade:id:bingx:BEAT": b"42",
            }
        )
        cfg = _mock_cfg()

        with (
            patch(
                "schurfer_execution.monitor.journal.try_commit_close",
                AsyncMock(return_value=True),
            ),
            patch("schurfer_execution.monitor.journal.delete_trade_id_if_matches", AsyncMock()),
            patch(
                "schurfer_execution.monitor.notify.credentials",
                return_value=("tok", "chat"),
            ),
            patch(
                "schurfer_execution.monitor.notify.notify_close", AsyncMock()
            ) as mock_notify_close,
        ):
            await _reconcile_one("bingx", "BEAT", {"bingx": ex}, rdb, cfg)

        assert mock_notify_close.call_args.kwargs["pnl_usd"] is None

    async def test_no_sl_order_id_is_a_noop(self) -> None:
        rdb = MagicMock()
        rdb.get = AsyncMock(return_value=None)
        rdb.delete = AsyncMock()
        cfg = _mock_cfg()

        with patch(
            "schurfer_execution.monitor.journal.try_commit_close", AsyncMock()
        ) as mock_close:
            await _reconcile_one("bingx", "BEAT", {}, rdb, cfg)

        mock_close.assert_not_called()
        rdb.delete.assert_not_called()


class TestRetryPendingCloses:
    async def test_retries_and_commits_a_pending_close(self) -> None:
        pending_payload = json.dumps(
            {
                "trade_id": 42,
                "exit_order_id": "sl-1",
                "exit_price": 1.2,
                "reason": "exchange_stop_loss_triggered",
            }
        )
        rdb = MagicMock()
        rdb.get = AsyncMock(return_value=pending_payload.encode())
        cfg = _mock_cfg()

        with (
            patch(
                "schurfer_execution.monitor.journal.try_commit_close",
                AsyncMock(return_value=True),
            ) as mock_close,
            patch(
                "schurfer_execution.monitor.journal.delete_trade_id_if_matches",
                AsyncMock(return_value=True),
            ) as mock_cas_delete,
        ):
            await _retry_one_pending_close("bingx", "BEAT", 42, rdb, cfg)

        mock_close.assert_called_once()
        assert mock_close.call_args.kwargs["trade_id"] == 42
        mock_cas_delete.assert_called_once_with(rdb, "trade:id:bingx:BEAT", 42)

    async def test_still_pending_leaves_trade_id_alone(self) -> None:
        rdb = MagicMock()
        rdb.get = AsyncMock(
            return_value=json.dumps(
                {
                    "trade_id": 42,
                    "exit_order_id": "sl-1",
                    "exit_price": 1.2,
                    "reason": "exchange_stop_loss_triggered",
                }
            ).encode()
        )
        cfg = _mock_cfg()

        with (
            patch(
                "schurfer_execution.monitor.journal.try_commit_close",
                AsyncMock(return_value=False),
            ),
            patch(
                "schurfer_execution.monitor.journal.delete_trade_id_if_matches", AsyncMock()
            ) as mock_cas_delete,
        ):
            await _retry_one_pending_close("bingx", "BEAT", 42, rdb, cfg)

        mock_cas_delete.assert_not_called()

    async def test_no_pending_marker_is_a_noop(self) -> None:
        rdb = MagicMock()
        rdb.get = AsyncMock(return_value=None)
        cfg = _mock_cfg()

        with patch(
            "schurfer_execution.monitor.journal.try_commit_close", AsyncMock()
        ) as mock_close:
            await _retry_one_pending_close("bingx", "BEAT", 42, rdb, cfg)

        mock_close.assert_not_called()

    async def test_scan_skips_when_db_not_configured(self) -> None:
        rdb = MagicMock()
        rdb.scan_iter = MagicMock()
        cfg = _mock_cfg(db_url=None)

        await _retry_pending_closes(rdb, cfg)

        rdb.scan_iter.assert_not_called()

    async def test_end_to_end_retry_recovers_after_transient_db_failure(self) -> None:
        """Regression: the full recovery path — a close that fails to commit
        must actually get committed on a later retry, proving the mechanism
        recovers rather than just leaving a log line. Uses the real
        try_commit_close/delete_trade_id_if_matches, only journal.close_trade
        (the DB call) and the CAS-delete's rdb.eval are faked."""
        ex = MagicMock()
        ex.fetch_order = AsyncMock(return_value={"status": "closed", "average": 1.2, "id": "sl-1"})

        store: dict[str, bytes | None] = {
            "position:sl_order_id:bingx:BEAT": b"sl-1",
            "position:entry:bingx:BEAT": b"1.0",
            "position:side:bingx:BEAT": b"short",
            "trade:id:bingx:BEAT": b"42",
        }

        async def _get(key: str) -> bytes | None:
            return store.get(key)

        async def _set(key: str, value: str, **kw: object) -> None:
            store[key] = value.encode()

        async def _delete(key: str) -> None:
            store.pop(key, None)

        async def _eval(_script: str, _numkeys: int, key: str, expected: str) -> int:
            # Mirrors the real Lua CAS: delete only if current value matches.
            current = store.get(key)
            if current is not None and current.decode() == expected:
                store.pop(key, None)
                return 1
            return 0

        rdb = MagicMock()
        rdb.get = AsyncMock(side_effect=_get)
        rdb.set = AsyncMock(side_effect=_set)
        rdb.delete = AsyncMock(side_effect=_delete)
        rdb.eval = AsyncMock(side_effect=_eval)
        cfg = _mock_cfg()

        # First reconciliation tick: journal commit fails (DB down).
        with patch(
            "schurfer_execution.monitor.journal.close_trade",
            AsyncMock(return_value=journal.CloseOutcome(committed=False)),
        ):
            await _reconcile_one("bingx", "BEAT", {"bingx": ex}, rdb, cfg)

        assert "trade:id:bingx:BEAT" in store
        assert "journal:pending_close:bingx:BEAT:42" in store

        # Second tick (DB back up): retry succeeds and cleans up.
        with patch(
            "schurfer_execution.monitor.journal.close_trade",
            AsyncMock(return_value=journal.CloseOutcome(committed=True)),
        ):
            await _retry_one_pending_close("bingx", "BEAT", 42, rdb, cfg)

        assert "trade:id:bingx:BEAT" not in store
        assert "journal:pending_close:bingx:BEAT:42" not in store
