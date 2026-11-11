from __future__ import annotations

import gzip
import json
import math
import random
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics import bybit_squeeze_continuation as sq
from schurfer_analytics import bybit_squeeze_policy as policy
from schurfer_analytics.bybit_compressed_spring import (
    DAY_MS,
    FIRST_DECISION_MS,
    HOUR_MS,
    WINDOW_FIRST_MS,
    Features,
    Series,
)
from schurfer_analytics.bybit_compressed_spring import (
    VERSION as FEATURES_VERSION,
)
from schurfer_analytics.bybit_post_funding_screen import file_sha256
from schurfer_analytics.bybit_squeeze_continuation import Impulse
from schurfer_analytics.clustered_inference import percentile

if TYPE_CHECKING:
    from pathlib import Path

T = FIRST_DECISION_MS + 5 * DAY_MS
D = T + HOUR_MS


def synthetic_features() -> Features:
    instruments = {}
    for symbol in ("BTCUSDT", "AUSDT", "CUSDT", "UUSDT"):
        series = Series(0, D + DAY_MS)
        price = 100.0
        for index, start in enumerate(range(WINDOW_FIRST_MS, series.end_ms, HOUR_MS)):
            is_impulse = symbol != "BTCUSDT" and start + HOUR_MS == T
            price *= 1.05 if is_impulse else (1.004 if index % 2 else 0.996)
            series.bars[start] = (price, price, price, 200_000.0)
            series.oi[start] = 100.0
        series.oi[T] = 120.0
        if symbol != "UUSDT":
            series.funding[D - 3 * HOUR_MS] = 0.0001 if symbol == "CUSDT" else -0.0001
        instruments[symbol] = series
    return Features(instruments, ())


def population() -> tuple[Impulse, ...]:
    """Forty symbols over 48 weeks in both discovery halves, without real data."""
    return tuple(
        Impulse(
            f"S{symbol:02d}USDT",
            FIRST_DECISION_MS + week * 7 * DAY_MS + symbol * HOUR_MS,
            5.0,
            0.0,
            5_000_000.0,
            "true" if (week + symbol) % 2 == 0 else "false",
            "true",
        )
        for week in range(48)
        for symbol in range(40)
    )


def test_synthetic_scanner_preflight_and_evaluation_use_normal_entry_points() -> None:
    report, events = policy.build_preflight(synthetic_features(), first_d=D, last_d=D)
    assert [(e.symbol, e.cell) for e in events] == [
        ("AUSDT", "event"),
        ("CUSDT", "g_only"),
        ("UUSDT", "undefined"),
    ]
    assert report["stage"] == "preflight_only"
    assert report["candidates"]["A"]["shape"]["impulses"] == 3
    assert report["candidates"]["B"]["shape"]["impulses"] == 1
    assert report["candidates"]["A"]["peak_unconstrained_holds"] == 3
    assert report["fg_determinable_share"] == pytest.approx(2 / 3)
    assert report["candidates"]["B"]["data_checks"]["fg_determinable"] is False
    assert report["candidates"]["B"]["power"]["tail"] == 0.025
    assert "mean_net" not in str(report)
    outcomes = {e.key: policy.EventOutcome(0.02, -0.001) for e in events}
    result = policy.evaluate_events(events, outcomes, iterations=100)
    assert result["candidates"]["A"]["mean_net"] == pytest.approx(0.016)
    assert result["candidates"]["B"]["mean_net"] == pytest.approx(0.016)
    assert result["candidates"]["A"]["verdict"] == "insufficient_data"


def test_future_bars_and_oi_do_not_change_preflight_at_the_decision() -> None:
    features = synthetic_features()
    before = policy.build_preflight(features, first_d=D, last_d=D)
    for series in features.instruments.values():
        series.bars[D + HOUR_MS] = (1e6, 1e6, 1e6, 1e12)
        series.oi[D] = 1e12
        series.funding[D] = 0.1
    assert policy.build_preflight(features, first_d=D, last_d=D) == before


def test_missing_oi_keeps_baseline_event_and_excludes_only_filtered_membership() -> None:
    features = synthetic_features()
    del features.instruments["AUSDT"].oi[T]
    report, events = policy.build_preflight(features, first_d=D, last_d=D)
    assert report["candidates"]["A"]["shape"]["impulses"] == 3
    assert report["candidates"]["B"]["shape"]["impulses"] == 0
    assert next(e for e in events if e.symbol == "AUSDT").g == "oi_gap"


def test_preflight_rejects_scope_outside_discovery() -> None:
    with pytest.raises(ValueError, match="scope outside"):
        policy.build_preflight(synthetic_features(), first_d=WINDOW_FIRST_MS)


