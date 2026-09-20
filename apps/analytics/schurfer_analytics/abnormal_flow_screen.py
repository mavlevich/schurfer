"""Frozen contract and pure features for the abnormal-flow economic screen (HYP
discovery family; design: docs/research/abnormal-flow-economic-screen-v1.md).

PRE-REGISTRATION. The feature FORMS here are frozen (owner-frozen 2026-09-20) so the
operationalization cannot be chosen after seeing returns; the numeric THRESHOLDS and
the full run specification (calibration rule, scan lag, entry/exit, matching,
portfolio, verdict) are NOT frozen, and a formal run must refuse to proceed until
every one of them is pinned AND in range (``require_frozen``). This module reads no
returns and makes no economic claim: it holds the contract and the pure per-window
feature maths only.

Primary mechanism (the ONE primary cell): positive within-instrument OI growth over
the prior 60 minutes, aggressive taker-buy notional dominating sell over that same
hour, and restrained price movement (bar high/low, not just closes) before the
decision; long; 720-minute horizon. Novelty is the OI requirement: the same
buy/price signal WITHOUT the OI-growth threshold, over the SAME eligible set, is the
registered ablation, and no incremental OI benefit closes the mechanism.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

CONTRACT_VERSION = "abnormal_flow_screen_v1"

# --- Frozen FORMS (not thresholds) -------------------------------------------------

LOOKBACK_MINUTES = 60
OUTCOME_HORIZON_MINUTES = 720
DIRECTION = "long"
# The episode cooldown is at least the outcome horizon so overlapping fires on one
# instrument are not counted as independent evidence.
COOLDOWN_MINUTES = OUTCOME_HORIZON_MINUTES


def _finite(x: float | None) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)


def oi_growth_pct_60m(oi_start: float, oi_end: float) -> float | None:
    """Within-instrument OI growth as a PERCENT of native OI amount over the 60m
    lookback: ``(oi_end - oi_start) / oi_start * 100``. No cross-venue comparison and
    no trailing z-score (frozen form). ``None`` (window unavailable) when either value
    is non-finite (NaN/Inf), the start is not a usable positive base, or the end is
    negative, so corrupt data never becomes a misleading percentage."""
    if not _finite(oi_start) or not _finite(oi_end) or oi_start <= 0 or oi_end < 0:
        return None
    return (oi_end - oi_start) / oi_start * 100.0


def buy_pressure_ratio_60m(buy_notional_usd: float, sell_notional_usd: float) -> float | None:
    """Taker buy pressure over the 60m lookback: ``buy / (buy + sell)`` in USD
    notional. ``None`` for a non-finite input, a negative value, or a zero-flow window
    (the ratio is undefined and the window is explicitly unavailable, never silently 0
    or 0.5). Frozen form."""
    if not _finite(buy_notional_usd) or not _finite(sell_notional_usd):
        return None
    if buy_notional_usd < 0 or sell_notional_usd < 0:
        return None
    total = buy_notional_usd + sell_notional_usd
    if total <= 0:
        return None
    return buy_notional_usd / total


def price_containment_max_bar_dev(
    open_price: float, bars: Sequence[tuple[float, float]]
) -> float | None:
    """Price restraint over the lookback as the MAXIMUM absolute deviation of the
    within-window bar EXTREMES (high and low of each 1m bar) from the window's OPENING
    price, as a fraction of that open: ``max over bars of max(|high-open|,|low-open|)
    / open``.

    Bar extremes, not closes: a strong intraminute wick that closes back near the open
    (e.g. SAYLORMOON) is NOT restrained, and closes-only would miss it (chosen here,
    before any returns were read). ``None`` (unavailable) when the open is not a usable
    positive base, there are no bars, or any high/low is non-finite. Frozen form."""
    if not _finite(open_price) or open_price <= 0 or not bars:
        return None
    worst = 0.0
    for high, low in bars:
        if not _finite(high) or not _finite(low):
            return None
        dev = max(abs(high - open_price), abs(low - open_price)) / open_price
        if dev > worst:
            worst = dev
    return worst


# --- Contract (thresholds + run spec NOT frozen; fail-closed until pinned) ----------


@dataclass(frozen=True)
class AbnormalFlowContract:
    """The full screen contract. Every numeric/spec field below defaults to ``None``
    and is DELIBERATELY unset: it must be pinned by one pre-declared outcome-blind rule
    before any returns are read. ``require_frozen`` fail-closes a formal run until every
    field is set AND within its declared range, so a run can never quietly select or
    corrupt (NaN/negative/out-of-range) the parameters it is scoring."""

    contract_version: str = CONTRACT_VERSION
    # Frozen forms (validated as unchanged, not merely present).
    lookback_minutes: int = LOOKBACK_MINUTES
    outcome_horizon_minutes: int = OUTCOME_HORIZON_MINUTES
    cooldown_minutes: int = COOLDOWN_MINUTES
    direction: str = DIRECTION

    # Primary-cell thresholds (frozen later, outcome-blind).
    min_oi_growth_pct: float | None = None
    min_buy_pressure_ratio: float | None = None
    max_price_containment: float | None = None

    # Eligibility floor. min_oi_notional_usd needs a per-venue native-OI -> USD rule at
    # a point-in-time price (Binance has no OI-value field); participation is against
    # pre-decision turnover, never the future entry minute.
    min_oi_notional_usd: float | None = None
    oi_usd_conversion_rule: str | None = None
    position_usd: float | None = None
    max_participation_frac: float | None = None
    participation_turnover_window_minutes: int | None = None

    # Registered run specification the formal run must satisfy in full.
    calibration_rule: str | None = None
    calibration_window_days: int | None = None
    scan_lag_minutes: int | None = None
    entry_reference: str | None = None
    exit_reference: str | None = None
    matching_rule: str | None = None
    portfolio_bank_usd: float | None = None
    portfolio_max_slots: int | None = None

    # Conservative execution/cost model (priced-proxy entry/exit).
    entry_cost_bps: float | None = None
    slippage_bps: float | None = None
    fee_bps: float | None = None
    funding_model: str | None = None

    # Evidence / missingness gates and the one-shot verdict inputs.
    min_resolved_episodes: int | None = None
    max_missing_fraction: float | None = None
    min_excess_over_control_pct: float | None = None

    def problems(self) -> tuple[str, ...]:
        """Every reason this contract is not a valid frozen spec: unset fields, frozen
        forms that were altered, and out-of-range or non-finite numbers."""
        issues: list[str] = []

        # Frozen forms must be exactly the pre-registered ones.
        if self.lookback_minutes != LOOKBACK_MINUTES:
            issues.append(f"lookback_minutes must be {LOOKBACK_MINUTES}")
        if self.outcome_horizon_minutes != OUTCOME_HORIZON_MINUTES:
            issues.append(f"outcome_horizon_minutes must be {OUTCOME_HORIZON_MINUTES}")
        if self.cooldown_minutes < self.outcome_horizon_minutes:
            issues.append("cooldown_minutes must be >= outcome_horizon_minutes")
        if self.direction != DIRECTION:
            issues.append(f"direction must be {DIRECTION!r}")

        def num(
            name: str,
            *,
            low: float | None = None,
            high: float | None = None,
            inclusive_low: bool = True,
            inclusive_high: bool = True,
        ) -> None:
            v = getattr(self, name)
            if v is None:
                issues.append(f"{name} is not set")
                return
            if not _finite(v):
                issues.append(f"{name} must be a finite number")
                return
            if low is not None and (v < low or (v == low and not inclusive_low)):
                issues.append(
                    f"{name} must be > {low}" if not inclusive_low else f"{name} must be >= {low}"
                )
            if high is not None and (v > high or (v == high and not inclusive_high)):
                issues.append(
                    f"{name} must be < {high}"
                    if not inclusive_high
                    else f"{name} must be <= {high}"
                )

        def integer(name: str, *, low: int) -> None:
            v = getattr(self, name)
            if v is None:
                issues.append(f"{name} is not set")
            elif not isinstance(v, int) or isinstance(v, bool) or v < low:
                issues.append(f"{name} must be an integer >= {low}")

        def text(name: str) -> None:
            v = getattr(self, name)
            if not isinstance(v, str) or not v:
                issues.append(f"{name} is not set")

        num("min_oi_growth_pct", low=0.0, inclusive_low=False)
        num("min_buy_pressure_ratio", low=0.5, high=1.0, inclusive_low=False)
        num("max_price_containment", low=0.0, inclusive_low=False)
        num("min_oi_notional_usd", low=0.0, inclusive_low=False)
        text("oi_usd_conversion_rule")
        num("position_usd", low=0.0, inclusive_low=False)
        num("max_participation_frac", low=0.0, high=1.0, inclusive_low=False)
        integer("participation_turnover_window_minutes", low=1)
        text("calibration_rule")
        integer("calibration_window_days", low=1)
        integer("scan_lag_minutes", low=0)
        text("entry_reference")
        text("exit_reference")
        text("matching_rule")
        num("portfolio_bank_usd", low=0.0, inclusive_low=False)
        integer("portfolio_max_slots", low=1)
        num("entry_cost_bps", low=0.0)
        num("slippage_bps", low=0.0)
        num("fee_bps", low=0.0)
        text("funding_model")
        integer("min_resolved_episodes", low=1)
        num("max_missing_fraction", low=0.0, high=1.0)
        num("min_excess_over_control_pct", low=0.0)
        return tuple(issues)

    def is_frozen(self) -> bool:
        return not self.problems()

    def require_frozen(self) -> None:
        """Raise unless every threshold/spec field is pinned AND in range. A formal,
        returns-reading run calls this first, so parameters can never be chosen from,
        or corrupted before, the results."""
        problems = self.problems()
        if problems:
            raise NotFrozenError(
                "abnormal-flow contract is not a valid frozen spec; refusing a formal "
                "run until the pre-declared outcome-blind rule pins/repairs: " + "; ".join(problems)
            )


class NotFrozenError(RuntimeError):
    """A formal (returns-reading) run was attempted before the contract's thresholds
    and run specification were fully pinned and validated. Discovery/scanning that
    reads no returns does not raise this."""
