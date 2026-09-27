from __future__ import annotations

from typing import Any

import pytest
from schurfer_analytics import mexc_early_trigger_screen as s

T0 = int(s.WINDOW_START.timestamp()) - s.DAY_BARS * s.BAR  # one day of history first


def _bars(n: int, **at: dict[str, float]) -> list[dict[str, float]]:
    """Flat price 1.0 and turnover 10k, with per-index overrides."""
    out = []
    for i in range(n):
        bar: dict[str, float] = {"t": T0 + i * s.BAR, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0}
        bar["a"] = 10_000.0
        bar.update(at.get(str(i), {}))
        out.append(bar)
    return out


N = s.DAY_BARS * 3
SPIKE = s.DAY_BARS + 10  # inside the window, after a full day of history


def _spike(ret: float = 0.06, turnover: float = 100_000.0) -> dict[str, float]:
    return {"o": 1.0, "c": 1.0 + ret, "h": 1.0 + ret, "a": turnover}


def test_a_volume_backed_early_move_is_one_trigger_with_next_bar_entry() -> None:
    bars = _bars(N, **{str(SPIKE): _spike()})
    bars[SPIKE + 1]["o"] = 1.06
    eps, _ = s.episodes_for("AAA_USDT", bars, 0.05, 10.0)
    assert [e.t for e in eps] == [bars[SPIKE]["t"]]
    assert eps[0].entry == pytest.approx(1.06)  # the open of the bar after the trigger


@pytest.mark.parametrize(
    ("override", "why"),
    [
        (_spike(ret=0.04), "below the return threshold"),
        (_spike(turnover=40_000.0), "below 5x the 24h median turnover"),
        (_spike(turnover=15_000.0), "below the absolute turnover floor"),
    ],
)
def test_filters_reject(override: dict[str, float], why: str) -> None:
    bars = _bars(N, **{str(SPIKE): override})
    assert s.episodes_for("AAA_USDT", bars, 0.05, 10.0)[0] == [], why


def test_an_already_pumped_coin_is_not_early() -> None:
    bars = _bars(N, **{str(SPIKE): _spike()})
    for i in range(SPIKE - 50, SPIKE + 1):
        bars[i]["o"] = bars[i]["c"] = 1.2  # +20% over the prior 24h before the trigger
    bars[SPIKE]["c"] = 1.2 * 1.06
    assert s.episodes_for("AAA_USDT", bars, 0.05, 10.0)[0] == []


def test_cooldown_keeps_one_trigger_per_symbol_per_day() -> None:
    overrides: dict[str, Any] = {str(SPIKE): _spike(), str(SPIKE + 5): _spike()}
    eps, _ = s.episodes_for("AAA_USDT", _bars(N, **overrides), 0.05, 10.0)
    assert len(eps) == 1


def test_outcomes_never_cross_the_window_end_and_need_no_extra_bar() -> None:
    end_index = (int(s.WINDOW_END.timestamp()) - T0) // s.BAR
    # The latest trigger whose every outcome bar (the delayed entry's 24h close included)
    # still closes before the window end: last needed bar index = i + 1 + LONGEST.
    latest = end_index - 2 - s.LONGEST
    ok = _bars(end_index + 400, **{str(latest): _spike()})
    assert len(s.episodes_for("AAA_USDT", ok, 0.05, 10.0)[0]) == 1
    late = _bars(end_index + 400, **{str(latest + 1): _spike()})
    assert s.episodes_for("AAA_USDT", late, 0.05, 10.0)[0] == []
    # Exactly the needed bars, none after: still one trigger (no extra bar is required).
    trimmed = _bars(latest + 2 + s.LONGEST, **{str(latest): _spike()})
    assert len(s.episodes_for("AAA_USDT", trimmed, 0.05, 10.0)[0]) == 1


def test_continuation_and_returns_are_measured_from_the_entry() -> None:
    bars = _bars(N, **{str(SPIKE): _spike()})
    bars[SPIKE + 1]["o"] = 1.0
    bars[SPIKE + 20]["h"] = 1.2  # +20% within 6h of the entry
    bars[SPIKE + 12]["c"] = 1.1  # the 1h close (entry bar + 11)
    [e], _ = s.episodes_for("AAA_USDT", bars, 0.05, 10.0)
    assert e.continued
    assert e.returns["1h"] == pytest.approx(10.0)


def test_the_delayed_entry_is_reported_one_bar_later() -> None:
    bars = _bars(N, **{str(SPIKE): _spike()})
    bars[SPIKE + 1]["o"] = 1.0
    bars[SPIKE + 2]["o"] = 1.05
    bars[SPIKE + 13]["c"] = 1.155  # the delayed entry's 1h close
    [e], _ = s.episodes_for("AAA_USDT", bars, 0.05, 10.0)
    assert e.delayed_returns["1h"] == pytest.approx(10.0)


def test_eligible_bars_are_the_frequency_denominator() -> None:
    _, eligible = s.episodes_for("AAA_USDT", _bars(N), 0.05, 10.0)
    _, eligible_with_trigger = s.episodes_for(
        "AAA_USDT", _bars(N, **{str(SPIKE): _spike()}), 0.05, 10.0
    )
    assert eligible > 0
    assert eligible == eligible_with_trigger  # triggers never change the denominator


def _episode(symbol: str, i: int, turnover: float) -> s.Episode:
    return s.Episode(
        symbol=symbol,
        i=i,
        t=T0 + i * s.BAR,
        entry=1.0,
        prior_median_turnover=turnover,
        returns={},
        delayed_returns={},
        continued=False,
        scanner_reach=False,
        mae_4h=0.0,
    )


def test_matched_controls_share_symbol_week_and_turnover_and_are_never_reused() -> None:
    trig = _episode("AAA_USDT", SPIKE, 10_000.0)
    pool = {
        "AAA_USDT": [
            _episode("AAA_USDT", SPIKE + 10, 10_000.0),  # inside the cooldown
            _episode("AAA_USDT", SPIKE + s.COOLDOWN, 50_000.0),  # turnover 5x off
            _episode("AAA_USDT", SPIKE + s.COOLDOWN + 1, 15_000.0),  # the match
        ],
        "BBB_USDT": [_episode("BBB_USDT", SPIKE + s.COOLDOWN, 10_000.0)],
    }
    [control] = s.matched_controls([trig], pool)
    assert (control.symbol, control.i) == ("AAA_USDT", SPIKE + s.COOLDOWN + 1)
    again = _episode("AAA_USDT", SPIKE + 1, 10_000.0)
    assert len(s.matched_controls([trig, again], pool)) == 1  # a control is used once
