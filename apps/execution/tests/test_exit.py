"""Tests for exit.py — 3-phase dynamic exit logic."""

import time
from unittest.mock import AsyncMock, MagicMock

from schurfer_execution.exit import best_price_key, check_exit, exit_params, params_key

# --- exit_params ---


def test_exit_params_small_pump() -> None:
    p = exit_params(30.0)
    assert p["initial_sl_pct"] == 8.0
    assert p["activation_pct"] == 8.0
    assert p["trail_pct"] == 12.0
    assert p["max_hold_min"] == 180.0


def test_exit_params_medium_pump() -> None:
    p = exit_params(75.0)
    assert p["initial_sl_pct"] == 10.0
    assert p["activation_pct"] == 12.0
    assert p["trail_pct"] == 15.0
    assert p["max_hold_min"] == 240.0


def test_exit_params_large_pump() -> None:
    p = exit_params(150.0)
    assert p["initial_sl_pct"] == 12.0
    assert p["activation_pct"] == 15.0
    assert p["trail_pct"] == 20.0
    assert p["max_hold_min"] == 360.0


def test_exit_params_none_defaults_to_medium() -> None:
    p = exit_params(None)
    assert p["initial_sl_pct"] == 10.0


def test_exit_params_boundary_50_is_medium() -> None:
    assert exit_params(50.0)["initial_sl_pct"] == 10.0


def test_exit_params_boundary_100_is_large() -> None:
    assert exit_params(100.0)["initial_sl_pct"] == 12.0


# --- key helpers ---


def test_best_price_key_real() -> None:
    assert best_price_key("bybit", "beat") == "exit:best:bybit:BEAT"


def test_best_price_key_paper() -> None:
    assert best_price_key("bybit", "beat", paper=True) == "exit:best:paper:bybit:BEAT"


def test_params_key() -> None:
    assert params_key("binance", "SOL") == "exit:params:binance:SOL"


# --- check_exit ---


def _rdb(*, best_price: str | None = None) -> MagicMock:
    rdb = MagicMock()
    rdb.get = AsyncMock(return_value=best_price.encode() if best_price else None)
    rdb.set = AsyncMock()
    return rdb


def _params(pump_pct: float = 50.0) -> dict:
    return exit_params(pump_pct)


# Phase 1: initial SL


async def test_initial_sl_triggers_when_price_rises_above_threshold() -> None:
    # Short entry=100, price=111 → move_pct=-11% (loss) > initial_sl=10%
    rdb = _rdb()
    reason = await check_exit(
        side="short",
        entry_price=100.0,
        current_price=111.0,
        opened_at=time.time(),
        params=_params(75.0),
        rdb=rdb,
        bp_key="exit:best:bybit:TEST",
    )
    assert reason is not None
    assert "initial_sl" in reason


async def test_no_close_when_loss_below_initial_sl_threshold() -> None:
    # Short entry=100, price=107 → move_pct=-7% (loss), initial_sl=10% → no close
    rdb = _rdb()
    reason = await check_exit(
        side="short",
        entry_price=100.0,
        current_price=107.0,
        opened_at=time.time(),
        params=_params(75.0),
        rdb=rdb,
        bp_key="exit:best:bybit:TEST",
    )
    assert reason is None


async def test_no_close_when_profit_below_activation_threshold() -> None:
    # Short entry=100, price=95 → move_pct=5% profit, activation=12% → no close, no activation
    rdb = _rdb()
    reason = await check_exit(
        side="short",
        entry_price=100.0,
        current_price=95.0,
        opened_at=time.time(),
        params=_params(75.0),
        rdb=rdb,
        bp_key="exit:best:bybit:TEST",
    )
    assert reason is None
    rdb.set.assert_not_called()


