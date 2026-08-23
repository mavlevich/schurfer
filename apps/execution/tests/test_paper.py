"""Tests for paper.py — paper trading open/close via Redis."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_execution import journal
from schurfer_execution.paper import (
    _display_strategy,
    _tick,
    close_paper,
    open_paper,
    open_paper_for_episode,
    paper_key,
    reconcile_missing_positions,
    release_reservation,
    reservation_key,
    reserve_position,
)
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


# --- _display_strategy (colleague review) ---
#
# Regression for a production bug: paper.py used to read setup_context.get(
# "strategy", "unknown") directly, which only ever found a value for
# early_momentum/liquidation_cascade (they set "strategy" themselves). Every
# pump_short paper trade -- which only ever sets "strategy_version", never
# "strategy" -- showed "Strategy: unknown" in every Telegram open/close
# message (verified against production, 2026-08-23: 100% of ~200 pump_short
# messages). journal.strategy_identity() already parses all three
# conventions correctly; these lock in that _display_strategy actually uses
# it instead of the single-key lookup it replaced.


def test_display_strategy_resolves_pump_shorts_bare_strategy_version() -> None:
    assert (
        _display_strategy({"strategy_version": "1_market_quality"})
        == "pump_short v1_market_quality"
    )


def test_display_strategy_resolves_combined_name_vn() -> None:
    assert _display_strategy({"strategy": "early_momentum_v4"}) == "early_momentum v4"
    assert _display_strategy({"strategy": "liquidation_cascade_v2"}) == "liquidation_cascade v2"


def test_display_strategy_defaults_to_pump_short_v1_with_no_identity_at_all() -> None:
    # journal.strategy_identity()'s own module-level default -- matches
    # pump_short's own historical convention, not an arbitrary "unknown".
    assert _display_strategy({"pump_pct": 45.0}) == "pump_short v1"


def test_display_strategy_falls_back_to_unknown_on_malformed_identity() -> None:
    # "strategy" present but missing the required "_vN" marker anywhere --
    # strategy_identity() raises ValueError; must not crash notification,
    # only this one row's display degrades.
    assert _display_strategy({"strategy": "noversionmarkerhere"}) == "unknown"


async def test_open_paper_telegram_shows_real_strategy_for_pump_short_context() -> None:
    """End-to-end regression: a pump_short-shaped setup_context (bare
    strategy_version, the real production shape) must reach notify_open with
    a real strategy label, not "unknown"."""
    rdb = _rdb()
    cfg = _cfg()
    cfg.telegram_bot_token = "tok"  # noqa: S105
    cfg.telegram_chat_id = "123"

    with patch(
        "schurfer_execution.paper.notify.notify_open", new_callable=AsyncMock
    ) as notify_open:
        await open_paper(
            rdb,
            instrument=_instrument(),
            price=0.0025,
            size_usd=50.0,
            leverage=3,
            score=8,
            setup_context={"pump_pct": 45.0, "strategy_version": "1_market_quality"},
            cfg=cfg,
        )

    notify_open.assert_awaited_once()
    assert notify_open.await_args.kwargs["strategy"] == "pump_short v1_market_quality"


# --- paper_key ---


def test_paper_key_format() -> None:
    assert paper_key("bybit", "beat") == "position:paper:bybit:BEAT"
    assert paper_key("bingx", "SOL") == "position:paper:bingx:SOL"


# --- reservation (separate namespace from the real position key) ---


def test_reservation_key_is_a_separate_namespace_from_the_real_position_key() -> None:
    key = reservation_key("bybit", "beat")
    assert key == "position:paper:reservation:bybit:BEAT"
    assert not key.startswith(paper_key("bybit", "beat"))
    assert paper_key("bybit", "beat") not in key.split("reservation:")[0]


async def test_reserve_position_acquires_atomically_via_lua() -> None:
    rdb = _rdb()
    rdb.eval = AsyncMock(return_value=1)
    acquired = await reserve_position(rdb, exchange="bybit", base="BEAT", token="tok-1")  # noqa: S106
    assert acquired is True
    rdb.eval.assert_awaited_once()
    script, numkeys, position_key, reservation_key_arg, token, ttl = rdb.eval.call_args.args
    assert "exists" in script.lower()
    assert numkeys == 2
    assert position_key == "position:paper:bybit:BEAT"
    assert reservation_key_arg == "position:paper:reservation:bybit:BEAT"
    assert token == "tok-1"  # noqa: S105
    assert ttl == 30


async def test_reserve_position_fails_when_already_reserved() -> None:
    rdb = _rdb()
    rdb.eval = AsyncMock(return_value=0)  # NX conflict inside the Lua script
    acquired = await reserve_position(rdb, exchange="bybit", base="BEAT", token="tok-2")  # noqa: S106
    assert acquired is False


async def test_reserve_position_fails_when_real_position_already_exists() -> None:
    """Regression (colleague review): a legacy Redis-only position from a
    different/older flow that never used the reservation namespace must
    still block a new reservation on the same instrument, not just get
    silently clobbered once the quote/open path finishes."""
    rdb = _rdb()
    rdb.eval = AsyncMock(return_value=0)  # exists-check short-circuits inside the script
    acquired = await reserve_position(rdb, exchange="bybit", base="BEAT", token="tok-3")  # noqa: S106
    assert acquired is False
    script = rdb.eval.call_args.args[0]
    assert "exists" in script.lower()


async def test_release_reservation_is_cas_by_token_never_unconditional_delete() -> None:
    rdb = _rdb()
    rdb.eval = AsyncMock(return_value=1)
    released = await release_reservation(rdb, exchange="bybit", base="BEAT", token="tok-1")  # noqa: S106
    assert released is True
    rdb.eval.assert_awaited_once()
    script, numkeys, key, token = rdb.eval.call_args.args
    assert "get" in script.lower()
    assert numkeys == 1
    assert key == "position:paper:reservation:bybit:BEAT"
    assert token == "tok-1"  # noqa: S105
    rdb.delete.assert_not_called()


async def test_release_reservation_fails_when_token_does_not_match() -> None:
    # A stale/expired caller's release must never remove a different (newer)
    # reservation that has since taken the same key.
    rdb = _rdb()
    rdb.eval = AsyncMock(return_value=0)
    released = await release_reservation(rdb, exchange="bybit", base="BEAT", token="stale-tok")  # noqa: S106
    assert released is False


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


# --- open_paper_for_episode ---


async def test_open_paper_for_episode_writes_position_with_episode_id() -> None:
    rdb = _rdb()
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"
    outcome_value = journal.OpenTradeOutcome(
        trade_id=42, created=True, recovered=False, claim_valid=True
    )

    with patch(
        "schurfer_execution.paper.journal.open_trade_for_episode",
        new_callable=AsyncMock,
        return_value=outcome_value,
    ) as mock_jrn:
        outcome = await open_paper_for_episode(
            rdb,
            instrument=_instrument(),
            price=1.0,
            size_usd=100.0,
            leverage=5,
            score=100,
            setup_context={"strategy": "early_momentum_v3"},
            cfg=cfg,
            side="long",
            exit_params={"initial_sl_pct": 10.0},
            episode_id="e1",
            claim_token="tok-1",  # noqa: S106
            entry_idempotency_key="e1:entry:base",
        )

    assert outcome == outcome_value
    mock_jrn.assert_called_once()
    kw = mock_jrn.call_args.kwargs
    assert kw["episode_id"] == "e1"
    assert kw["claim_token"] == "tok-1"  # noqa: S105
    assert kw["entry_idempotency_key"] == "e1:entry:base"

    position_calls = [c for c in rdb.set.call_args_list if c.args[0] == "position:paper:bybit:BEAT"]
    assert len(position_calls) == 1
    stored = json.loads(position_calls[0].args[1])
    assert stored["episode_id"] == "e1"
    assert stored["side"] == "long"
    # trade_id is embedded directly in the position payload -- close_paper's
    # primary lookup no longer depends on the separate trade:id:paper:* key
    # surviving a crash between the two rdb.set calls (colleague review).
    assert stored["trade_id"] == 42
    rdb.set.assert_any_call("trade:id:paper:bybit:BEAT", "42", ex=86400 * 7)


async def test_open_paper_for_episode_skips_redis_write_when_trade_id_is_none() -> None:
    """A claim-invalid or idempotency-key-collision outcome must never write
    a position under the real key -- there's nothing to track."""
    rdb = _rdb()
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"
    outcome_value = journal.OpenTradeOutcome(
        trade_id=None, created=False, recovered=False, claim_valid=False
    )

    with patch(
        "schurfer_execution.paper.journal.open_trade_for_episode",
        new_callable=AsyncMock,
        return_value=outcome_value,
    ):
        outcome = await open_paper_for_episode(
            rdb,
            instrument=_instrument(),
            price=1.0,
            size_usd=100.0,
            leverage=5,
            score=100,
            setup_context={"strategy": "early_momentum_v3"},
            cfg=cfg,
            side="long",
            exit_params={"initial_sl_pct": 10.0},
            episode_id="e1",
            claim_token="tok-1",  # noqa: S106
            entry_idempotency_key="e1:entry:base",
        )

    assert outcome.trade_id is None
    rdb.set.assert_not_called()


