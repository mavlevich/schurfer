"""LIVE_PROBE loss threshold L: the probe's loss and the entry reserve.

docs/engineering/live-probe-loss-threshold-design-v1.md, sections 1 to 3. Arithmetic
only, all in ``Decimal``: the inputs come from the journal, the open-position collector
and Bybit reads, and nothing here reads or writes anything.

    loss(t) = - realized_closed
              - realized_open_parts
              - unrealized_at_mark(t)
              + fees_paid_open
              + funding_paid_open
              + funding_unlisted_estimate
              + exit_fee_estimate(t)

With no open probe position every term after ``realized_closed`` is zero and no mark is
needed. With one, the funding tail after the newest published settlement is never
proven, so the loss is a lower bound: it may decide that L is reached, never that it is
not.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

ZERO = Decimal(0)
MARK_MAX_AGE = timedelta(seconds=60)
STALE_MARK_ALERT_TICKS = 3


def side_sign(side: str) -> int:
    if side == "long":
        return 1
    if side == "short":
        return -1
    raise ValueError(f"unknown side {side!r}")


@dataclass(frozen=True)
class Fill:
    """One confirmed execution of the open trade: quantity and price."""

    quantity: Decimal
    price: Decimal


def entry_vwap(entries: Iterable[Fill]) -> Decimal:
    """The amount-weighted entry price of the trade's own entry fills."""
    fills = tuple(entries)
    quantity = sum((f.quantity for f in fills), ZERO)
    if quantity <= 0:
        raise ValueError("an open trade needs a positive entry quantity")
    return sum((f.quantity * f.price for f in fills), ZERO) / quantity


def realized_open_parts(side: str, vwap: Decimal, exits: Iterable[Fill]) -> Decimal:
    """Gross PnL of the parts of the open trade already closed:
    ``sign * sum(exit_qty_i * (exit_price_i - entry_vwap))``. Fees are not in it."""
    sign = side_sign(side)
    return sign * sum((f.quantity * (f.price - vwap) for f in exits), ZERO)


def settlement_estimate(
    side: str, *, quantity_held: Decimal, mark: Decimal, rate: Decimal
) -> Decimal:
    """Funding paid at one settlement not listed yet, by Bybit's rule (position value
    times rate; a positive rate means long pays short). Funding received counts as zero
    until its row is listed."""
    return max(ZERO, side_sign(side) * quantity_held * mark * rate)


@dataclass(frozen=True)
class UnlistedSettlement:
    """A settlement the probe held that no history lists yet. Any input that could not
    be read makes it unknown: no estimate is made from the current quantity or mark."""

    at: datetime
    rate: Decimal | None
    quantity_held: Decimal | None
    mark: Decimal | None

    def estimate(self, side: str) -> Decimal | None:
        if self.rate is None or self.quantity_held is None or self.mark is None:
            return None
        return settlement_estimate(
            side, quantity_held=self.quantity_held, mark=self.mark, rate=self.rate
        )


@dataclass(frozen=True)
class OpenPosition:
    """The open probe trade as the collector owns it (section 1.1, ``unproven_tail``)."""

    side: str
    entry_vwap: Decimal
    open_qty: Decimal
    realized_open_parts: Decimal
    fees_paid_open: Decimal
    funding_paid_open: Decimal
    unlisted: tuple[UnlistedSettlement, ...] = ()


@dataclass(frozen=True)
class Loss:
    loss: Decimal
    # True while a position is open: the funding tail is unproven, so the true loss
    # may be higher.
    lower_bound: bool
    terms: dict[str, Decimal]
    unknown_settlements: tuple[datetime, ...]

    def reached(self, threshold: Decimal) -> bool:
        """L is reached when the loss is at least L. A lower bound can only decide
        reached; below L it decides nothing."""
        return self.loss >= threshold


