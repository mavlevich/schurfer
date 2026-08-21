"""Bar-by-bar exit-policy replay for analysis/liquidation-cascade-validation-v2.

Reuses apps/execution/schurfer_execution/liquidation_cascade.py's own live
`exit_params` (SL 3% / TP 5% / trail 10%/10% tighten after 60min / max_hold
60min) -- must track that file's own dict, same manual-sync-with-comment
convention `liquidation_cascade_repository.py` already uses for the entry
thresholds.

Simplification, disclosed rather than silent (matching momentum_flow_
bidirectional_burst_study.py's own precedent for a known, bounded modeling
gap): under the CURRENTLY REGISTERED exit_params, take_profit_pct (5%) is
always shallower than activation_pct (10%), so the trailing stop never
actually arms before either TP or the SL/max-hold exit resolves the trade --
"TP hits first", per that file's own comment. This replay therefore checks
SL, then TP, then max-hold, in that order, and does not model the trailing-
stop state machine. If the live exit_params ever change such that
activation_pct <= take_profit_pct, this simplification stops being faithful
and must be revisited together with the runtime file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from schurfer_performance.accounting import (
    DEFAULT_COSTS,
    AccountingResult,
    CostParameters,
    calculate_performance,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from .liquidation_cascade_repository import OutcomeBar, Quote


@dataclass(frozen=True)
class ExitPolicy:
    initial_sl_pct: float = 3.0
    take_profit_pct: float = 5.0
    max_hold_minutes: int = 60

    def __post_init__(self) -> None:
        if self.initial_sl_pct <= 0 or self.take_profit_pct <= 0:
            raise ValueError("stop and target percentages must be positive")
        if self.max_hold_minutes <= 0:
            raise ValueError("max_hold_minutes must be positive")


# Must track apps/execution/schurfer_execution/liquidation_cascade.py's own
# exit_params dict -- see this module's own doc comment.
RUNTIME_EXIT_POLICY = ExitPolicy(initial_sl_pct=3.0, take_profit_pct=5.0, max_hold_minutes=60)


@dataclass(frozen=True)
class ReplayExit:
    exit_at: datetime
    exit_price: float
    exit_reason: str  # "stop_loss" | "take_profit" | "max_hold"
    duration_minutes: float

    def __post_init__(self) -> None:
        if self.exit_reason not in {"stop_loss", "take_profit", "max_hold"}:
            raise ValueError("unknown exit_reason")
        if self.duration_minutes < 0:
            raise ValueError("duration_minutes must be non-negative")


def simulate_exit(
    *,
    entry_at: datetime,
    entry_price: float,
    bars: Sequence[OutcomeBar],
    policy: ExitPolicy = RUNTIME_EXIT_POLICY,
) -> ReplayExit | None:
    """Long-only bar-by-bar replay (the live strategy is long-only -- see
    liquidation_cascade.py's own hardcoded `side="long"`).

    Walks the EXPECTED minute-by-minute sequence from `entry_at + 1` to the
    max-hold deadline, not just the bars that happen to be present in
    `bars`. A minute with no bar at all, or an incomplete one, stops the
    walk immediately and returns None (unresolved) -- fail-closed, not
    skipped over. A gap that happened to contain the real stop-loss or
    take-profit crossing must never let a later, unrelated complete bar
    "win" the replay by default (colleague review, 2026-08-21: the previous
    version only skipped incomplete/missing bars, so an incomplete minute
    that actually breached the stop could be followed by a complete bar
    that reached take-profit, and the replay would wrongly record a win)."""
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    stop_price = entry_price * (1 - policy.initial_sl_pct / 100)
    target_price = entry_price * (1 + policy.take_profit_pct / 100)
    by_bucket = {bar.bucket_start: bar for bar in bars}
    deadline = entry_at + timedelta(minutes=policy.max_hold_minutes)

    minute = entry_at + timedelta(minutes=1)
    while minute <= deadline:
        bar = by_bucket.get(minute)
        if bar is None or not bar.complete or bar.low_price is None or bar.high_price is None:
            return None
        if bar.low_price <= stop_price:
            return ReplayExit(
                exit_at=minute,
                exit_price=stop_price,
                exit_reason="stop_loss",
                duration_minutes=(minute - entry_at).total_seconds() / 60,
            )
        if bar.high_price >= target_price:
            return ReplayExit(
                exit_at=minute,
                exit_price=target_price,
                exit_reason="take_profit",
                duration_minutes=(minute - entry_at).total_seconds() / 60,
            )
        if minute == deadline:
            if bar.close_price is None:
                return None
            return ReplayExit(
                exit_at=minute,
                exit_price=bar.close_price,
                exit_reason="max_hold",
                duration_minutes=(minute - entry_at).total_seconds() / 60,
            )
        minute += timedelta(minutes=1)
    return None


def _half_spread_bps(quote: Quote | None) -> float | None:
    """Required, never assumed zero -- an episode with no resolved spread at
    a leg gets `entry_slippage_bps`/`exit_slippage_bps=None`, which
    `calculate_performance` itself already fails closed on
    (`status="incomplete"`, `net_return_pct=None`)."""
    if quote is None or not quote.price_complete:
        return None
    if quote.last_bid_price is None or quote.last_ask_price is None:
        return None
    if quote.last_bid_price <= 0 or quote.last_ask_price <= quote.last_bid_price:
        return None
    mid = (quote.last_bid_price + quote.last_ask_price) / 2
    return (quote.last_ask_price - quote.last_bid_price) / mid * 10_000 / 2


def net_return_from_replay(
    *,
    entry_price: float,
    exit_result: ReplayExit,
    entry_quote: Quote | None,
    exit_quote: Quote | None,
    position_usd: float,
    costs: CostParameters = DEFAULT_COSTS,
) -> AccountingResult:
    return calculate_performance(
        position_usd=position_usd,
        entry_price=entry_price,
        exit_price=exit_result.exit_price,
        side="long",
        duration_minutes=exit_result.duration_minutes,
        entry_slippage_bps=_half_spread_bps(entry_quote),
        exit_slippage_bps=_half_spread_bps(exit_quote),
        costs=costs,
    )
