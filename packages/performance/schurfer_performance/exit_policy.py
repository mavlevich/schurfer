"""Pure exit policy shared by the live monitor, the paper broker, and replay.

The live monitor keeps the running best price in Redis and the replay keeps it
in a local variable, but the *decision* -- initial stop, trailing activation,
trail tightening, and both time-based exits -- must be one implementation.
Reimplementing it for offline work produced a measured answer about a policy
that does not exist (see docs/research/stop-survival-bound-v1.md), which is the
failure this module is here to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

EXIT_POLICY_VERSION = "dynamic_trailing_v1"

REQUIRED_PARAM_KEYS = frozenset(
    {
        "initial_sl_pct",
        "activation_pct",
        "trail_pct",
        "trail_tighten_pct",
        "tighten_after_min",
        "max_hold_min",
    }
)


def exit_params(pump_pct: float | None) -> dict[str, float]:
    """Compute exit parameters from pump magnitude.

    Bigger pumps are more volatile and retrace deeper -- they need wider initial SL,
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


@dataclass(frozen=True)
class ExitEvaluation:
    """One exit decision, plus the best price the caller must persist.

    `best_price` is None when the running extreme did not change, so a caller
    backed by Redis writes only when there is something to write. It is set
    even when `reason` is not None: the caller must not have to reason about
    whether a closing tick also advanced the extreme.
    """

    reason: str | None
    best_price: float | None


def evaluate_exit(
    *,
    side: str,
    entry_price: float,
    current_price: float,
    elapsed_min: float,
    best_price: float | None,
    params: Mapping[str, float],
) -> ExitEvaluation:
    """3-phase exit check. Returns the close reason, or None to stay open.

    Phase 1 (pre-activation): fixed initial SL -- protects against immediate reversal.
    Phase 2 (activation):     trailing begins once the trade is in profit by activation_pct.
    Phase 3 (tightening):     trail narrows after tighten_after_min to lock in profits.
    max_hold applies at all phases.

    `best_price` is None until trailing activates. Note what that implies and
    what the phase names alone do not say: once trailing is active, the initial
    stop and the no-progress exit no longer apply at all.
    """
    initial_sl_pct = params["initial_sl_pct"]
    activation_pct = params["activation_pct"]
    trail_pct = params["trail_pct"]
    trail_tighten_pct = params["trail_tighten_pct"]
    tighten_after_min = params["tighten_after_min"]
    max_hold_min = params["max_hold_min"]
    no_progress_min = params.get("no_progress_min", max_hold_min)
    take_profit_pct = params.get("take_profit_pct")

    if elapsed_min >= max_hold_min:
        return ExitEvaluation(f"max_hold age={elapsed_min:.0f}min", None)

    # move_pct > 0 means trade is in profit
    if side == "short":
        move_pct = (entry_price - current_price) / entry_price * 100
    else:
        move_pct = (current_price - entry_price) / entry_price * 100

    if take_profit_pct is not None and move_pct >= take_profit_pct:
        return ExitEvaluation(f"take_profit move={move_pct:.1f}%", None)

    if best_price is None:
        # Phase 1: fixed initial SL
        if elapsed_min >= no_progress_min:
            return ExitEvaluation(f"no_progress age={elapsed_min:.0f}min", None)
        if move_pct <= -initial_sl_pct:
            return ExitEvaluation(f"initial_sl move={move_pct:.1f}%", None)
        # Activate trailing when in profit by activation_pct
        if move_pct >= activation_pct:
            return ExitEvaluation(None, current_price)
        return ExitEvaluation(None, None)

    # Phase 2/3: trailing
    best = best_price
    new_best = min(best, current_price) if side == "short" else max(best, current_price)
    advanced = new_best if new_best != best else None
    best = new_best

    trail = trail_tighten_pct if elapsed_min >= tighten_after_min else trail_pct

    if side == "short":
        stopped = current_price >= best * (1 + trail / 100)
        profit = (entry_price - current_price) / entry_price * 100
    else:
        stopped = current_price <= best * (1 - trail / 100)
        profit = (current_price - entry_price) / entry_price * 100

    if stopped:
        reason = f"trailing_stop trail={trail:.0f}% profit={profit:.1f}%"
        return ExitEvaluation(reason, advanced)

    return ExitEvaluation(None, advanced)