async def test_open_paper_for_episode_requires_db_url() -> None:
    rdb = _rdb()
    with pytest.raises(ValueError, match="db_url"):
        await open_paper_for_episode(
            rdb,
            instrument=_instrument(),
            price=1.0,
            size_usd=100.0,
            leverage=5,
            score=100,
            setup_context={},
            cfg=_cfg(),
            side="long",
            exit_params={},
            episode_id="e1",
            claim_token="tok-1",  # noqa: S106
            entry_idempotency_key="e1:entry:base",
        )


# --- reconcile_missing_positions ---


def _open_episode_trade(**overrides: object) -> journal.OpenEpisodeTrade:
    import datetime as dt

    fields: dict[str, object] = {
        "trade_id": 42,
        "symbol": "BEAT/USDT:USDT",
        "exchange": "bybit",
        "side": "long",
        "entry_price": 1.0,
        "size_usd": 100.0,
        "leverage": 5,
        "entry_at": dt.datetime(2026, 8, 22, tzinfo=dt.UTC),
        "entry_slippage_bps": 0.0,
        "exit_slippage_bps": None,
        "accounting_version": PAPER_ACCOUNTING_VERSION,
        "setup_context": {"strategy": "early_momentum_v3", "exit_params": {"initial_sl_pct": 10.0}},
        "episode_id": "e1",
    }
    fields.update(overrides)
    return journal.OpenEpisodeTrade(**fields)  # type: ignore[arg-type]


