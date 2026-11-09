"""The LIVE_PROBE loss and entry reserve arithmetic
(docs/engineering/live-probe-loss-threshold-design-v1.md, sections 1 to 3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from schurfer_execution import probe_loss as pl

D = Decimal
T = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)


def _long(**overrides: object) -> pl.OpenPosition:
    fields: dict[str, object] = {
        "side": "long",
        "entry_vwap": D("1.00"),
        "open_qty": D("50"),
        "realized_open_parts": D(0),
        "fees_paid_open": D(0),
        "funding_paid_open": D(0),
    }
    fields.update(overrides)
    return pl.OpenPosition(**fields)  # type: ignore[arg-type]


def test_no_open_position_needs_no_mark() -> None:
    loss = pl.probe_loss(D("-3.5"))

    assert loss.loss == D("3.5")
    assert not loss.lower_bound


def test_an_open_position_without_a_fresh_mark_is_refused() -> None:
    with pytest.raises(ValueError, match="fresh mark"):
        pl.probe_loss(D(0), _long(), mark=None, taker_rate=D("0.00055"))


def test_a_partial_close_counts_its_realized_part() -> None:
    """Long 50 at 1.00, 20 closed at 0.90, mark 1.00: the loss before costs is 2.00."""
    vwap = pl.entry_vwap([pl.Fill(D(50), D("1.00"))])
    realized = pl.realized_open_parts("long", vwap, [pl.Fill(D(20), D("0.90"))])
    position = _long(open_qty=D(30), realized_open_parts=realized)

    loss = pl.probe_loss(D(0), position, mark=D("1.00"), taker_rate=D(0))

    assert realized == D("-2.00")
    assert loss.loss == D("2.00")


def test_entry_vwap_is_amount_weighted() -> None:
    assert pl.entry_vwap([pl.Fill(D(10), D(1)), pl.Fill(D(30), D(2))]) == D("1.75")


def test_a_short_gains_when_the_price_falls() -> None:
    vwap = D(2)
    assert pl.realized_open_parts("short", vwap, [pl.Fill(D(5), D("1.5"))]) == D("2.5")
    position = _long(side="short", entry_vwap=vwap, open_qty=D(5))
    assert pl.probe_loss(D(0), position, mark=D("1.5"), taker_rate=D(0)).loss == D("-2.5")


def test_l_is_reached_as_the_mark_falls() -> None:
    threshold = D(5)
    position = _long()

    above = pl.probe_loss(D(-1), position, mark=D("0.95"), taker_rate=D(0))
    crossed = pl.probe_loss(D(-1), position, mark=D("0.92"), taker_rate=D(0))

    assert above.loss == D("3.50") and not above.reached(threshold)
    assert crossed.loss == D("5.00") and crossed.reached(threshold)
    assert crossed.lower_bound, "an open position's funding tail is never proven"


def test_the_exit_fee_estimate_can_tip_the_loss_over_l() -> None:
    position = _long()
    before_fee = pl.probe_loss(D(0), position, mark=D("0.90"), taker_rate=D(0))
    with_fee = pl.probe_loss(D(0), position, mark=D("0.90"), taker_rate=D("0.001"))

    assert before_fee.loss == D("5.00") and not before_fee.reached(D("5.03"))
    assert with_fee.terms["exit_fee_estimate"] == D("0.04500")
    assert with_fee.reached(D("5.03"))


def test_fees_and_listed_funding_add_to_the_loss() -> None:
    position = _long(fees_paid_open=D("0.03"), funding_paid_open=D("0.02"))
    assert pl.probe_loss(D(0), position, mark=D(1), taker_rate=D(0)).loss == D("0.05")


def test_a_rebate_lowers_the_loss() -> None:
    position = _long(fees_paid_open=D("-0.01"))
    assert pl.probe_loss(D(0), position, mark=D(1), taker_rate=D(0)).loss == D("-0.01")


def test_a_short_pays_a_negative_rate_at_the_held_quantity_and_the_bar_high() -> None:
    settlement = pl.UnlistedSettlement(at=T, rate=D("-0.001"), quantity_held=D(40), mark=D("1.10"))
    position = _long(side="short", open_qty=D(30), unlisted=(settlement,))

    loss = pl.probe_loss(D(0), position, mark=D(1), taker_rate=D(0))

    assert loss.terms["funding_unlisted_estimate"] == D("0.04400")
    assert loss.loss == D("0.04400")


def test_a_long_receiving_on_a_negative_rate_counts_zero() -> None:
    settlement = pl.UnlistedSettlement(at=T, rate=D("-0.001"), quantity_held=D(50), mark=D(1))
    position = _long(unlisted=(settlement,))

    loss = pl.probe_loss(D(0), position, mark=D(1), taker_rate=D(0))

    assert loss.terms["funding_unlisted_estimate"] == 0
    assert loss.loss == 0


def test_a_partial_exit_after_a_settlement_uses_the_quantity_held_then() -> None:
    """50 held at the settlement, 20 closed after it: the estimate is on 50."""
    settlement = pl.UnlistedSettlement(at=T, rate=D("0.001"), quantity_held=D(50), mark=D(1))
    position = _long(open_qty=D(30), unlisted=(settlement,))

    loss = pl.probe_loss(D(0), position, mark=D(1), taker_rate=D(0))

    assert loss.terms["funding_unlisted_estimate"] == D("0.050")


def test_an_unreadable_settlement_is_unknown_never_estimated_from_now() -> None:
    known = pl.UnlistedSettlement(at=T, rate=D("0.001"), quantity_held=D(50), mark=D(1))
    no_rate = pl.UnlistedSettlement(
        at=T + timedelta(hours=1), rate=None, quantity_held=D(50), mark=D(1)
    )
    no_mark = pl.UnlistedSettlement(
        at=T + timedelta(hours=2), rate=D("0.001"), quantity_held=D(50), mark=None
    )
    position = _long(unlisted=(known, no_rate, no_mark))

    loss = pl.probe_loss(D(0), position, mark=D(1), taker_rate=D(0))

    assert loss.terms["funding_unlisted_estimate"] == D("0.050")
    assert loss.unknown_settlements == (T + timedelta(hours=1), T + timedelta(hours=2))
    assert loss.lower_bound


def test_the_mark_decides_only_within_60_seconds() -> None:
    assert pl.mark_is_fresh(T, T + timedelta(seconds=60))
    assert not pl.mark_is_fresh(T, T + timedelta(seconds=61))
    assert not pl.mark_is_fresh(T, T - timedelta(seconds=1)), "a read from the future"


@pytest.mark.parametrize(
    ("max_hold", "interval", "expected"),
    [
        (timedelta(minutes=720), timedelta(hours=8), 3),
        (timedelta(minutes=720), timedelta(hours=1), 13),
        (timedelta(hours=8), timedelta(hours=8), 2),
    ],
)
def test_settlements_in_the_maximum_hold(
    max_hold: timedelta, interval: timedelta, expected: int
) -> None:
    assert pl.settlements_in_max_hold(max_hold, interval) == expected


def test_the_entry_reserve() -> None:
    inputs = pl.ReserveInputs(
        initial_sl_pct=D(5),
        taker_rate=D("0.00055"),
        funding_rate=D("-0.0001"),
        settlements_in_max_hold=3,
    )
    # 10 x (0.05 + 0.0011) + 10 x 0.0001 x 3
    assert pl.entry_reserve(D(10), inputs) == D("0.514")


def test_a_reserve_without_a_stop_or_with_a_negative_taker_rate_is_refused() -> None:
    with pytest.raises(ValueError, match="stop distance"):
        pl.entry_reserve(D(10), pl.ReserveInputs(D(0), D("0.00055"), D(0), 1))
    with pytest.raises(ValueError, match="taker rate"):
        pl.entry_reserve(D(10), pl.ReserveInputs(D(5), D("-0.0001"), D(0), 1))


def test_admission_holds_at_l_and_refuses_above() -> None:
    assert pl.admits(realized_closed=D(-2), reserved_open=D(1), reserve=D(2), threshold=D(5))
    assert not pl.admits(
        realized_closed=D(-2), reserved_open=D(1), reserve=D("2.01"), threshold=D(5)
    )