def probe_loss(
    realized_closed: Decimal,
    position: OpenPosition | None = None,
    *,
    mark: Decimal | None = None,
    taker_rate: Decimal | None = None,
) -> Loss:
    """The probe's loss at a fresh mark. ``realized_closed`` is the sum of ``pnl_usd``
    of its closed trades."""
    if position is None:
        return Loss(
            loss=-realized_closed,
            lower_bound=False,
            terms={"realized_closed": realized_closed},
            unknown_settlements=(),
        )
    if mark is None or taker_rate is None:
        raise ValueError("an open probe position needs a fresh mark and the taker rate")
    if position.open_qty <= 0:
        raise ValueError("an open probe position needs a positive open quantity")
    if taker_rate < 0:
        raise ValueError("a negative taker rate would understate the close")
    sign = side_sign(position.side)
    unrealized = sign * position.open_qty * (mark - position.entry_vwap)
    estimated = ZERO
    unknown: list[datetime] = []
    for settlement in position.unlisted:
        estimate = settlement.estimate(position.side)
        if estimate is None:
            unknown.append(settlement.at)
        else:
            estimated += estimate
    exit_fee = position.open_qty * mark * taker_rate
    terms = {
        "realized_closed": realized_closed,
        "realized_open_parts": position.realized_open_parts,
        "unrealized_at_mark": unrealized,
        "fees_paid_open": position.fees_paid_open,
        "funding_paid_open": position.funding_paid_open,
        "funding_unlisted_estimate": estimated,
        "exit_fee_estimate": exit_fee,
    }
    loss = (
        -realized_closed
        - position.realized_open_parts
        - unrealized
        + position.fees_paid_open
        + position.funding_paid_open
        + estimated
        + exit_fee
    )
    return Loss(loss=loss, lower_bound=True, terms=terms, unknown_settlements=tuple(unknown))


def mark_is_fresh(read_at: datetime, now: datetime) -> bool:
    """A mark decides only within 60 s of the read that returned it."""
    return timedelta(0) <= now - read_at <= MARK_MAX_AGE


def settlements_in_max_hold(max_hold: timedelta, interval: timedelta) -> int:
    """Settlements the strategy's maximum hold can span: rounded up, plus one."""
    if max_hold <= timedelta(0) or interval <= timedelta(0):
        raise ValueError("the maximum hold and the funding interval must be positive")
    return math.ceil(max_hold / interval) + 1


@dataclass(frozen=True)
class ReserveInputs:
    initial_sl_pct: Decimal
    taker_rate: Decimal
    funding_rate: Decimal
    settlements_in_max_hold: int

    def as_json(self) -> dict[str, str | int]:
        return {
            "initial_sl_pct": str(self.initial_sl_pct),
            "taker_rate": str(self.taker_rate),
            "funding_rate": str(self.funding_rate),
            "settlements_in_max_hold": self.settlements_in_max_hold,
        }


def entry_reserve(notional: Decimal, inputs: ReserveInputs) -> Decimal:
    """An entry's worst planned loss (section 3):

    ``notional * (initial_sl_pct / 100 + 2 * taker_rate)
      + notional * |funding_rate| * settlements_in_max_hold``
    """
    if notional <= 0 or inputs.initial_sl_pct <= 0:
        raise ValueError("an entry reserve needs a positive notional and stop distance")
    if inputs.taker_rate < 0 or inputs.settlements_in_max_hold < 1:
        raise ValueError("an entry reserve needs a taker rate and at least one settlement")
    return (
        notional * (inputs.initial_sl_pct / 100 + 2 * inputs.taker_rate)
        + notional * abs(inputs.funding_rate) * inputs.settlements_in_max_hold
    )


def admits(
    *, realized_closed: Decimal, reserved_open: Decimal, reserve: Decimal, threshold: Decimal
) -> bool:
    """``-realized_closed + reserved_open + reserve <= L``."""
    return -realized_closed + reserved_open + reserve <= threshold