def _rdb_exists(*, position: bool, trade_id_key: bool) -> AsyncMock:
    """rdb.exists keyed by whether the call is for the position key or the
    standalone trade-id key -- lets tests exercise the two independent
    failure points separately."""

    async def _exists(key: str) -> bool:
        return trade_id_key if key.startswith("trade:id:paper:") else position

    return AsyncMock(side_effect=_exists)


async def test_reconcile_missing_positions_rebuilds_missing_key_from_the_trade_row() -> None:
    rdb = _rdb()
    rdb.exists = _rdb_exists(position=False, trade_id_key=False)  # both genuinely missing
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"
    trade = _open_episode_trade()

    with patch(
        "schurfer_execution.paper.journal.find_open_episode_trades",
        AsyncMock(return_value=[trade]),
    ):
        repaired = await reconcile_missing_positions(rdb, cfg)

    assert repaired == 1
    position_calls = [c for c in rdb.set.call_args_list if c.args[0] == "position:paper:bybit:BEAT"]
    assert len(position_calls) == 1
    stored = json.loads(position_calls[0].args[1])
    assert stored["episode_id"] == "e1"
    assert stored["entry_price"] == 1.0
    assert stored["exit_params"] == {"initial_sl_pct": 10.0}
    assert stored["trade_id"] == 42
    rdb.set.assert_any_call("trade:id:paper:bybit:BEAT", "42", ex=86400 * 7)


async def test_reconcile_missing_positions_errors_and_skips_when_exit_params_missing() -> None:
    """A trade whose setup_context never got exit_params persisted (an
    older/malformed row) must be skipped, never silently rebuilt with
    whatever the code's current exit_params constant happens to be."""
    rdb = _rdb()
    rdb.exists = _rdb_exists(position=False, trade_id_key=False)
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"
    trade = _open_episode_trade(setup_context={"strategy": "early_momentum_v3"})

    with patch(
        "schurfer_execution.paper.journal.find_open_episode_trades",
        AsyncMock(return_value=[trade]),
    ):
        repaired = await reconcile_missing_positions(rdb, cfg)

    assert repaired == 0
    rdb.set.assert_not_called()


async def test_reconcile_missing_positions_skips_when_both_keys_already_present() -> None:
    rdb = _rdb()
    rdb.exists = _rdb_exists(position=True, trade_id_key=True)  # nothing to repair
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"

    with patch(
        "schurfer_execution.paper.journal.find_open_episode_trades",
        AsyncMock(return_value=[_open_episode_trade()]),
    ):
        repaired = await reconcile_missing_positions(rdb, cfg)

    assert repaired == 0
    rdb.set.assert_not_called()


