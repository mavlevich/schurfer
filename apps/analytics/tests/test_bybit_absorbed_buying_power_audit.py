from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics import bybit_absorbed_buying as ab
from schurfer_analytics import bybit_absorbed_buying_power_audit as audit
from schurfer_analytics.bybit_compressed_spring import DAY_MS, HOUR_MS, Features, Series
from schurfer_analytics.bybit_squeeze_continuation import _iso

if TYPE_CHECKING:
    from pathlib import Path


def payload(rows: list[tuple[str, int]]) -> dict[str, Any]:
    return {
        "version": ab.VERSION,
        "mode": "blocks",
        "events": [{"symbol": s, "t": _iso(t), "controls": []} for s, t in rows],
    }


def test_metadata_cannot_change_scope_or_duplicate_events() -> None:
    t = ab.BLOCKS[0][0]
    with pytest.raises(ValueError, match="duplicate"):
        audit.read_events(payload([("X", t), ("X", t)]))
    with pytest.raises(ValueError, match="outside"):
        audit.read_events(payload([("X", t - HOUR_MS)]))
    with pytest.raises(ValueError, match="off-hour"):
        audit.read_events(payload([("X", t + 1)]))
    bad = payload([("X", t)])
    bad["events"][0]["t"] = "2025-01-13T00:00:00"
    with pytest.raises(ValueError, match="aware UTC"):
        audit.read_events(bad)


def test_projection_and_extra_variance_do_not_create_observed_information() -> None:
    t = ab.BLOCKS[0][0]
    weights = {("X", t): 1.0}
    scales = {("X", t): 0.03}
    rhos = {ab.month_of(t): 0.4}
    result = audit.model(weights, scales, rhos, 28)
    own = (1.645 + 0.842) * 1.5 * math.sqrt(1.2) * 0.03 * 10_000
    assert result["observed_sample_mde_bps"] == pytest.approx(own)
    projected = own * math.sqrt(28 / 334)
    assert result["discovery_projection_mde_bps"] == pytest.approx(projected)
    assert result["extra_variance_scenarios_bps"]["4.0"] == pytest.approx(2 * projected)
    assert result["extra_variance_to_reach_30_bps"] == pytest.approx((30 / projected) ** 2)


def synthetic_features() -> Features:
    instruments = {}
    spans = ((ab._ms(2024, 12, 1), ab.BLOCKS[0][1]), (ab._ms(2025, 6, 1), ab.BLOCKS[1][1]))
    for i, symbol in enumerate([ab.BENCHMARK, *(f"S{k}" for k in range(30))]):
        series = Series(0, ab.WINDOW_END_MS - 1)
        for a, b in spans:
            for t in range(a, b, HOUR_MS):
                k = t // HOUR_MS
                price = math.exp(0.02 * math.sin(k / 11) + 0.01 * math.sin(k / (i + 2)))
                series.bars[t] = (price, price, price, 1e6 + i)
        instruments[symbol] = series
    return Features(instruments, ())


def synthetic_events() -> list[ab.Member]:
    days = [ab.BLOCKS[0][0] + k * DAY_MS for k in range(14)]
    days += [ab.BLOCKS[1][0] + k * DAY_MS for k in range(11)]
    rows = [(f"S{i}", d + 4 * HOUR_MS) for d in days for i in range(30)]
    rows += [(s, t + HOUR_MS) for s, t in rows[:34]]
    return audit.read_events(payload(rows))


def historical(features: Features, events: list[ab.Member]) -> dict[str, Any]:
    months = {ab.month_of(e.d) for e in events}
    rhos = {m: ab.month_rho(features, m, ab.pool(features, m), relative=False) for m in months}
    old = ab.power(features, events, {}, {"a": rhos, "b": rhos}, 28 / 334)
    return {"decision_days": 28, "power": old}


def test_audit_reproduces_real_original_path_and_keeps_exam_unread() -> None:
    features, events = synthetic_features(), synthetic_events()
    result = audit.build(features, events, historical(features, events))
    assert result["historical_a_net_reproduced"]
    assert result["sample"]["events"] == 784
    assert result["sample"]["symbol_day_clusters"] == 750
    assert result["sample"]["iso_weeks"] == 4
    assert result["exam_power"] is None
    assert result["exam_assigned"] is False
    assert result["status"] == "planning_only_requires_design_decision"
    raw = result["legs"]["raw_short_price_proxy"]["combined"]
    adjusted = result["legs"]["btc_adjusted_short_proxy"]["combined"]
    assert adjusted["discovery_projection_mde_bps"] != raw["discovery_projection_mde_bps"]
    assert result["strategy_outcomes_computed"] is False


def test_changed_baseline_fails_closed() -> None:
    features, events = synthetic_features(), synthetic_events()
    old = historical(features, events)
    old["power"]["a_net"]["mde_unrounded"] *= 2
    with pytest.raises(ValueError, match="not reproduced"):
        audit.build(features, events, old)


@pytest.mark.parametrize("relative", [False, True])
def test_scale_and_month_inputs_do_not_read_future_bars(relative: bool) -> None:
    features = synthetic_features()
    t = ab.BLOCKS[0][0]
    asset, btc = features.instruments["S0"], features.instruments[ab.BENCHMARK]

    def measure() -> tuple[float | None, float]:
        return (
            ab.scale(asset, btc, t, relative=relative),
            ab.month_rho(features, ab.month_of(t), ["S0", "S1"], relative=relative),
        )

    before = measure()
    for series in features.instruments.values():
        for start in list(series.bars):
            if start >= t:
                series.bars[start] = (100.0, 100.0, 100.0, 999.0)
    assert measure() == before


def test_missing_scale_cannot_turn_into_a_favorable_substitution() -> None:
    features, events = synthetic_features(), synthetic_events()
    old = historical(features, events)
    features.instruments["S0"].bars.clear()
    with pytest.raises(ValueError, match="no substitution"):
        audit.build(features, events, old)


def test_cli_rejects_dirty_code_before_opening_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit, "_source_binding", lambda **kw: {"working_tree_dirty": True})
    with pytest.raises(SystemExit, match="commit contract"):
        audit.main(
            [
                "--features",
                "absent",
                "--counts",
                "absent",
                "--events",
                "absent",
                "--output",
                "absent",
            ]
        )


def test_cli_rejects_wrong_snapshot_before_loading_prices(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(audit, "_source_binding", lambda **kw: {"working_tree_dirty": False})
    wrong = tmp_path / "wrong.gz"
    wrong.write_bytes(b"not the registered discovery snapshot")
    with pytest.raises(SystemExit, match="sha256"):
        audit.main(
            [
                "--features",
                str(wrong),
                "--counts",
                "absent",
                "--events",
                "absent",
                "--output",
                str(tmp_path / "output.json"),
            ]
        )
    assert not (tmp_path / "output.json").exists()
