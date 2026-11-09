from __future__ import annotations

from schurfer_analytics import bybit_absorbed_buying as v1
from schurfer_analytics import bybit_next_candidate_feasibility as nc
from schurfer_analytics.bybit_compressed_spring import (
    HOUR_MS,
    WINDOW_END_MS,
    WINDOW_FIRST_MS,
    Features,
    Series,
)

from .test_bybit_absorbed_buying import T, member, series

T0 = T + 10 * 24 * HOUR_MS


def pumped(rise: float, oi_after: float, fall_after: int = 99) -> Series:
    """Flat at 100 with OI 1000, a rise to 100 * (1 + rise) in the hour ending T0, then
    flat; OI is oi_after from T0 + 2 h, and the price falls to 100 from T0 + fall_after h."""
    s = Series(0, WINDOW_END_MS - 1)
    for start in range(WINDOW_FIRST_MS, WINDOW_END_MS, HOUR_MS):
        t = start + HOUR_MS
        price = 100.0 * (1 + rise) if T0 <= t < T0 + fall_after * HOUR_MS else 100.0
        s.bars[start] = (price, price, price, 1e6)
        s.oi[start] = oi_after if start >= T0 + 2 * HOUR_MS else 1000.0
    return s


def test_a_pump_with_an_oi_turn_is_an_event_at_the_turn() -> None:
    features = Features({"BTCUSDT": series(), "A": pumped(0.3, 900.0)}, ())
    pools = v1.pools_of(features)
    assert nc.pumps(features, pools, 0.2) == [("A", T0)]
    hours = nc.episode(features, pools, "A", T0, 0.05)
    assert hours is not None and [h.k for h in hours] == [1, 2]
    assert hours[-1].turned and hours[-1].kept == 1.0
    assert abs(hours[-1].size - 0.3) < 1e-9


def test_no_event_without_a_turn_or_after_the_pump_is_given_back() -> None:
    features = Features({"BTCUSDT": series(), "A": pumped(0.3, 990.0, fall_after=1),
                         "B": pumped(0.3, 1000.0)}, ())  # fmt: skip
    pools = v1.pools_of(features)
    given_back = nc.episode(features, pools, "A", T0, 0.05)
    assert given_back == []  # the first hour after the pump already lost it
    flat = nc.episode(features, pools, "B", T0, 0.05)
    assert flat is not None and len(flat) == nc.MAX_K and not any(h.turned for h in flat)


def test_own_match_keeps_the_instrument_and_the_window() -> None:
    event = member("E", z1=0.3, z4=0.1)
    near = member("E", qb=0.5, z1=0.35, z4=0.1, d=T + 2 * 24 * HOUR_MS)
    episode = member("E", qb=0.5, z1=0.3, z4=0.1, d=T + 3 * HOUR_MS)
    far = member("E", qb=0.5, z1=0.3, z4=0.1, d=T + 8 * 24 * HOUR_MS)
    other = member("X", qb=0.5, z1=0.3, z4=0.1)
    pairs = nc.own_match([event], [near, episode, far, other])
    assert [c.d for c in pairs[event.key]] == [near.d]