async def test_reconcile_missing_positions_repairs_trade_id_key_independently() -> None:
    """Crash-window regression (colleague review): the position rdb.set
    succeeds but the process dies before the trade-id rdb.set runs. Must be
    repaired on its own -- not skipped just because the position key is
    already present -- so close_paper's legacy fallback path can still find
    it even for a position payload written before trade_id was embedded."""
    rdb = _rdb()
    rdb.exists = _rdb_exists(position=True, trade_id_key=False)
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"
    trade = _open_episode_trade()

    with patch(
        "schurfer_execution.paper.journal.find_open_episode_trades",
        AsyncMock(return_value=[trade]),
    ):
        repaired = await reconcile_missing_positions(rdb, cfg)

    assert repaired == 1
    # The already-present position key must be left untouched -- only the
    # missing trade-id key gets written.
    assert all(c.args[0] != "position:paper:bybit:BEAT" for c in rdb.set.call_args_list)
    rdb.set.assert_called_once_with("trade:id:paper:bybit:BEAT", "42", ex=86400 * 7)


async def test_reconcile_missing_positions_noop_without_db_url() -> None:
    rdb = _rdb()
    with patch(
        "schurfer_execution.paper.journal.find_open_episode_trades", new_callable=AsyncMock
    ) as find:
        repaired = await reconcile_missing_positions(rdb, _cfg())

    assert repaired == 0
    find.assert_not_awaited()


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


async def test_close_paper_prefers_trade_id_embedded_in_position_over_the_redis_key() -> None:
    """The embedded trade_id must win even when the standalone key is
    missing/stale -- it can't be separated from the position by a crash
    the way the standalone key's own rdb.set can (colleague review)."""
    rdb = _rdb()
    rdb.get = AsyncMock(return_value=None)  # standalone trade:id:* key never written
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"
    pos = {
        "base": "BEAT",
        "exchange": "bybit",
        "entry_price": 0.0030,
        "side": "short",
        "trade_id": 42,
    }

    with (
        patch(
            "schurfer_execution.paper.journal.close_trade",
            new_callable=AsyncMock,
            return_value=journal.CloseOutcome(committed=True),
        ) as mock_jrn,
        patch(
            "schurfer_execution.paper.journal.delete_trade_id_if_matches",
            new_callable=AsyncMock,
        ),
    ):
        await close_paper(rdb, pos=pos, current_price=0.0025, reason="take_profit", cfg=cfg)

    mock_jrn.assert_called_once()
    assert mock_jrn.call_args.kwargs["trade_id"] == 42
    rdb.get.assert_not_called()


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


async def test_close_paper_with_episode_id_cas_deletes_and_marks_episode_closed() -> None:
    rdb = _rdb()
    rdb.get = AsyncMock(return_value=None)  # no trade_id pointer -> no DB commit path
    rdb.eval = AsyncMock(return_value=1)
    cfg = _cfg()
    cfg.db_url = "postgresql://localhost/test"
    pos = {
        "base": "BEAT",
        "exchange": "bybit",
        "entry_price": 100,
        "side": "long",
        "episode_id": "e1",
    }

    with patch(
        "schurfer_execution.paper.episodes.mark_closed", new_callable=AsyncMock
    ) as mark_closed:
        await close_paper(rdb, pos=pos, current_price=90, reason="stop_loss", cfg=cfg)

    # CAS-scoped, never the plain unconditional delete.
    rdb.delete.assert_not_called()
    rdb.eval.assert_awaited_once()
    script, numkeys, key, episode_id = rdb.eval.call_args.args
    assert "cjson.decode" in script
    assert numkeys == 1
    assert key == "position:paper:bybit:BEAT"
    assert episode_id == "e1"
    mark_closed.assert_awaited_once_with(cfg.db_url, episode_id="e1")


async def test_close_paper_without_episode_id_uses_plain_delete() -> None:
    """v1/v2 legacy positions carry no episode_id -- unchanged behavior."""
    rdb = _rdb()
    rdb.get = AsyncMock(return_value=None)
    cfg = _cfg()
    pos = {"base": "BEAT", "exchange": "bybit", "entry_price": 100, "side": "short"}

    await close_paper(rdb, pos=pos, current_price=90, reason="stop_loss", cfg=cfg)

    rdb.delete.assert_any_call("position:paper:bybit:BEAT")
    rdb.eval.assert_not_called()


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
