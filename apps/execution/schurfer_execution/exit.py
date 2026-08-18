"""Dynamic 3-phase exit logic: initial SL → trailing activation → trailing stop."""

from __future__ import annotations

import json
import time
from typing import Any

import structlog

log = structlog.get_logger()

_BEST_KEY = "exit:best:{exchange}:{base}"
_BEST_KEY_PAPER = "exit:best:paper:{exchange}:{base}"
_PARAMS_KEY = "exit:params:{exchange}:{base}"
_ENTRY_KEY = "position:entry:{exchange}:{base}"
_SIDE_KEY = "position:side:{exchange}:{base}"
_SIZE_USD_KEY = "position:size_usd:{exchange}:{base}"

_KEY_TTL = 86400 * 7


def exit_params(pump_pct: float | None) -> dict[str, float]:
    """Compute exit parameters from pump magnitude.

    Bigger pumps are more volatile and retrace deeper — they need wider initial SL,
    higher activation threshold, looser trail, and more time to develop.
    """
    p = pump_pct or 50.0
    if p < 50:
        return {
            "initial_sl_pct": 8.0,
            "activation_pct": 8.0,
            "trail_pct": 12.0,
            "trail_tighten_pct": 8.0,
            "tighten_after_min": 90.0,
            "max_hold_min": 180.0,
            "no_progress_min": 60.0,
        }
    if p < 100:
        return {
            "initial_sl_pct": 10.0,
            "activation_pct": 12.0,
            "trail_pct": 15.0,
            "trail_tighten_pct": 10.0,
            "tighten_after_min": 120.0,
            "max_hold_min": 240.0,
            "no_progress_min": 60.0,
        }
    return {
        "initial_sl_pct": 12.0,
        "activation_pct": 15.0,
        "trail_pct": 20.0,
        "trail_tighten_pct": 12.0,
        "tighten_after_min": 180.0,
        "max_hold_min": 360.0,
        "no_progress_min": 60.0,
    }


_REQUIRED_PARAM_KEYS = frozenset(
    {
        "initial_sl_pct",
        "activation_pct",
        "trail_pct",
        "trail_tighten_pct",
        "tighten_after_min",
        "no_progress_min",
        "max_hold_min",
    }
)


def load_exit_params(raw: bytes | str | None) -> dict[str, float]:
    """Parse exit params from Redis bytes, falling back to defaults on any error."""
    if not raw:
        return exit_params(None)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not a dict")
        missing = _REQUIRED_PARAM_KEYS - data.keys()
        if missing:
            raise ValueError(f"missing keys: {missing}")
        if not all(isinstance(data[k], int | float) for k in _REQUIRED_PARAM_KEYS):
            raise ValueError("non-numeric value")
        return data
    except Exception as exc:
        log.warning("exit.params.invalid", raw=repr(raw)[:80], error=str(exc))
        return exit_params(None)


def best_price_key(exchange: str, base: str, *, paper: bool = False) -> str:
    tpl = _BEST_KEY_PAPER if paper else _BEST_KEY
    return tpl.format(exchange=exchange, base=base.upper())


def params_key(exchange: str, base: str) -> str:
    return _PARAMS_KEY.format(exchange=exchange, base=base.upper())


def entry_key(exchange: str, base: str) -> str:
    return _ENTRY_KEY.format(exchange=exchange, base=base.upper())


def side_key(exchange: str, base: str) -> str:
    return _SIDE_KEY.format(exchange=exchange, base=base.upper())


def size_usd_key(exchange: str, base: str) -> str:
    return _SIZE_USD_KEY.format(exchange=exchange, base=base.upper())


async def check_exit(
    *,
    side: str,
    entry_price: float,
    current_price: float,
    opened_at: float,
    params: dict[str, float],
    rdb: Any,
    bp_key: str,
) -> str | None:
    """3-phase exit check. Returns close reason string or None.

    Phase 1 (pre-activation): fixed initial SL — protects against immediate reversal.
    Phase 2 (activation):     trailing begins once the trade is in profit by activation_pct.
    Phase 3 (tightening):     trail narrows after tighten_after_min to lock in profits.
    max_hold applies at all phases.
    """
    initial_sl_pct = params["initial_sl_pct"]
    activation_pct = params["activation_pct"]
    trail_pct = params["trail_pct"]
    trail_tighten_pct = params["trail_tighten_pct"]
    tighten_after_min = params["tighten_after_min"]
    max_hold_min = params["max_hold_min"]
    no_progress_min = params["no_progress_min"]

    elapsed_min = (time.time() - opened_at) / 60
    if elapsed_min >= max_hold_min:
        return f"max_hold age={elapsed_min:.0f}min"

    # move_pct > 0 means trade is in profit
    if side == "short":
        move_pct = (entry_price - current_price) / entry_price * 100
    else:
        move_pct = (current_price - entry_price) / entry_price * 100

    best_raw = await rdb.get(bp_key)

    if best_raw is None:
        # Phase 1: fixed initial SL
        if elapsed_min >= no_progress_min:
            return f"no_progress age={elapsed_min:.0f}min"
        if move_pct <= -initial_sl_pct:
            return f"initial_sl move={move_pct:.1f}%"
        # Activate trailing when in profit by activation_pct
        if move_pct >= activation_pct:
            await rdb.set(bp_key, str(current_price), ex=_KEY_TTL)
        return None

    # Phase 2/3: trailing
    best = float(best_raw)
    new_best = min(best, current_price) if side == "short" else max(best, current_price)
    if new_best != best:
        await rdb.set(bp_key, str(new_best), ex=_KEY_TTL)
        best = new_best

    trail = trail_tighten_pct if elapsed_min >= tighten_after_min else trail_pct

    if side == "short":
        if current_price >= best * (1 + trail / 100):
            profit = (entry_price - current_price) / entry_price * 100
            return f"trailing_stop trail={trail:.0f}% profit={profit:.1f}%"
    else:
        if current_price <= best * (1 - trail / 100):
            profit = (current_price - entry_price) / entry_price * 100
            return f"trailing_stop trail={trail:.0f}% profit={profit:.1f}%"

    return None
