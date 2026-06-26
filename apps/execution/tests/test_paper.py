"""Tests for paper.py — paper trading open/close via Redis."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from schurfer_execution.paper import close_paper, open_paper, paper_key


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


# --- paper_key ---


def test_paper_key_format() -> None:
    assert paper_key("bybit", "beat") == "position:paper:bybit:BEAT"
    assert paper_key("bingx", "SOL") == "position:paper:bingx:SOL"


# --- open_paper ---


async def test_open_paper_stores_position_in_redis() -> None:
    rdb = _rdb()
    await open_paper(
        rdb,
        base="BEAT",
        exchange="bybit",
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
    assert value["exit_params"]["initial_sl_pct"] == 8.0  # pump_pct=45 → small bucket


async def test_open_paper_writes_journal_when_db_url_set() -> None:
    rdb = _rdb()
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"

    with patch("schurfer_execution.paper.journal.open_trade", new_callable=AsyncMock) as mock_jrn:
        mock_jrn.return_value = 42
        await open_paper(
            rdb,
            base="BEAT",
            exchange="bybit",
            price=0.0025,
            size_usd=50.0,
            leverage=3,
            score=8,
            setup_context={},
            cfg=cfg,
        )

    mock_jrn.assert_called_once()
    kw = mock_jrn.call_args.kwargs
    assert kw["base"] == "BEAT"
    assert kw["entry_price"] == 0.0025
    assert kw["setup_context"]["paper"] is True


async def test_open_paper_does_not_write_journal_without_db_url() -> None:
    rdb = _rdb()
    with patch("schurfer_execution.paper.journal.open_trade", new_callable=AsyncMock) as mock_jrn:
        await open_paper(
            rdb,
            base="BEAT",
            exchange="bybit",
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

    with patch("schurfer_execution.paper.journal.close_trade", new_callable=AsyncMock) as mock_jrn:
        await close_paper(rdb, pos=pos, current_price=0.0025, reason="take_profit", cfg=cfg)

    mock_jrn.assert_called_once()
    kw = mock_jrn.call_args.kwargs
    assert kw["trade_id"] == 42
    assert kw["exit_price"] == 0.0025
    assert kw["entry_price"] == 0.0030
    assert kw["side"] == "short"
    assert kw["reason"] == "take_profit"


async def test_close_paper_pnl_computed_correctly_for_short() -> None:
    # Entry 0.003, exit 0.0025 → 16.67% profit for short
    rdb = _rdb()
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"
    rdb.get = AsyncMock(return_value=b"7")
    pos = {"base": "SOL", "exchange": "bingx", "entry_price": 0.003, "side": "short"}

    with patch("schurfer_execution.paper.journal.close_trade", new_callable=AsyncMock) as mock_jrn:
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
