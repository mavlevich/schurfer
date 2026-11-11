from __future__ import annotations

import gzip
import hashlib
import json
import math
from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics import bybit_squeeze_continuation as sq
from schurfer_analytics import bybit_squeeze_power_calibration as power
from schurfer_analytics.bybit_compressed_spring import (
    DAY_MS,
    FIRST_DECISION_MS,
    HOUR_MS,
    WINDOW_END_MS,
    Features,
    Series,
)
from schurfer_analytics.bybit_compressed_spring import VERSION as FEATURES_VERSION
from schurfer_analytics.bybit_post_funding_screen import file_sha256

if TYPE_CHECKING:
    from pathlib import Path


def synthetic_inputs(
    *, synchronized_wait: bool = False
) -> tuple[Features, list[power.Opportunity]]:
    instruments: dict[str, Series] = {}
    events = []
    for week in range(32):
        for symbol in range(40):
            native = f"S{symbol:02d}USDT"
            d = FIRST_DECISION_MS + week * 7 * DAY_MS + symbol * HOUR_MS
            events.append((native, d, symbol % 2 == 0))
            series = instruments.setdefault(native, Series(0, WINDOW_END_MS - 1))
            individual = 0.01 if symbol % 4 < 2 else -0.01
            common = 0.01 if week % 2 else -0.01
            for name, offset, span in power.PROXIES:
                end = d - offset * HOUR_MS
                value = common if synchronized_wait and name == "waiting_bar" else individual
                series.bars[end - (span + 1) * HOUR_MS] = (100.0, 100.0, 100.0, 1e6)
                close = 100 * (1 + value)
                series.bars[end - HOUR_MS] = (close, close, close, 1e6)
    return Features(instruments, ()), events


def test_waiting_bar_correlation_controls_maximum_without_changing_four_hour_scale() -> None:
    features, events = synthetic_inputs(synchronized_wait=True)
    base = {"A": 0.001, "B": 0.002}
    report = power.calibrate(features, events, base, iterations=100)
    for name in ("A", "B"):
        waiting = report["proxies"]["waiting_bar"][name]["design_effect"]
        earlier = [
            p[name]["design_effect"]
            for label, p in report["proxies"].items()
            if label != "waiting_bar"
        ]
        assert waiting > max(earlier)
        candidate = report["candidates"][name]
        assert candidate["calibrated_design_effect"] == waiting
        assert candidate["mde"] == pytest.approx(base[name] * math.sqrt(waiting / 1.2))
    assert report["proxies"]["waiting_bar"]["span_hours"] == 1
    assert report["proxies"]["pre_0h"]["span_hours"] == 4
    assert len(report["proxies"]) == 8


@pytest.mark.parametrize(
    ("base", "a", "b"),
    [
        ({"A": 0.005, "B": 0.00501}, "planning_gate_pass", "insufficient_power"),
        ({"A": 0.00501, "B": 0.005}, "insufficient_power", "planning_gate_pass"),
    ],
)
def test_unrounded_limit_stops_only_the_failing_candidate(
    base: dict[str, float],
    a: str,
    b: str,
) -> None:
    features, events = synthetic_inputs()
    report = power.calibrate(features, events, base, iterations=100)
    assert report["candidates"]["A"]["verdict"] == a
    assert report["candidates"]["B"]["verdict"] == b
    failed = "A" if a == "insufficient_power" else "B"
    assert report["candidates"][failed]["next_action"] == "stop_candidate_without_outcomes"


def test_one_missing_original_week_cannot_be_dropped_to_improve_calibration() -> None:
    features, events = synthetic_inputs()
    for symbol, d, _ in events[:40]:
        del features.instruments[symbol].bars[d - HOUR_MS]
    report = power.calibrate(features, events, {"A": 0.001, "B": 0.002}, iterations=100)
    waiting = report["proxies"]["waiting_bar"]["A"]
    assert waiting["checks"]["complete"] is True
    assert waiting["missing_weeks"]
    assert report["candidates"]["A"]["verdict"] == "not_estimable"
    assert report["candidates"]["A"]["next_action"] == "block_candidate_without_outcomes"


def test_prices_after_decision_do_not_change_calibration() -> None:
    features, events = synthetic_inputs()
    base = {"A": 0.001, "B": 0.002}
    before = power.calibrate(features, events, base, iterations=100)
    for symbol, d, _ in events:
        features.instruments[symbol].bars[d] = (1e6, 1e6, 1e6, 1e12)
    assert power.calibrate(features, events, base, iterations=100) == before


def test_degenerate_proxy_is_unestimable_without_fallback_to_assumed_power() -> None:
    features, events = synthetic_inputs()
    for series in features.instruments.values():
        series.bars = {stamp: (100.0, 100.0, 100.0, 1e6) for stamp in series.bars}
    report = power.calibrate(features, events, {"A": 0.001, "B": 0.002}, iterations=100)
    assert report["candidates"]["A"]["verdict"] == "not_estimable"
    assert report["candidates"]["B"]["verdict"] == "not_estimable"
    assert all(proxy["A"]["design_effect"] is None for proxy in report["proxies"].values())


def test_economic_gate_power_is_distinct_from_detecting_nonzero_mean() -> None:
    features, events = synthetic_inputs()
    report = power.calibrate(features, events, {"A": 0.001, "B": 0.00492285}, iterations=100)
    a = report["candidates"]["A"]["normal_approximation_power"]["at_30_bps"]
    b = report["candidates"]["B"]["normal_approximation_power"]["at_30_bps"]
    assert a["detect_zero"] > 0.99
    assert a["pass_both_net_conditions"] == pytest.approx(0.5)
    assert b["detect_zero"] == pytest.approx(0.40, abs=0.01)


