from __future__ import annotations

from schurfer_analytics import bybit_liquid_top10_pilot as lt
from schurfer_analytics.bybit_compressed_spring import DAY_MS, Features
from schurfer_analytics.bybit_squeeze_continuation import _day

from .test_bybit_absorbed_buying import series


def test_blocks_are_fixed_and_apart_from_the_hyp036_pilot() -> None:
    assert [(_day(a), _day(b)) for a, b in lt.BLOCKS] == [
        ("2024-11-18", "2024-12-01"),
        ("2025-04-14", "2025-04-27"),
    ]
    assert lt.LEAD_D == 22


def test_scope_covers_history_and_the_control_window_of_the_top_ten() -> None:
    instruments = {
        "BTCUSDT": series(),
        **{f"S{k:02d}": series(turnover=1e6 + k) for k in range(12)},
    }
    days = lt.scope_days(Features(instruments, ()))
    assert len(days) == lt.TOP and "S00" not in days and "BTCUSDT" not in days
    first, last = lt.BLOCKS[0]
    assert days["S11"][0] == _day(first - 22 * DAY_MS)
    assert _day(last + 7 * DAY_MS) in days["S11"]
    assert _day(last + 8 * DAY_MS) not in days["S11"]
    assert lt.seen_overlap(days) == 0
