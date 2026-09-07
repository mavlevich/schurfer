"""Redis-backed wrapper around the shared exit policy.

The decision itself lives in schurfer_performance.exit_policy so the live
monitor, the paper broker, and offline replay cannot drift apart. What is left
here is the part that is genuinely about this process: where the running best
price and the per-position parameters are stored, and what "now" means.
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog
from schurfer_performance.exit_policy import (
    REQUIRED_PARAM_KEYS as _REQUIRED_PARAM_KEYS,
)
from schurfer_performance.exit_policy import (
    evaluate_exit,
    exit_params,
)

__all__ = [
    "best_price_key",
    "check_exit",
    "entry_key",
    "exit_params",
    "load_exit_params",
    "params_key",
    "side_key",
    "size_usd_key",
]

log = structlog.get_logger()

_BEST_KEY = "exit:best:{exchange}:{base}"
_BEST_KEY_PAPER = "exit:best:paper:{exchange}:{base}"
_PARAMS_KEY = "exit:params:{exchange}:{base}"
_ENTRY_KEY = "position:entry:{exchange}:{base}"
_SIDE_KEY = "position:side:{exchange}:{base}"
_SIZE_USD_KEY = "position:size_usd:{exchange}:{base}"

_KEY_TTL = 86400 * 7


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
    """Evaluate the shared exit policy against the best price kept in Redis.

    The best price is read before the elapsed-time and take-profit checks,
    which the pure policy applies first. That costs one extra GET on the two
    ticks that close a position on those paths -- once per position lifetime --
    and buys a single decision function for live, paper, and replay.
    """
    best_raw = await rdb.get(bp_key)
    evaluation = evaluate_exit(
        side=side,
        entry_price=entry_price,
        current_price=current_price,
        elapsed_min=(time.time() - opened_at) / 60,
        best_price=None if best_raw is None else float(best_raw),
        params=params,
    )
    if evaluation.best_price is not None:
        await rdb.set(bp_key, str(evaluation.best_price), ex=_KEY_TTL)
    return evaluation.reason