def test_registration_freezes_eighth_proxy_and_failure_actions() -> None:
    contract = power.calibration_contract()
    assert len(contract["proxies"]) == 8
    assert contract["proxies"][-1] == {
        "name": "waiting_bar",
        "end_offset_hours": 0,
        "span_hours": 1,
    }
    assert contract["above_limit"] == "stop_candidate_without_outcomes"
    assert contract["unestimable"] == "block_candidate_without_outcomes"
    assert contract["mde_limit"] == 0.005


def test_report_emits_dispersion_without_individual_proxies_or_means() -> None:
    features, events = synthetic_inputs()
    report = power.calibrate(features, events, {"A": 0.001, "B": 0.002}, iterations=100)

    def check_keys(value: Any) -> None:
        if isinstance(value, dict):
            assert not ({"mean", "proxy_mean", "net", "returns", "values"} & value.keys())
            for child in value.values():
                check_keys(child)

    check_keys(report)


def registered_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    features, events = synthetic_inputs(synchronized_wait=True)
    snapshot = tmp_path / "features.jsonl.gz"
    catalog = [
        {
            "symbol": symbol,
            "contractType": "LinearPerpetual",
            "settleCoin": "USDT",
            "launchTime": 0,
            "deliveryTime": 0,
        }
        for symbol in features.instruments
    ]
    rows: list[dict[str, Any]] = [
        {"kind": "version", "symbol": None, "response": {"version": FEATURES_VERSION}},
        {"kind": "catalog", "symbol": None, "response": {"list": catalog}},
    ]
    for symbol, series in features.instruments.items():
        bars = [
            [stamp, close, high, low, close, 1, turnover]
            for stamp, (high, low, close, turnover) in series.bars.items()
        ]
        rows.append(
            {
                "kind": "kline_60",
                "symbol": symbol,
                "response": {"result": {"symbol": symbol, "list": bars}},
            }
        )
    with gzip.open(snapshot, "wt") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")
    monkeypatch.setattr(sq, "FEATURES_SHA256", file_sha256(snapshot))
    preflight = tmp_path / "preflight"
    preflight.mkdir()
    run = preflight / "run.json"
    run.write_text("{}\n")
    manifest = {
        "run_sha256": file_sha256(run),
        "events": [
            {"symbol": symbol, "decision_ms": d, "in_B": member} for symbol, d, member in events
        ],
    }
    body = json.dumps(manifest).encode()
    frozen = preflight / "events.json.gz"
    frozen.write_bytes(gzip.compress(body, mtime=0))
    counts = preflight / "counts.json"
    counts.write_text(
        json.dumps(
            {
                "hypothesis_id": "HYP-037",
                "run_sha256": file_sha256(run),
                "events_sha256": hashlib.sha256(body).hexdigest(),
                "candidates": {
                    name: {"shape": {"impulses": n}, "power": {"mde": mde}}
                    for name, n, mde in (("A", len(events), 0.001), ("B", len(events) // 2, 0.002))
                },
            }
        )
    )
    monkeypatch.setattr(power, "COUNTS_SHA256", file_sha256(counts))
    monkeypatch.setattr(power, "EVENTS_GZIP_SHA256", file_sha256(frozen))
    protocol = tmp_path / "protocol.md"
    protocol.write_text("Synthetic calibration protocol\n")
    registration = tmp_path / "registration.json"
    registration.write_text(
        json.dumps(
            {"contract": power.calibration_contract(), "protocol_sha256": file_sha256(protocol)}
        )
    )
    monkeypatch.setattr(
        power,
        "_source_binding",
        lambda **kwargs: {
            "code_revision": "a" * 40,
            "working_tree_dirty": False,
            "working_tree_status": [],
            "source_sha256": {"synthetic.py": "b" * 64},
        },
    )
    return {
        "features_path": snapshot,
        "preflight_dir": preflight,
        "registration_path": registration,
        "registration_sha256": file_sha256(registration),
        "protocol_path": protocol,
        "output_dir": tmp_path / "output",
    }


def test_registered_reader_loads_real_snapshot_and_seals_calibration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = registered_inputs(tmp_path, monkeypatch)
    report = power.run_calibration(**args)
    output = args["output_dir"]
    assert report["run_sha256"] == file_sha256(output / "run.json")
    assert json.loads((output / "calibration.json").read_text()) == report
    assert report["sources"]["working_tree_dirty"] is False
    assert report["proxies"]["waiting_bar"]["A"]["defined"] == 1280


def test_dirty_revision_refuses_before_reading_features(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = registered_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(power, "_source_binding", lambda **kwargs: {"working_tree_dirty": True})

    def forbidden(path: Path) -> Features:
        pytest.fail("dirty revision reached features loader")

    monkeypatch.setattr(power, "load_features", forbidden)
    with pytest.raises(ValueError, match="clean committed"):
        power.run_calibration(**args)
    assert not args["output_dir"].exists()


def test_changed_failure_rule_is_refused_before_reading_features(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = registered_inputs(tmp_path, monkeypatch)
    path = args["registration_path"]
    payload = json.loads(path.read_text())
    payload["contract"]["above_limit"] = "read_anyway"
    path.write_text(json.dumps(payload))
    args["registration_sha256"] = file_sha256(path)
    with pytest.raises(ValueError, match="contract mismatch"):
        power.run_calibration(**args)
    assert not args["output_dir"].exists()
