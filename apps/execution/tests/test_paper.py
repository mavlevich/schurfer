"""Tests for paper.py — paper trading open/close via Redis."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_execution import journal
from schurfer_execution.paper import _tick, close_paper, open_paper, paper_key
from schurfer_execution.symbols import ExecutionInstrument
from schurfer_performance import PAPER_ACCOUNTING_VERSION


def _cfg() -> MagicMock:
    cfg = MagicMock()
    cfg.db_url = None
    cfg.telegram_bot_token = None
    cfg.telegram_chat_id = None
    return cfg


def _rdb() -> MagicMock:
    rdb = MagicMock()
    rdb.set = AsyncMock()
    rdb.get = AsyncMock(return_value=None)
    rdb.delete = AsyncMock()
    return rdb


def _instrument() -> ExecutionInstrument:
    return ExecutionInstrument(
        exchange="bybit",
        symbol="BEAT/USDT:USDT",
        native_market_id="BEATUSDT",
        base="BEAT",
        quote="USDT",
        settle="USDT",
        market_type="swap",
    )


# --- paper_key ---


def test_paper_key_format() -> None:
    assert paper_key("bybit", "beat") == "position:paper:bybit:BEAT"
    assert paper_key("bingx", "SOL") == "position:paper:bingx:SOL"


# --- open_paper ---


async def test_open_paper_stores_position_in_redis() -> None:
    rdb = _rdb()
    await open_paper(
        rdb,
        instrument=_instrument(),
        price=0.0025,
        size_usd=50.0,
        leverage=3,
        score=8,
        setup_context={"pump_pct": 45.0},
        cfg=_cfg(),
    )

    rdb.set.assert_called_once()
    call_args = rdb.set.call_args
    key = call_args.args[0]
    value = json.loads(call_args.args[1])
    assert key == "position:paper:bybit:BEAT"
    assert value["base"] == "BEAT"
    assert value["exchange"] == "bybit"
    assert value["side"] == "short"
    assert value["entry_price"] == 0.0025
    assert value["size_usd"] == 50.0
    assert value["leverage"] == 3
    assert value["score"] == 8
    assert "opened_at" in value
    assert "exit_params" in value
    assert value["accounting_version"] == PAPER_ACCOUNTING_VERSION
    assert value["entry_slippage_bps"] is None
    assert value["exit_slippage_bps"] is None
    assert value["exit_params"]["initial_sl_pct"] == 8.0  # pump_pct=45 → small bucket


async def test_open_paper_writes_journal_when_db_url_set() -> None:
    rdb = _rdb()
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"

    with patch("schurfer_execution.paper.journal.open_trade", new_callable=AsyncMock) as mock_jrn:
        mock_jrn.return_value = 42
        await open_paper(
            rdb,
            instrument=_instrument(),
            price=0.0025,
            size_usd=50.0,
            leverage=3,
            score=8,
            setup_context={},
            cfg=cfg,
        )

    mock_jrn.assert_called_once()
    kw = mock_jrn.call_args.kwargs
    assert kw["symbol"] == "BEAT/USDT:USDT"
    assert kw["entry_price"] == 0.0025
    assert kw["setup_context"]["paper"] is True


async def test_open_paper_does_not_write_journal_without_db_url() -> None:
    rdb = _rdb()
    with patch("schurfer_execution.paper.journal.open_trade", new_callable=AsyncMock) as mock_jrn:
        await open_paper(
            rdb,
            instrument=_instrument(),
            price=0.0025,
            size_usd=50.0,
            leverage=3,
            score=8,
            setup_context={},
            cfg=_cfg(),
        )

    mock_jrn.assert_not_called()


# --- close_paper ---


async def test_close_paper_deletes_redis_key() -> None:
    rdb = _rdb()
    pos = {"base": "BEAT", "exchange": "bybit", "entry_price": 0.0030, "side": "short"}

    await close_paper(
        rdb, pos=pos, current_price=0.0025, reason="take_profit pnl=16.7%", cfg=_cfg()
    )

    rdb.delete.assert_any_call("position:paper:bybit:BEAT")


async def test_close_paper_writes_journal_when_trade_id_exists() -> None:
    rdb = _rdb()
    rdb.get = AsyncMock(return_value=b"42")
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"
    pos = {"base": "BEAT", "exchange": "bybit", "entry_price": 0.0030, "side": "short"}

    with (
        patch(
            "schurfer_execution.paper.journal.close_trade",
            new_callable=AsyncMock,
            return_value=journal.CloseOutcome(committed=True),
        ) as mock_jrn,
        patch(
            "schurfer_execution.paper.journal.delete_trade_id_if_matches",
            new_callable=AsyncMock,
        ) as mock_cas_delete,
    ):
        await close_paper(rdb, pos=pos, current_price=0.0025, reason="take_profit", cfg=cfg)

    mock_jrn.assert_called_once()
    kw = mock_jrn.call_args.kwargs
    assert kw["trade_id"] == 42
    assert kw["exit_price"] == 0.0025
    assert kw["reason"] == "take_profit"
    # entry_price/side are no longer caller-supplied — close_trade loads them
    # from the trade's own DB row.
    assert "entry_price" not in kw
    assert "side" not in kw
    mock_cas_delete.assert_called_once_with(rdb, "trade:id:paper:bybit:BEAT", 42)


async def test_close_paper_pnl_computed_correctly_for_short() -> None:
    # Entry 0.003, exit 0.0025 → 16.67% profit for short
    rdb = _rdb()
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"
    rdb.get = AsyncMock(return_value=b"7")
    pos = {"base": "SOL", "exchange": "bingx", "entry_price": 0.003, "side": "short"}

    with (
        patch("schurfer_execution.paper.journal.close_trade", new_callable=AsyncMock) as mock_jrn,
        patch(
            "schurfer_execution.paper.journal.delete_trade_id_if_matches", new_callable=AsyncMock
        ),
    ):
        await close_paper(rdb, pos=pos, current_price=0.0025, reason="take_profit", cfg=cfg)

    kw = mock_jrn.call_args.kwargs
    # pnl computed inside close_paper and forwarded to journal
    assert kw["exit_price"] == 0.0025


async def test_close_paper_does_not_write_journal_without_db_url() -> None:
    rdb = _rdb()
    rdb.get = AsyncMock(return_value=b"42")
    pos = {"base": "BEAT", "exchange": "bybit", "entry_price": 0.003, "side": "short"}

    with patch("schurfer_execution.paper.journal.close_trade", new_callable=AsyncMock) as mock_jrn:
        await close_paper(rdb, pos=pos, current_price=0.0025, reason="stop_loss", cfg=_cfg())

    mock_jrn.assert_not_called()


async def test_close_paper_notification_uses_the_same_accounting_close_trade_persisted() -> None:
    # Regression: DB row and Telegram notification must show identical
    # numbers. close_paper must report journal.close_trade's own computed
    # CloseOutcome verbatim, never recompute PnL a second time itself (the
    # old code ran calculate_performance twice -- once here from a
    # Redis-cached pos, once inside close_trade from the DB row -- which
    # could silently diverge).
    rdb = _rdb()
    rdb.get = AsyncMock(return_value=b"42")
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"
    pos = {
        "base": "BEAT",
        "exchange": "bybit",
        "entry_price": 100,
        "size_usd": 100,
        "leverage": 5,
        "side": "short",
    }
    outcome = journal.CloseOutcome(
        committed=True,
        gross_pnl_usd=10.0,
        gross_pnl_pct=10.0,
        net_pnl_usd=9.73,
        net_pnl_pct=9.73,
        fees_usd=0.2,
        funding_usd=0.02,
        slippage_usd=0.07,
        accounting_status="complete",
    )

    with (
        patch(
            "schurfer_execution.paper.journal.close_trade",
            new_callable=AsyncMock,
            return_value=outcome,
        ),
        patch(
            "schurfer_execution.paper.journal.delete_trade_id_if_matches", new_callable=AsyncMock
        ),
        patch("schurfer_execution.paper.notify.credentials", return_value=("test", "chat")),
        patch(
            "schurfer_execution.paper.notify.notify_close", new_callable=AsyncMock
        ) as close_notice,
    ):
        await close_paper(rdb, pos=pos, current_price=90, reason="take_profit", cfg=cfg)

    kw = close_notice.call_args.kwargs
    assert kw["pnl_pct"] == 9.73
    assert kw["pnl_usd"] == 9.73
    assert kw["pnl_kind"] == "modeled_net"
    assert kw["gross_pnl_pct"] == 10.0
    assert kw["accounting_status"] == "complete"
    assert kw["fees_usd"] == 0.2
    assert kw["funding_usd"] == 0.02
    assert kw["slippage_usd"] == 0.07
    assert kw["margin_usd"] == pytest.approx(20.0)


async def test_close_paper_notification_falls_back_when_outcome_has_no_fresh_accounting() -> None:
    # The idempotent already-closed retry path returns a committed CloseOutcome
    # with no accounting fields at all. The notification must fall back to the
    # pre-computed pos-based estimate, not silently drop to a bare None dollar
    # figure while still showing a real percent.
    rdb = _rdb()
    rdb.get = AsyncMock(return_value=b"42")
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"
    pos = {
        "base": "BEAT",
        "exchange": "bybit",
        "entry_price": 100,
        "size_usd": 50,
        "side": "short",
    }

    with (
        patch(
            "schurfer_execution.paper.journal.close_trade",
            new_callable=AsyncMock,
            return_value=journal.CloseOutcome(committed=True),
        ),
        patch(
            "schurfer_execution.paper.journal.delete_trade_id_if_matches", new_callable=AsyncMock
        ),
        patch("schurfer_execution.paper.notify.credentials", return_value=("test", "chat")),
        patch(
            "schurfer_execution.paper.notify.notify_close", new_callable=AsyncMock
        ) as close_notice,
    ):
        await close_paper(rdb, pos=pos, current_price=90, reason="take_profit", cfg=cfg)

    kw = close_notice.call_args.kwargs
    # short, entry 100 -> exit 90 = +10% gross, on $50 size = $5.
    assert kw["pnl_pct"] == pytest.approx(10.0)
    assert kw["pnl_usd"] == pytest.approx(5.0)


async def test_close_paper_notification_pnl_usd_is_none_when_size_usd_missing() -> None:
    # A position stored before size tracking existed. Must show percent
    # alone, never fabricate a dollar figure from a missing size.
    rdb = _rdb()
    cfg = _cfg()
    pos = {"base": "BEAT", "exchange": "bybit", "entry_price": 0.003, "side": "short"}

    with (
        patch("schurfer_execution.paper.notify.credentials", return_value=("test", "chat")),
        patch(
            "schurfer_execution.paper.notify.notify_close", new_callable=AsyncMock
        ) as close_notice,
    ):
        await close_paper(rdb, pos=pos, current_price=0.0025, reason="take_profit", cfg=cfg)

    assert close_notice.call_args.kwargs["pnl_usd"] is None


async def test_close_paper_notification_computes_gross_pnl_usd_without_full_accounting() -> None:
    # size_usd present, but no (or non-matching) accounting_version -> the
    # "legacy"/gross path. The dollar figure must match the same raw,
    # unmodeled percent already labeled "Gross PnL" -- not a mix of a net
    # percent with a gross dollar amount or vice versa.
    rdb = _rdb()
    cfg = _cfg()
    pos = {
        "base": "BEAT",
        "exchange": "bybit",
        "entry_price": 0.0030,
        "size_usd": 50.0,
        "side": "short",
    }

    with (
        patch("schurfer_execution.paper.notify.credentials", return_value=("test", "chat")),
        patch(
            "schurfer_execution.paper.notify.notify_close", new_callable=AsyncMock
        ) as close_notice,
    ):
        await close_paper(rdb, pos=pos, current_price=0.0025, reason="take_profit", cfg=cfg)

    kw = close_notice.call_args.kwargs
    assert kw["pnl_kind"] == "gross"
    # (0.0030 - 0.0025) / 0.0030 * 100 = 16.666...% gross, on $50.
    assert kw["pnl_usd"] == pytest.approx(50.0 * kw["pnl_pct"] / 100, abs=0.01)


async def test_close_paper_journal_failure_keeps_trade_id() -> None:
    """A failed paper-trade journal write must not delete the trade-id
    pointer — best-effort retry stays possible, without polluting the
    journal:pending_close namespace that real-trade risk gating depends on."""
    rdb = _rdb()
    rdb.get = AsyncMock(return_value=b"42")
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"
    pos = {"base": "BEAT", "exchange": "bybit", "entry_price": 0.003, "side": "short"}

    with (
        patch(
            "schurfer_execution.paper.journal.close_trade",
            new_callable=AsyncMock,
            return_value=journal.CloseOutcome(committed=False),
        ),
        patch(
            "schurfer_execution.paper.journal.delete_trade_id_if_matches",
            new_callable=AsyncMock,
        ) as mock_cas_delete,
    ):
        await close_paper(rdb, pos=pos, current_price=0.0025, reason="stop_loss", cfg=cfg)

    mock_cas_delete.assert_not_called()


async def test_close_paper_journal_failure_leaves_position_tracked_for_retry() -> None:
    """Regression (colleague review): a DB outage at close time must not
    silently orphan the trade -- the Redis position key must stay in place
    (so the next monitor tick retries the close) instead of vanishing while
    the DB row is permanently stuck 'open'. No 'closed' notification either,
    since the position was never actually closed."""
    rdb = _rdb()
    rdb.get = AsyncMock(return_value=b"42")
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"
    cfg.telegram_bot_token = "tok"  # noqa: S105
    cfg.telegram_chat_id = "123"
    pos = {"base": "BEAT", "exchange": "bybit", "entry_price": 0.003, "side": "short"}

    with (
        patch(
            "schurfer_execution.paper.journal.close_trade",
            new_callable=AsyncMock,
            return_value=journal.CloseOutcome(committed=False),
        ),
        patch(
            "schurfer_execution.paper.journal.delete_trade_id_if_matches",
            new_callable=AsyncMock,
        ) as mock_cas_delete,
        patch(
            "schurfer_execution.paper.notify.notify_close", new_callable=AsyncMock
        ) as close_notice,
    ):
        await close_paper(rdb, pos=pos, current_price=0.0025, reason="stop_loss", cfg=cfg)

    mock_cas_delete.assert_not_called()
    rdb.delete.assert_not_called()
    close_notice.assert_not_awaited()


async def test_close_paper_records_fresh_buy_to_close_quote() -> None:
    # SHORT exit buys to close -- prices off the ask side.
    rdb = _rdb()
    rdb.get = AsyncMock(return_value=b"42")
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"
    pos = {
        "base": "BEAT",
        "symbol": "BEAT/USDT:USDT",
        "exchange": "bybit",
        "entry_price": 100,
        "size_usd": 50,
        "side": "short",
    }
    ex = AsyncMock()
    ex.markets = {
        "BEAT/USDT:USDT": {
            "id": "BEATUSDT",
            "contract": True,
            "contractSize": 1,
        }
    }
    ex.fetch_order_book = AsyncMock(
        return_value={
            "bids": [[99.9, 10]],
            "asks": [[100.1, 10]],
        }
    )

    with (
        patch(
            "schurfer_execution.paper.journal.close_trade",
            new_callable=AsyncMock,
            return_value=journal.CloseOutcome(committed=True),
        ) as close_trade,
        patch(
            "schurfer_execution.paper.journal.delete_trade_id_if_matches",
            new_callable=AsyncMock,
        ),
    ):
        await close_paper(
            rdb,
            pos=pos,
            current_price=100,
            reason="max_hold",
            cfg=cfg,
            exchange_client=ex,
        )

    kw = close_trade.call_args.kwargs
    assert kw["trade_id"] == 42
    observation = kw["exit_observation"]
    assert observation["status"] == "sampled"
    assert observation["exchange"] == "bybit"
    assert observation["symbol"] == "BEAT/USDT:USDT"
    assert observation["market_id"] == "BEATUSDT"
    assert observation["requested_notional_usd"] == 50
    assert observation["filled_notional_usd"] == 50
    assert observation["best_bid"] == 99.9
    assert observation["best_ask"] == 100.1
    assert observation["ask_vwap"] == 100.1
    assert observation["ask_impact_bps"] == 10
    # Recorded for reference even though this SHORT exit doesn't price off it.
    assert observation["bid_vwap"] == pytest.approx(99.9)
    # Exit is booked at the exit-side (ask, for a SHORT) VWAP -- the cost is
    # already inside that price, so no separate slippage charge on top.
    assert kw["exit_price"] == pytest.approx(100.1)
    assert kw["fresh_exit_slippage_bps"] == 0.0


async def test_close_paper_long_exit_prices_off_the_bid() -> None:
    # LONG exit sells to close -- prices off the bid side, the mirror image
    # of the SHORT case above.
    rdb = _rdb()
    rdb.get = AsyncMock(return_value=b"42")
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"
    pos = {
        "base": "BEAT",
        "symbol": "BEAT/USDT:USDT",
        "exchange": "bybit",
        "entry_price": 100,
        "size_usd": 50,
        "side": "long",
    }
    ex = AsyncMock()
    ex.markets = {"BEAT/USDT:USDT": {"id": "BEATUSDT", "contract": True, "contractSize": 1}}
    ex.fetch_order_book = AsyncMock(
        return_value={
            "bids": [[99.9, 10]],
            "asks": [[100.1, 10]],
        }
    )

    with (
        patch(
            "schurfer_execution.paper.journal.close_trade",
            new_callable=AsyncMock,
            return_value=journal.CloseOutcome(committed=True),
        ) as close_trade,
        patch(
            "schurfer_execution.paper.journal.delete_trade_id_if_matches",
            new_callable=AsyncMock,
        ),
    ):
        await close_paper(
            rdb, pos=pos, current_price=100, reason="max_hold", cfg=cfg, exchange_client=ex
        )

    kw = close_trade.call_args.kwargs
    observation = kw["exit_observation"]
    assert observation["status"] == "sampled"
    assert observation["bid_vwap"] == pytest.approx(99.9)
    assert observation["bid_impact_bps"] == 10
    assert observation["filled_notional_usd"] == 50
    # Exit is booked at the exit-side (bid, for a LONG) VWAP -- the cost is
    # already inside that price, so no separate slippage charge on top.
    assert kw["exit_price"] == pytest.approx(99.9)
    assert kw["fresh_exit_slippage_bps"] == 0.0


async def test_close_paper_persists_quote_failure_without_blocking_close() -> None:
    rdb = _rdb()
    rdb.get = AsyncMock(return_value=b"42")
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"
    pos = {
        "base": "BEAT",
        "symbol": "BEAT/USDT:USDT",
        "exchange": "bybit",
        "entry_price": 100,
        "size_usd": 50,
        "side": "short",
    }
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(side_effect=RuntimeError("venue unavailable"))

    with (
        patch(
            "schurfer_execution.paper.journal.close_trade",
            new_callable=AsyncMock,
            return_value=journal.CloseOutcome(committed=True),
        ) as close_trade,
        patch(
            "schurfer_execution.paper.journal.delete_trade_id_if_matches",
            new_callable=AsyncMock,
        ) as delete_trade_id,
    ):
        await close_paper(
            rdb,
            pos=pos,
            current_price=100,
            reason="stop_loss",
            cfg=cfg,
            exchange_client=ex,
        )

    close_trade.assert_awaited_once()
    delete_trade_id.assert_awaited_once()
    kw = close_trade.call_args.kwargs
    observation = kw["exit_observation"]
    assert observation["status"] == "fetch_failed"
    assert observation["ask_impact_bps"] is None
    assert observation["error"] == "RuntimeError: venue unavailable"
    assert kw["fresh_exit_slippage_bps"] is None


async def test_close_paper_capture_exception_does_not_block_close_or_atomicity() -> None:
    # If _capture_exit_liquidity itself blows up (not just a failed/
    # insufficient exchange reading, which it already handles internally),
    # the close must still proceed with no exit evidence rather than
    # getting stuck open.
    rdb = _rdb()
    rdb.get = AsyncMock(return_value=b"42")
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"
    pos = {
        "base": "BEAT",
        "symbol": "BEAT/USDT:USDT",
        "exchange": "bybit",
        "entry_price": 100,
        "size_usd": 50,
        "side": "short",
    }
    ex = AsyncMock()

    with (
        patch(
            "schurfer_execution.paper._capture_exit_liquidity",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
        patch(
            "schurfer_execution.paper.journal.close_trade",
            new_callable=AsyncMock,
            return_value=journal.CloseOutcome(committed=True),
        ) as close_trade,
        patch(
            "schurfer_execution.paper.journal.delete_trade_id_if_matches",
            new_callable=AsyncMock,
        ) as delete_trade_id,
    ):
        await close_paper(
            rdb, pos=pos, current_price=100, reason="max_hold", cfg=cfg, exchange_client=ex
        )

    rdb.delete.assert_any_call("position:paper:bybit:BEAT")
    delete_trade_id.assert_awaited_once()
    kw = close_trade.call_args.kwargs
    assert kw["exit_observation"] is None
    assert kw["fresh_exit_slippage_bps"] is None


async def test_close_paper_labels_insufficient_buy_to_close_depth() -> None:
    rdb = _rdb()
    rdb.get = AsyncMock(return_value=b"42")
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"
    pos = {
        "base": "BEAT",
        "symbol": "BEAT/USDT:USDT",
        "exchange": "bybit",
        "entry_price": 100,
        "size_usd": 50,
        "side": "short",
    }
    ex = AsyncMock()
    ex.fetch_order_book = AsyncMock(
        return_value={
            "bids": [[99.9, 10]],
            "asks": [[100.1, 0.1]],
        }
    )

    with (
        patch(
            "schurfer_execution.paper.journal.close_trade",
            new_callable=AsyncMock,
            return_value=journal.CloseOutcome(committed=True),
        ) as close_trade,
        patch(
            "schurfer_execution.paper.journal.delete_trade_id_if_matches",
            new_callable=AsyncMock,
        ),
    ):
        await close_paper(
            rdb,
            pos=pos,
            current_price=100,
            reason="max_hold",
            cfg=cfg,
            exchange_client=ex,
        )

    kw = close_trade.call_args.kwargs
    observation = kw["exit_observation"]
    assert observation["status"] == "insufficient_ask_depth"
    assert observation["filled_notional_usd"] == 10.01
    assert observation["ask_vwap"] is None
    assert observation["ask_impact_bps"] is None
    assert kw["fresh_exit_slippage_bps"] is None


async def test_paper_tick_passes_same_market_client_to_close_capture() -> None:
    pos = {
        "base": "BEAT",
        "exchange": "bybit",
        "entry_price": 100,
        "size_usd": 50,
        "side": "short",
        "opened_at": 1_000,
        "exit_params": {},
    }
    key = b"position:paper:bybit:BEAT"
    rdb = _rdb()

    async def _scan_iter(_pattern: str) -> object:  # type: ignore[type-arg]
        yield key

    rdb.scan_iter = _scan_iter
    rdb.get = AsyncMock(return_value=json.dumps(pos).encode())
    ex = AsyncMock()
    ex.id = "bybit"
    ex.markets = {
        "BEAT/USDT:USDT": {
            "id": "BEATUSDT",
            "symbol": "BEAT/USDT:USDT",
            "base": "BEAT",
            "quote": "USDT",
            "settle": "USDT",
            "type": "swap",
            "active": True,
        }
    }
    ex.fetch_ticker = AsyncMock(return_value={"last": 90})

    with (
        patch(
            "schurfer_execution.paper.exit_module.check_exit",
            new_callable=AsyncMock,
            return_value="take_profit",
        ),
        patch("schurfer_execution.paper.close_paper", new_callable=AsyncMock) as close,
    ):
        await _tick({"bybit": ex}, rdb, _cfg())

    assert close.call_args.kwargs["exchange_client"] is ex
    assert close.call_args.kwargs["pos"]["symbol"] == "BEAT/USDT:USDT"
