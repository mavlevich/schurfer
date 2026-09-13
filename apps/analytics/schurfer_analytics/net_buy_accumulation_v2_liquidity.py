"""Executability / capacity analysis for the net-buy accumulation v2 signal. This is
the CHEAP, OUTCOME-BLIND half of the L2/latency shadow: it uses only traded notional
we already capture (never a price or a return) to answer, before any live order-book
collector is built, one question:

    at a given target position size, how much can we actually deliver into each fire
    without dominating the flow, and how many fires survive as economically worth a
    slot?

Two hard corrections over the first cut (both from methodology review):

1. POINT-IN-TIME ONLY. The fire minute's own traded notional is NOT known at
   `decision_at` (it accrues over the whole minute), so using it to gate or size a
   trade is look-ahead. Sizing uses only flow that is complete BEFORE the fire
   minute: `conservative_trailing_flow_usd` is the p25 of per-minute traded notional
   over the trailing window, strictly earlier than the fire. The fire-minute notional
   is retained as a DIAGNOSTIC only.

2. NO BINARY $1500 GATE. Instead of dropping fires under a fixed-size participation
   cap, we DYNAMICALLY SIZE: deliver `min(target_notional, cap * conservative_trailing
   _flow)` and skip a fire only when even that is below a pre-registered minimum
   economic notional. So thin fires shrink the position rather than being silently
   excluded, and the capacity curve shows the size/throughput trade-off.

Traded notional is realized flow, not resting order-book depth: high participation
is decisively bad, but low participation does NOT prove a good fill. This bounds the
problem from one side only; the live L2 depth + latency shadow remains the authority,
and no return is read here, so nothing here is a verdict on edge.
"""

from __future__ import annotations

from dataclasses import dataclass

# Capacity-curve target sizes (USD notional). 300 is the test bank at 1x; 1500 is the
# same bank at the 5x ceiling. 50/100 show how throughput recovers at smaller size.
TARGET_NOTIONALS_USD = (50.0, 100.0, 300.0, 1500.0)

# We allow ourselves to be at most this fraction of the conservative trailing flow.
# Candidate only; the real impact ceiling comes from the L2 shadow. 0.10 is the common
# "stay under ~10% of volume" market-impact rule of thumb.
PARTICIPATION_CAP = 0.10

# A fire whose dynamically sized deliverable notional falls below this is not worth a
# portfolio slot. Candidate: the old USD 50 real-money mechanics-test floor; the real
# value needs owner/ECONOMICS sign-off before it is frozen.
MIN_ECONOMIC_NOTIONAL_USD = 50.0


@dataclass(frozen=True)
class FireFlow:
    """One deduped fire's point-in-time flow, in USD. `conservative_trailing_flow_usd`
    is the p25 of per-minute traded notional over the trailing window that ends BEFORE
    the fire minute (known at decision time, safe to size on).
    `fire_minute_notional_usd` is the fire minute's own traded notional: a DIAGNOSTIC
    only, not known at decision time, never used for sizing or gating."""

    exchange: str
    symbol: str
    conservative_trailing_flow_usd: float
    fire_minute_notional_usd: float

    def deliverable_notional(self, target_usd: float, cap: float) -> float:
        """The size we could enter without exceeding `cap` of conservative trailing
        flow: `min(target, cap * flow)`. Zero when there is no trailing flow (nothing
        to size against)."""
        if self.conservative_trailing_flow_usd <= 0:
            return 0.0
        return min(target_usd, cap * self.conservative_trailing_flow_usd)


@dataclass(frozen=True)
class CapacityPoint:
    """Capacity at one target size: how many fires stay economically tradeable after
    dynamic sizing, and how much notional they actually absorb. `flow_capped_fires` is
    the count whose deliverable was clipped below target by the flow cap (we could not
    reach full size); `zero_flow_fires` had no trailing flow at all."""

    primary: str
    theta: float
    target_notional_usd: float
    cap: float
    min_economic_notional_usd: float
    n_fires: int
    n_tradeable: int
    tradeable_rate: float | None
    delivered_median_usd: float | None
    delivered_p25_usd: float | None
    flow_capped_fires: int
    zero_flow_fires: int


def _quantile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated quantile of a non-empty pre-sorted list (`q` in [0, 1])."""
    if not sorted_vals:
        raise ValueError("empty")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def capacity_at_target(
    primary: str,
    theta: float,
    fires: list[FireFlow],
    *,
    target_notional_usd: float,
    cap: float = PARTICIPATION_CAP,
    min_economic_notional_usd: float = MIN_ECONOMIC_NOTIONAL_USD,
) -> CapacityPoint:
    """Reduce a primary's deduped fires to capacity at one target size via dynamic
    sizing. A fire is tradeable when its deliverable notional is at least the minimum
    economic notional. Deterministic."""
    delivered_tradeable: list[float] = []
    flow_capped = 0
    zero_flow = 0
    for f in fires:
        if f.conservative_trailing_flow_usd <= 0:
            zero_flow += 1
            continue
        delivered = f.deliverable_notional(target_notional_usd, cap)
        if delivered < min_economic_notional_usd:
            continue
        delivered_tradeable.append(delivered)
        if delivered < target_notional_usd:
            flow_capped += 1
    delivered_tradeable.sort()
    n = len(fires)
    n_tradeable = len(delivered_tradeable)
    return CapacityPoint(
        primary=primary,
        theta=theta,
        target_notional_usd=target_notional_usd,
        cap=cap,
        min_economic_notional_usd=min_economic_notional_usd,
        n_fires=n,
        n_tradeable=n_tradeable,
        tradeable_rate=(n_tradeable / n) if n else None,
        delivered_median_usd=_quantile(delivered_tradeable, 0.5) if delivered_tradeable else None,
        delivered_p25_usd=_quantile(delivered_tradeable, 0.25) if delivered_tradeable else None,
        flow_capped_fires=flow_capped,
        zero_flow_fires=zero_flow,
    )


def capacity_curve(
    primary: str,
    theta: float,
    fires: list[FireFlow],
    *,
    targets: tuple[float, ...] = TARGET_NOTIONALS_USD,
    cap: float = PARTICIPATION_CAP,
    min_economic_notional_usd: float = MIN_ECONOMIC_NOTIONAL_USD,
) -> list[CapacityPoint]:
    """The capacity curve: `capacity_at_target` across every target size. Outcome-blind;
    reads no return."""
    return [
        capacity_at_target(
            primary,
            theta,
            fires,
            target_notional_usd=t,
            cap=cap,
            min_economic_notional_usd=min_economic_notional_usd,
        )
        for t in targets
    ]


__all__ = [
    "MIN_ECONOMIC_NOTIONAL_USD",
    "PARTICIPATION_CAP",
    "TARGET_NOTIONALS_USD",
    "CapacityPoint",
    "FireFlow",
    "capacity_at_target",
    "capacity_curve",
]
