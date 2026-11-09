from __future__ import annotations

from typing import TYPE_CHECKING

from schurfer_analytics import bybit_liquid_top10_pilot as lt
from schurfer_analytics.bybit_compressed_spring import DAY_MS, Features
from schurfer_analytics.bybit_squeeze_continuation import _day

if TYPE_CHECKING:
    from pathlib import Path

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


def test_poisson_lower_bound() -> None:
    assert lt.poisson_lower(0) == 0.0
    assert abs(lt.poisson_lower(10) - 5.4254) < 1e-3
    assert abs(lt.poisson_lower(1) - 0.05129) < 1e-4


def test_match_keeps_the_instrument_window_and_activity_caliper() -> None:
    from .test_bybit_absorbed_buying import T, member

    event = member("E", z1=0.3, z4=0.1, qv=0.6)
    near = member("E", qb=0.5, z1=0.35, z4=0.1, qv=0.65, d=T + 2 * DAY_MS)
    busy = member("E", qb=0.5, z1=0.3, z4=0.1, qv=0.75, d=T + DAY_MS)
    inside_episode = member("E", qb=0.5, z1=0.3, z4=0.1, qv=0.6, d=T + 8 * 3_600_000)
    too_far = member("E", qb=0.5, z1=0.3, z4=0.1, qv=0.6, d=T + 8 * DAY_MS)
    other = member("X", qb=0.5, z1=0.3, z4=0.1, qv=0.6)
    pairs = lt.match([event], {"E": [near, busy, inside_episode, too_far], "X": [other]})
    assert [c.d for c in pairs[event.key]] == [near.d]


def test_concentration_weighs_clusters_equally() -> None:
    from .test_bybit_absorbed_buying import T, member

    events = [member("A", d=T), member("A", d=T + 3_600_000), member("B", d=T + 2 * DAY_MS)]
    shape = lt.concentration(events)
    assert shape["symbols"] == 2 and shape["largest_symbol_share"] == 0.5
    assert shape["effective_symbols"] == 2.0


def test_counts_refuse_another_scope(tmp_path: Path) -> None:
    import json

    import pytest

    features = Features({"BTCUSDT": series()}, ())
    scope = {"version": "x", "study": lt.VERSION, "mode": "blocks", "primary": {}}
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({}))
    with pytest.raises(SystemExit, match="not this pilot"):
        lt.bound_tape(manifest, "0" * 64, scope, "s", features)


def test_build_runs_on_the_two_blocks() -> None:
    from .test_bybit_absorbed_buying import tape

    names = {f"S{k:02d}": series(phase=k, turnover=1e6 + k) for k in range(12)}
    features = Features({"BTCUSDT": series(), **names}, ())
    tapes = {s: tape(buy=1.0 + k / 10) for k, s in enumerate(names)}
    report, frozen = lt.build(features, tapes)
    assert report["funnel"]["member_hours"] == 10 * 28 * 24
    assert report["verdict"] in ("pass", "fail", "indeterminate")
    assert len(frozen["events"]) == report["overall"]["events"]