def test_both_candidates_can_pass_when_filter_adds_nothing() -> None:
    events = population()
    outcomes = {e.key: policy.EventOutcome(0.02, 0.0) for e in events}
    report = policy.evaluate_events(events, outcomes, iterations=200)
    assert report["candidates"]["A"]["verdict"] == "discovery_candidate"
    assert report["candidates"]["B"]["verdict"] == "discovery_candidate"
    assert report["filter_diagnostic"]["mean_B_minus_A"] == pytest.approx(0.0)
    assert report["filter_diagnostic"]["decides_candidate"] is False


@pytest.mark.parametrize(
    ("outside_gross", "inside_gross", "a_verdict", "b_verdict"),
    [
        (0.02, 0.0, "discovery_candidate", "fail"),
        (-0.02, 0.02, "fail", "discovery_candidate"),
        (-0.02, -0.02, "fail", "fail"),
        (0.003, 0.003, "inconclusive", "inconclusive"),
    ],
)
def test_candidate_verdicts_are_independent(
    outside_gross: float,
    inside_gross: float,
    a_verdict: str,
    b_verdict: str,
) -> None:
    events = population()
    outcomes = {
        e.key: policy.EventOutcome(inside_gross if e.cell == "event" else outside_gross, 0.0)
        for e in events
    }
    report = policy.evaluate_events(events, outcomes, iterations=200)
    assert report["candidates"]["A"]["verdict"] == a_verdict
    assert report["candidates"]["B"]["verdict"] == b_verdict


def test_undefined_funding_does_not_disqualify_baseline() -> None:
    events = tuple(replace(e, f="no_settlement") for e in population())
    outcomes = {e.key: policy.EventOutcome(0.02, 0.0) for e in events}
    report = policy.evaluate_events(events, outcomes, iterations=100)
    assert report["candidates"]["A"]["verdict"] == "discovery_candidate"
    assert report["candidates"]["B"]["mean_net"] is None
    assert report["candidates"]["B"]["lower_bound"] is None
    assert report["candidates"]["B"]["verdict"] == "insufficient_data"


def test_missing_outcomes_remain_in_intended_denominator_without_zero_fills() -> None:
    events = population()
    outcomes = {e.key: policy.EventOutcome(0.02, 0.0) for e in events[200:]}
    report = policy.evaluate_events(events, outcomes, iterations=100)
    a = report["candidates"]["A"]
    assert a["intended"] == len(events)
    assert a["complete"] == len(events) - 200
    assert a["missing_by_reason"] == {"missing_outcome": 200}
    assert a["mean_net"] == pytest.approx(0.017)
    assert a["verdict"] == "insufficient_data"


def test_named_unresolved_outcome_is_kept_in_its_candidate_denominator() -> None:
    event = population()[0]
    report = policy.evaluate_events(
        [event],
        {event.key: policy.EventOutcome(0.02, None, "funding_mark_gap")},
        iterations=100,
    )
    assert report["candidates"]["B"]["missing_by_reason"] == {"funding_mark_gap": 1}
    assert report["candidates"]["B"]["mean_net"] is None


def test_paired_week_samples_match_a_raw_row_reference_and_keep_covariance() -> None:
    rows = [("W1", 0.10, True)] + [("W2", 0.0, False)] * 20
    rng = random.Random(42)  # noqa: S311 - fixed synthetic research reference
    expected: dict[str, list[float]] = {"A": [], "B": [], "B_minus_A": []}
    for _ in range(100):
        drawn = [rng.randrange(2) for _ in range(2)]
        sampled = [row for index in drawn for row in rows if row[0] == ("W1", "W2")[index]]
        a = math.fsum(row[1] for row in sampled) / len(sampled)
        expected["A"].append(a)
        b_rows = [row[1] for row in sampled if row[2]]
        if b_rows:
            b = math.fsum(b_rows) / len(b_rows)
            expected["B"].append(b)
            expected["B_minus_A"].append(b - a)
    actual = policy.paired_week_samples(rows, iterations=100, seed=42)
    assert actual == {name: tuple(values) for name, values in expected.items()}
    assert len(actual["B"]) < 95


def test_identical_candidate_sets_have_exactly_zero_paired_difference() -> None:
    rows = [(f"W{i}", 0.01 * i, True) for i in range(12)]
    samples = policy.paired_week_samples(rows, iterations=100)
    assert samples["A"] == samples["B"]
    assert samples["B_minus_A"] == (0.0,) * 100
    assert policy.paired_week_samples(list(reversed(rows)), iterations=100) == samples