async def test_activation_sets_best_price_in_redis() -> None:
    # Short entry=100, price=85 → move_pct=15% ≥ activation=12% → activate trailing
    rdb = _rdb()
    reason = await check_exit(
        side="short",
        entry_price=100.0,
        current_price=85.0,
        opened_at=time.time(),
        params=_params(75.0),
        rdb=rdb,
        bp_key="exit:best:bybit:TEST",
    )
    assert reason is None
    rdb.set.assert_called_once()
    assert rdb.set.call_args.args[1] == "85.0"


# Phase 2/3: trailing stop


async def test_trailing_stop_triggers_when_price_bounces_past_trail() -> None:
    # Activated at best_price=80, trail=15% → trailing_sl=92, current=93 → close
    rdb = _rdb(best_price="80.0")
    reason = await check_exit(
        side="short",
        entry_price=100.0,
        current_price=93.0,
        opened_at=time.time(),
        params=_params(75.0),
        rdb=rdb,
        bp_key="exit:best:bybit:TEST",
    )
    assert reason is not None
    assert "trailing_stop" in reason


async def test_trailing_stop_not_triggered_below_trail_threshold() -> None:
    # best_price=80, trail=15% → trailing_sl=92, current=90 → no close
    rdb = _rdb(best_price="80.0")
    reason = await check_exit(
        side="short",
        entry_price=100.0,
        current_price=90.0,
        opened_at=time.time(),
        params=_params(75.0),
        rdb=rdb,
        bp_key="exit:best:bybit:TEST",
    )
    assert reason is None


async def test_best_price_updates_when_lower() -> None:
    # best_price=80, current=75 → update best_price to 75
    rdb = _rdb(best_price="80.0")
    await check_exit(
        side="short",
        entry_price=100.0,
        current_price=75.0,
        opened_at=time.time(),
        params=_params(75.0),
        rdb=rdb,
        bp_key="exit:best:bybit:TEST",
    )
    rdb.set.assert_called_once()
    assert rdb.set.call_args.args[1] == "75.0"


async def test_trail_tightens_after_time() -> None:
    # After tighten_after_min, trail_tighten_pct=10% applies instead of 15%
    # best_price=80, current=89 — with trail=15% this is 80*1.15=92 → no close
    # but with trail=10% this is 80*1.10=88 → close
    params = _params(75.0)
    old_time = time.time() - params["tighten_after_min"] * 60 - 1
    rdb = _rdb(best_price="80.0")
    reason = await check_exit(
        side="short",
        entry_price=100.0,
        current_price=89.0,
        opened_at=old_time,
        params=params,
        rdb=rdb,
        bp_key="exit:best:bybit:TEST",
    )
    assert reason is not None
    assert "trailing_stop" in reason


# max_hold


async def test_max_hold_triggers_regardless_of_phase() -> None:
    params = _params(75.0)
    old_time = time.time() - params["max_hold_min"] * 60 - 1
    rdb = _rdb()
    reason = await check_exit(
        side="short",
        entry_price=100.0,
        current_price=100.0,
        opened_at=old_time,
        params=params,
        rdb=rdb,
        bp_key="exit:best:bybit:TEST",
    )
    assert reason is not None
    assert "max_hold" in reason


async def test_max_hold_not_triggered_before_threshold() -> None:
    params = _params(75.0)
    recent_time = time.time() - 60  # 1 minute ago
    rdb = _rdb()
    reason = await check_exit(
        side="short",
        entry_price=100.0,
        current_price=100.0,
        opened_at=recent_time,
        params=params,
        rdb=rdb,
        bp_key="exit:best:bybit:TEST",
    )
    assert reason is None


async def test_long_position_trailing_stop() -> None:
    # Long: best_price=120, trail=12% → trailing_sl=120*0.88=105.6, current=104 → close
    rdb = _rdb(best_price="120.0")
    reason = await check_exit(
        side="long",
        entry_price=100.0,
        current_price=104.0,
        opened_at=time.time(),
        params=_params(30.0),
        rdb=rdb,
        bp_key="exit:best:bybit:TEST",
    )
    assert reason is not None
    assert "trailing_stop" in reason