def test_absolute_bounds_use_corrected_tail_from_the_same_draws() -> None:
    events = population()
    outcomes = {e.key: policy.EventOutcome(0.01 + i % 5 * 0.002, 0.0) for i, e in enumerate(events)}
    rows = []
    for event in events:
        net = outcomes[event.key].net(30.0)
        assert net is not None
        rows.append((policy.week_key(event.d), net, event.cell == "event"))
    samples = policy.paired_week_samples(rows, iterations=100)
    report = policy.evaluate_events(events, outcomes, iterations=100)
    assert report["absolute_tail"] == 0.025
    for name in ("A", "B"):
        assert report["candidates"][name]["lower_bound"] == percentile(
            tuple(sorted(samples[name])), 0.025
        )


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True])
def test_nonfinite_or_boolean_outcomes_are_refused(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        policy.EventOutcome(value, 0.0)


def test_unresolved_outcomes_require_a_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        policy.EventOutcome(None, 0.0)


def test_funding_receipts_and_payments_have_opposite_net_effects() -> None:
    assert policy.EventOutcome(0.02, 0.001).net(30.0) == pytest.approx(0.018)
    assert policy.EventOutcome(0.02, -0.001).net(30.0) == pytest.approx(0.016)


def test_too_few_defined_bootstrap_draws_do_not_become_a_zero_bound() -> None:
    events = tuple(
        replace(event, f="true" if event.d < FIRST_DECISION_MS + 7 * DAY_MS else "false")
        for event in population()
    )
    outcomes = {e.key: policy.EventOutcome(0.02, 0.0) for e in events}
    report = policy.evaluate_events(events, outcomes, iterations=200, seed=42)
    assert report["candidates"]["A"]["verdict"] == "discovery_candidate"
    assert report["candidates"]["B"]["valid_draws"] < 190
    assert report["candidates"]["B"]["lower_bound"] is None
    assert report["filter_diagnostic"]["lower_bound"] is None


def test_missing_all_outcomes_of_a_symbol_cannot_count_it_as_resolved_evidence() -> None:
    events = population()
    outcomes = {e.key: policy.EventOutcome(0.02, 0.0) for e in events if e.symbol != "S00USDT"}
    report = policy.evaluate_events(events, outcomes, iterations=100)
    a = report["candidates"]["A"]
    assert a["data_checks"]["complete"] is True  # 39/40 is above 95%.
    assert a["data_checks"]["resolved_evidence"] is False
    assert a["verdict"] == "insufficient_data"


def test_duplicate_and_foreign_outcomes_are_refused() -> None:
    event = population()[0]
    with pytest.raises(ValueError, match="duplicate"):
        policy.evaluate_events([event, event], {}, iterations=100)
    with pytest.raises(ValueError, match="outside the frozen"):
        policy.evaluate_events([], {event.key: policy.EventOutcome(0.02, 0.0)}, iterations=100)


def test_empty_input_has_explicit_incomplete_verdicts() -> None:
    report = policy.evaluate_events([], {}, iterations=100)
    assert report["candidates"]["A"]["verdict"] == "insufficient_data"
    assert report["candidates"]["B"]["verdict"] == "insufficient_data"
    assert report["filter_diagnostic"]["mean_B_minus_A"] is None


def registered_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path, str]:
    features = synthetic_features()
    rows: list[dict[str, Any]] = [
        {"kind": "version", "symbol": None, "response": {"version": FEATURES_VERSION}},
        {
            "kind": "catalog",
            "symbol": None,
            "response": {
                "list": [
                    {
                        "symbol": symbol,
                        "contractType": "LinearPerpetual",
                        "settleCoin": "USDT",
                        "launchTime": 0,
                        "deliveryTime": series.end_ms + 1,
                    }
                    for symbol, series in features.instruments.items()
                ]
            },
        },
    ]
    for symbol, series in features.instruments.items():
        bars = [
            [start, close, high, low, close, 1, turnover]
            for start, (high, low, close, turnover) in series.bars.items()
        ]
        oi = [{"timestamp": stamp, "openInterest": value} for stamp, value in series.oi.items()]
        funding = [
            {"symbol": symbol, "fundingRateTimestamp": stamp, "fundingRate": value}
            for stamp, value in series.funding.items()
        ]
        for kind, values in (("kline_60", bars), ("oi_60", oi), ("funding", funding)):
            rows.append(
                {
                    "kind": kind,
                    "symbol": symbol,
                    "response": {"result": {"symbol": symbol, "list": values}},
                }
            )
    features_path = tmp_path / "features.jsonl.gz"
    with gzip.open(features_path, "wt") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")
    monkeypatch.setattr(sq, "FEATURES_SHA256", file_sha256(features_path))
    protocol = tmp_path / "protocol.md"
    protocol.write_text("Synthetic preflight protocol\n")
    registration = tmp_path / "registration.json"
    registration.write_text(
        json.dumps(
            {
                "contract": policy.preflight_contract(),
                "protocol_sha256": file_sha256(protocol),
            }
        )
    )
    monkeypatch.setattr(
        policy,
        "_source_binding",
        lambda: {
            "code_revision": "a" * 40,
            "working_tree_dirty": True,
            "source_sha256": {"synthetic.py": "b" * 64},
        },
    )
    return features_path, registration, protocol, file_sha256(registration)


def test_registered_cli_loads_snapshot_and_seals_linked_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    features, registration, protocol, digest = registered_fixture(tmp_path, monkeypatch)
    output = tmp_path / "result"
    policy.main(
        [
            "--features",
            str(features),
            "--registration",
            str(registration),
            "--registration-sha256",
            digest,
            "--protocol",
            str(protocol),
            "--output-dir",
            str(output),
        ]
    )
    report = json.loads((output / "counts.json").read_text())
    frozen = json.loads((output / "events.json").read_text())
    assert report["hypothesis_id"] == "HYP-037"
    assert report["stage"] == "preflight_only"
    assert report["run_sha256"] == file_sha256(output / "run.json")
    assert report["events_sha256"] == file_sha256(output / "events.json")
    assert frozen["run_sha256"] == report["run_sha256"]
    assert report["registration_sha256"] == digest
    assert report["sources"]["working_tree_dirty"] is True
    assert report["candidates"]["A"]["shape"]["impulses"] == 3
    assert report["candidates"]["B"]["shape"]["impulses"] == 1
    assert sum(event["in_B"] for event in frozen["events"]) == 1
    assert "counts_sha256=" in capsys.readouterr().out


@pytest.mark.parametrize(
    "field",
    ["hypothesis_id", "stage", "features_sha256", "window_end_exclusive_ms", "last_decision_ms"],
)
def test_altered_registration_is_refused_before_feature_loading(
    field: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    features, registration, protocol, _ = registered_fixture(tmp_path, monkeypatch)
    payload = json.loads(registration.read_text())
    payload["contract"][field] = "wrong"
    registration.write_text(json.dumps(payload))

    def forbidden_load(path: Path) -> Features:
        pytest.fail("invalid registration reached features loader")

    monkeypatch.setattr(policy, "load_features", forbidden_load)
    with pytest.raises(ValueError, match="contract"):
        policy.run_preflight(
            features_path=features,
            registration_path=registration,
            registration_sha256=file_sha256(registration),
            protocol_path=protocol,
            output_dir=tmp_path / "result",
        )
    assert not (tmp_path / "result").exists()


@pytest.mark.parametrize("modified", ["features", "protocol", "registration"])
def test_changed_input_hash_refuses_sealing(
    modified: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    features, registration, protocol, digest = registered_fixture(tmp_path, monkeypatch)
    target = {"features": features, "protocol": protocol, "registration": registration}[modified]
    with target.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(SystemExit, match="sha256"):
        policy.run_preflight(
            features_path=features,
            registration_path=registration,
            registration_sha256=digest,
            protocol_path=protocol,
            output_dir=tmp_path / "result",
        )
    assert not (tmp_path / "result").exists()


def test_source_change_during_run_leaves_only_unsealed_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    features, registration, protocol, digest = registered_fixture(tmp_path, monkeypatch)
    states = iter([{"source_sha256": "before"}, {"source_sha256": "after"}])
    monkeypatch.setattr(policy, "_source_binding", lambda: next(states))
    output = tmp_path / "result"
    with pytest.raises(ValueError, match="code changed"):
        policy.run_preflight(
            features_path=features,
            registration_path=registration,
            registration_sha256=digest,
            protocol_path=protocol,
            output_dir=output,
        )
    assert (output / "run.json").exists()
    assert not (output / "counts.json").exists()


def test_existing_output_is_refused_even_when_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    features, registration, protocol, digest = registered_fixture(tmp_path, monkeypatch)
    output = tmp_path / "result"
    output.mkdir()
    with pytest.raises(FileExistsError):
        policy.run_preflight(
            features_path=features,
            registration_path=registration,
            registration_sha256=digest,
            protocol_path=protocol,
            output_dir=output,
        )
    assert list(output.iterdir()) == []
