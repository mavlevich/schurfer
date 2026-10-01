from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from schurfer_analytics import mexc_early_trigger_hyp029 as hyp029
from schurfer_analytics import preblind_book_cost_baseline as baseline
from schurfer_analytics import research_cost_power_planning as plan
from schurfer_analytics import source_lead_gap_hyp012c as hyp012c
from schurfer_analytics import source_lead_multi_source as hyp012b
from schurfer_analytics import source_lead_multi_source_report as report_io

REPO = Path(__file__).resolve().parents[3]
MINUTE_MS = 60_000


def _write(root: Path, relative: str, payload: Any, *, sidecar: str | None = None) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        json.dumps(payload, sort_keys=True).encode() if not isinstance(payload, bytes) else payload
    )
    path.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    path.with_name(path.name + ".sha256").write_text((sidecar or digest) + "\n")
    return digest


def _spec(key: str, relative: str, registered: str | None = None) -> plan.ArtifactSpec:
    return plan.ArtifactSpec(key, relative, registered, "test")


# --- artifacts ---------------------------------------------------------------------------


def test_missing_artifact_is_an_explicit_gap(tmp_path: Path) -> None:
    loaded = plan.load_artifact(tmp_path, _spec("x", "a/result.json"))
    assert loaded.status == "missing"
    assert loaded.fingerprint()["sha256"] is None


@pytest.mark.parametrize(
    ("payload", "sidecar", "registered", "message"),
    [
        ({"a": 1}, "0" * 64, None, "does not match its sidecar"),
        ({"a": 1}, None, "f" * 64, "not the registered artifact"),
        (b"{not json", None, None, "not valid JSON"),
        (b"[1, 2]", None, None, "not a JSON object"),
    ],
)
def test_corrupt_artifacts_are_refused(
    tmp_path: Path, payload: Any, sidecar: str | None, registered: str | None, message: str
) -> None:
    _write(tmp_path, "a/result.json", payload, sidecar=sidecar)
    with pytest.raises(plan.ArtifactIntegrityError, match=message):
        plan.load_artifact(tmp_path, _spec("x", "a/result.json", registered))


def test_artifact_without_digest_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "a/result.json", {"a": 1})
    (tmp_path / "a/result.json.sha256").unlink()
    with pytest.raises(plan.ArtifactIntegrityError, match=r"no \.sha256 sidecar"):
        plan.load_artifact(tmp_path, _spec("x", "a/result.json"))


def test_registered_digests_are_the_published_ones() -> None:
    ledger = (REPO / "docs/research/discovery-ledger.md").read_text(encoding="utf-8")
    readout = (REPO / "docs/research/preblind-book-cost-baseline-v1-readout.md").read_text(
        encoding="utf-8"
    )
    for spec in plan.ARTIFACTS:
        if spec.registered_sha256 is not None:
            assert spec.registered_sha256 in ledger + readout, spec.key


# --- trading thresholds ------------------------------------------------------------------


def _paper_row(spread: float, ask: float, bid: float) -> dict[str, Any]:
    at = datetime(2026, 8, 10, tzinfo=UTC)
    return {
        "paper_version": "v1",
        "exchange": "bybit",
        "requested_notional_usd": 50.0,
        "entry_status": "opened",
        "position_status": "closed",
        "entry_quote_observed_at": at,
        "exit_quote_observed_at": at + timedelta(minutes=30),
        "entry_exchange_event_at": at,
        "exit_exchange_event_at": at + timedelta(minutes=30),
        "entry_spread_bps": spread,
        "entry_impact_bps": ask,
        "exit_impact_bps": bid,
        "entry_filled_notional_usd": 50.0,
        "exit_filled_notional_usd": 50.0,
    }


def test_threshold_reuses_the_registered_formula_and_never_adds_the_spread() -> None:
    narrow = baseline.summarize_cost_rows([_paper_row(1.0, 5.0, 6.0)], [])
    wide = baseline.summarize_cost_rows([_paper_row(40.0, 5.0, 6.0)], [])
    [n], [w] = plan.trading_thresholds(narrow)["groups"], plan.trading_thresholds(wide)["groups"]
    for fee in baseline.FEE_SCENARIOS_BPS:
        expected = baseline.break_even_mid_move_bps(5.0, 6.0, fee)
        assert n["break_even_bps"][f"fee_{fee:g}"]["mean"] == pytest.approx(expected)
        assert w["break_even_bps"][f"fee_{fee:g}"]["mean"] == pytest.approx(expected)
    assert n["entry_spread_bucket"] != w["entry_spread_bucket"]
    assert n["break_even_bps"]["fee_0"]["mean"] == pytest.approx(11.0, abs=0.01)


def test_only_usd50_capacity_is_measured() -> None:
    thresholds = plan.trading_thresholds(baseline.summarize_cost_rows([_paper_row(1, 5, 6)], []))
    assert thresholds["capacity"] == [
        {"notional_usd": 50.0, "status": "measured_book_quotes"},
        {"notional_usd": 500.0, "status": "capacity_not_measured"},
        {"notional_usd": 5_000.0, "status": "capacity_not_measured"},
    ]
    missing = plan.trading_thresholds(None)
    assert missing["status"] == "missing"
    assert {c["status"] for c in missing["capacity"]} == {"capacity_not_measured"}


def test_unobserved_costs_are_additive_scenarios_in_bps() -> None:
    rows = plan.unobserved_cost_scenarios()
    row = next(r for r in rows if r["hold_minutes"] == 60 and r["funding_bps_per_8h"] == 5.0)
    assert row["funding_bps"] == pytest.approx(5.0 * 60 / 480)
    assert {r["quote_to_fill_bps"] for r in rows} == {0.0, 15.0, 30.0}


# --- dataset replays ---------------------------------------------------------------------

T0 = int(datetime(2026, 9, 2, tzinfo=UTC).timestamp())


def _leg(symbol: str, close_t: int, exit_close: float) -> dict[str, Any]:
    entry_ms = (close_t + hyp029.ENTRY_DELAY) * 1000
    exit_ms = entry_ms + (hyp029.HOLD_MINUTES - 1) * MINUTE_MS
    return {
        "symbol": symbol,
        "close_t": close_t,
        "mexc_close": 1.0,
        "route": symbol.replace("_", ""),
        "route_status": "route",
        "delivery_ms": 0,
        "bybit_bars": [
            [str(entry_ms), "1.0", "1.0", "1.0", "1.0", "1"],
            [str(exit_ms), "1.0", "1.0", "1.0", str(exit_close), "1"],
        ],
    }


def _hyp029(
    legs: list[dict[str, Any]], net_mean_pct: float
) -> tuple[dict[str, Any], dict[str, Any]]:
    inputs = {"family_version": hyp029.FAMILY_VERSION, "legs": legs}
    return inputs, {"legs": len(legs), "net_mean_pct_primary_cost": net_mean_pct}


def test_hyp029_replay_is_in_net_bps_and_must_match_the_published_mean() -> None:
    legs = [_leg("AAA_USDT", T0, 1.02), _leg("BBB_USDT", T0 + 3_600, 0.99)]
    inputs, result = _hyp029(legs, ((2.0 - 0.4) + (-1.0 - 0.4)) / 2)
    episodes = plan.hyp029_episodes(inputs, result)
    assert [round(e.net_bps, 6) for e in episodes] == [160.0, -140.0]
    with pytest.raises(plan.ArtifactIntegrityError, match="replay differs"):
        plan.hyp029_episodes(inputs, {**result, "net_mean_pct_primary_cost": 0.3})
    with pytest.raises(plan.ArtifactIntegrityError, match="published count"):
        plan.hyp029_episodes(inputs, {**result, "legs": 3})


def _candidate(event_id: int, source: str, cluster: str, price: float) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "base": cluster,
        "cluster_key": cluster,
        "source_exchange": source,
        "source_at": datetime(2026, 9, 1, 10, 0, 30, tzinfo=UTC).isoformat(),
        "bybit_native_id": f"{cluster}USDT",
        "source_price": price,
    }


def _bars(exit_close: float) -> list[list[str]]:
    entry_ms = int(datetime(2026, 9, 1, 10, 1, tzinfo=UTC).timestamp() * 1000)
    reference_ms = entry_ms - 2 * MINUTE_MS
    exit_ms = entry_ms + hyp012b.HORIZON_MINUTES * MINUTE_MS
    return [
        [str(reference_ms), "1", "1", "1", "1.0", "1"],
        [str(entry_ms), "1.0", "1", "1", "1", "1"],
        [str(exit_ms), "1", "1", "1", str(exit_close), "1"],
    ]


def test_hyp012c_replays_only_in_band_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    candidates = [
        _candidate(1, "gate", "AAA", 1.01),  # 1% gap: in band
        _candidate(2, "mexc", "BBB", 1.015),  # in band
        _candidate(3, "gate", "CCC", 1.05),  # 5%: out of band, must stay unread
        _candidate(4, "coinex", "DDD", 1.01),  # not a HYP-012c source
    ]
    bars = {"1": _bars(1.01), "2": _bars(1.0), "3": _bars(1.5), "4": _bars(1.5)}
    inputs = {"stage": "holdout", "candidates": candidates, "bars": bars}
    cost = hyp012b.round_trip_cost_pct(31)
    result = {
        "family_version": hyp012c.FAMILY_VERSION,
        "pooled": {"resolved": 2, "mean_net_pct": ((1.0 - cost) + (0.0 - cost)) / 2},
    }
    evaluated: list[int] = []
    original = report_io.outcome_for

    def recording(candidate: Any, raw: Any, fetched: bool) -> Any:
        evaluated.append(candidate.event_id)
        return original(candidate, raw, fetched)

    monkeypatch.setattr(plan, "outcome_for", recording)
    episodes = plan.hyp012c_episodes(inputs, result)
    assert evaluated == [1, 2]
    assert [e.cluster for e in episodes] == ["AAA", "BBB"]
    assert episodes[0].net_bps == pytest.approx((1.0 - cost) * 100)


def test_hyp012b_pools_formal_venues_only_and_checks_each_venue() -> None:
    candidates = [
        _candidate(1, "blofin", "AAA", 1.0),
        _candidate(2, "blofin", "BBB", 1.0),
        _candidate(3, "coinex", "CCC", 1.0),
    ]
    bars = {"1": _bars(1.01), "2": _bars(0.99), "3": _bars(2.0)}
    cost = hyp012b.round_trip_cost_pct(31)
    formal = list(hyp012b.FORMAL_SOURCES)
    published: list[dict[str, Any]] = [
        {"source": s, "resolved": 0, "mean_net_pct": None} for s in formal if s != "blofin"
    ]
    published.append({"source": "blofin", "resolved": 2, "mean_net_pct": -cost})
    result = {
        "family_version": hyp012b.FAMILY_VERSION,
        "stage": "discovery",
        "tested_family": formal,
        "formal_results": published,
    }
    inputs = {"stage": "discovery", "candidates": candidates, "bars": bars}
    episodes = plan.hyp012b_episodes(inputs, result)
    assert sorted(e.cluster for e in episodes) == ["AAA", "BBB"]
    published[-1]["resolved"] = 3
    with pytest.raises(plan.ArtifactIntegrityError, match="resolved differs"):
        plan.hyp012b_episodes(inputs, result)


# --- statistics and power ----------------------------------------------------------------


def _episodes(values_by_cluster: dict[str, list[float]]) -> list[plan.Episode]:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    out = []
    for index, (cluster, values) in enumerate(sorted(values_by_cluster.items())):
        for k, value in enumerate(values):
            out.append(plan.Episode(cluster, start + timedelta(hours=index * 7 + k), value))
    return out


def test_singleton_clusters_have_no_design_effect() -> None:
    rng = random.Random(7)  # noqa: S311
    episodes = _episodes({f"A{i:03d}": [rng.gauss(0, 100)] for i in range(50)})
    summary = plan.dispersion_summary(episodes)
    assert summary["by_cluster_scheme"]["asset"]["design_effect"] == pytest.approx(1.0)


def test_repeats_within_a_cluster_inflate_the_requirement() -> None:
    rng = random.Random(11)  # noqa: S311
    repeated = {f"A{i:02d}": [rng.gauss(0, 100)] * 4 for i in range(25)}
    summary = plan.dispersion_summary(_episodes(repeated))
    asset = summary["by_cluster_scheme"]["asset"]
    assert asset["intraclass_correlation"] == pytest.approx(1.0)
    assert asset["design_effect"] == pytest.approx(4.0, rel=0.05)
    power = plan.dataset_power(
        _episodes(repeated),
        label="repeats",
        replicates=100,
        calibration_replicates=0,
        calibration_iterations=100,
        seed=1,
    )
    sizes = [row["episodes_for_50bps_80pct"] for row in power["repeat_sensitivity"]]
    assert sizes == sorted(sizes) and sizes[-1] > sizes[0]


def test_independent_sample_size_formula() -> None:
    expected = math.ceil(((1.959964 + 0.841621) * 100 / 50) ** 2)
    assert plan.independent_sample_size(100.0, 50.0, 0.8) == expected
    with pytest.raises(ValueError, match="invalid"):
        plan.independent_sample_size(100.0, 0.0, 0.8)


def _normal_cells(clusters: int, per: int, sd: float, seed: int) -> list[plan.ClusterCell]:
    rng = random.Random(seed)  # noqa: S311
    rows = [(f"A{i:03d}", rng.gauss(0, sd)) for i in range(clusters) for _ in range(per)]
    mean = sum(v for _, v in rows) / len(rows)
    return plan.cluster_cells([(k, v - mean) for k, v in rows])


def test_zero_effect_pass_rate_stays_near_alpha_and_power_rises() -> None:
    cells = _normal_cells(200, 2, 100.0, seed=3)
    curve = plan.power_curve(cells, label="null", replicates=400, seed=5, sizes=(100, 400))
    for row in curve:
        assert row["pass_rate"]["0"] < 0.06
        rates = [row["pass_rate"][f"{e:g}"] for e in plan.EFFECT_GRID_BPS]
        assert rates == sorted(rates)
    assert curve[1]["pass_rate"]["25"] > curve[0]["pass_rate"]["25"]
    required = plan.required_from_curve(curve, 25.0, 0.8)
    assert required is not None and required["episodes"] >= plan.EVIDENCE_FLOOR_RESOLVED


def test_draw_stops_at_the_first_cluster_that_reaches_the_target() -> None:
    cells = [plan.ClusterCell(0.0, 3, (0.0, 0.0, 0.0)), plan.ClusterCell(0.0, 1, (0.0,))]
    rng = random.Random(1)  # noqa: S311
    for target in (1, 5, 17, 100):
        drawn = plan._draw(cells, target, rng)
        total = sum(cells[i].count for i in drawn)
        assert target <= total < target + 3
        assert total - cells[drawn[-1]].count < target


def test_simulation_is_reproducible_with_its_seed() -> None:
    cells = _normal_cells(40, 3, 50.0, seed=9)
    first = plan.simulate_cohorts(cells, 120, 50, seed=42)
    assert first == plan.simulate_cohorts(cells, 120, 50, seed=42)
    assert first != plan.simulate_cohorts(cells, 120, 50, seed=43)


def test_bootstrap_calibration_agrees_with_the_linearized_rule() -> None:
    cells = _normal_cells(60, 2, 80.0, seed=13)
    calibration = plan.calibrate_against_bootstrap(
        cells, target_n=100, replicates=30, iterations=300, seed=2, label="cal"
    )
    for row in calibration["by_effect_bps"].values():
        assert row["agreement"] >= 0.9


def test_too_few_clusters_gives_no_power_estimate() -> None:
    episodes = _episodes({f"A{i}": [10.0 * i, -5.0 * i] for i in range(10)})
    power = plan.dataset_power(
        episodes,
        label="few",
        replicates=100,
        calibration_replicates=5,
        calibration_iterations=100,
        seed=1,
    )
    assert power["simulation"]["asset"]["status"] == "unavailable"
    assert power["bootstrap_calibration"] == []
    assert plan.conservative_required(power, 50.0, 0.8) is None


# --- calendar and economics --------------------------------------------------------------


def test_zero_flow_never_completes_and_needs_no_threshold() -> None:
    assert plan.calendar_days(100, 0.0, 0.9) is None
    entries = plan.executable_entries_per_month(
        0.0, resolved_fraction=0.9, rejection_fraction=0.2, max_concurrent=1, hold_minutes=60
    )
    assert entries == 0.0
    assert plan.required_net_bps(10.0, 0.0, entries, 50.0) is None


def test_units_bps_percent_and_dollars() -> None:
    assert plan.monthly_result_usd(50.0, 40.0, 50.0) == pytest.approx(10.0)
    assert plan.required_net_bps(10.0, 0.0, 40.0, 50.0) == pytest.approx(50.0)
    assert plan.calendar_days(180, 2.0, 0.9) == pytest.approx(100.0)


def test_concurrency_blocking_uses_erlang_b() -> None:
    assert plan.erlang_b(1, 0.5) == pytest.approx(0.5 / 1.5)
    assert plan.erlang_b(3, 0.0) == 0.0
    one = plan.executable_entries_per_month(
        24.0, resolved_fraction=1.0, rejection_fraction=0.0, max_concurrent=1, hold_minutes=60
    )
    assert one == pytest.approx(24.0 * (1 - 0.5) * plan.DAYS_PER_MONTH)


def test_accrual_reference_requires_the_published_counters(tmp_path: Path) -> None:
    missing = plan.accrual_reference(tmp_path / "audit.md")
    assert missing["status"] == "missing" and missing["flow_per_day"] is None
    doc = tmp_path / "audit.md"
    doc.write_text("header\n" + plan.AUDIT_ROW_MARKER + "\n")
    verified = plan.accrual_reference(doc)
    assert verified["flow_per_day"] == pytest.approx(42 / 22.8333, rel=1e-4)
    doc.write_text("edited counters\n")
    with pytest.raises(plan.ArtifactIntegrityError, match="counters"):
        plan.accrual_reference(doc)


# --- end to end --------------------------------------------------------------------------


def _artifact_root(tmp_path: Path) -> tuple[Path, list[plan.ArtifactSpec]]:
    root = tmp_path / "research"
    rng = random.Random(21)  # noqa: S311
    legs = [
        _leg(f"S{i:02d}_USDT", T0 + i * 86_400 // 2, 1.0 + rng.gauss(0.004, 0.004))
        for i in range(48)
    ]
    gross = [hyp029.leg_return(leg, hyp029.ENTRY_DELAY)[0] for leg in legs]
    episodes = [g - hyp029.COST_PRIMARY_PCT for g in gross if g is not None]
    inputs, result = _hyp029(legs, sum(episodes) / len(episodes))
    result["inputs_sha256"] = _write(root, "hyp029/inputs.json", inputs)
    _write(root, "hyp029/result.json", result)
    preblind = json.loads(
        json.dumps(baseline.summarize_cost_rows([_paper_row(1.0, 5.0, 6.0)], []), default=str)
    )
    _write(root, "preblind-book-cost-baseline/result.json", preblind)
    specs = [plan.ArtifactSpec(s.key, s.relative_path, None, s.provenance) for s in plan.ARTIFACTS]
    return root, specs


def test_report_is_reproducible_and_names_what_is_missing(tmp_path: Path) -> None:
    root, specs = _artifact_root(tmp_path)
    audit = tmp_path / "audit.md"
    audit.write_text(plan.AUDIT_ROW_MARKER + "\n")

    def build() -> dict[str, Any]:
        return plan.build_report(
            root,
            audit,
            code_revision="abc",
            working_tree_dirty=False,
            replicates=100,
            calibration_replicates=2,
            calibration_iterations=100,
            artifact_specs=specs,
        )

    report = build()
    assert plan.report_body(report) == plan.report_body(build())
    assert report["power"]["hyp029_september"]["status"] == "available"
    assert report["power"]["hyp012b_discovery_formal"]["status"] == "unavailable"
    assert any("hyp012c" in item for item in report["missing_measurements"])
    assert report["trading_thresholds"]["status"] == "verified"
    assert any(row["flow_per_day"] == 0.0 for row in report["economics_usd50"])
    markdown = plan.render_markdown(report)
    assert "capacity_not_measured" in markdown and "Missing measurements" in markdown


def test_report_refuses_a_result_that_pins_other_inputs(tmp_path: Path) -> None:
    root, specs = _artifact_root(tmp_path)
    result = json.loads((root / "hyp029/result.json").read_text())
    (root / "hyp029/result.json.sha256").unlink()
    _write(root, "hyp029/result.json", {**result, "inputs_sha256": "0" * 64})
    with pytest.raises(plan.ArtifactIntegrityError, match="pins another inputs"):
        plan.build_report(
            root,
            tmp_path / "missing.md",
            code_revision="abc",
            working_tree_dirty=True,
            replicates=100,
            calibration_replicates=0,
            artifact_specs=specs,
        )


def test_sizes_with_few_clusters_per_cohort_neither_qualify_nor_bind() -> None:
    cells = _normal_cells(40, 10, 50.0, seed=17)
    curve = plan.power_curve(cells, label="dense", replicates=200, seed=3, sizes=(100, 200, 400))
    assert [row["evaluable"] for row in curve][:2] == [False, True]
    required = plan.required_from_curve(curve, 100.0, 0.8)
    assert required is not None
    assert required["episodes"] == 200 and required["censored"] is True
    block = {
        "simulation": {
            "asset": {"status": "simulated", "required": {"100": {"0.8": {
                "episodes": 150, "censored": False, "simulated_power": 0.85,
                "monte_carlo_se": 0.01, "clusters_drawn": 60.0,
            }}}},
            "utc_day": {"status": "simulated", "required": {"100": {"0.8": required}}},
        }
    }  # fmt: skip
    chosen = plan.conservative_required(block, 100.0, 0.8)
    assert chosen["scheme"] == "asset" and chosen["episodes"] == 150


def _row(size: int, evaluable: bool, rate: float) -> dict[str, Any]:
    return {
        "target_episodes": size,
        "evaluable": evaluable,
        "mean_clusters_drawn": 30.0,
        "pass_rate": {"50": rate},
        "monte_carlo_se": {"50": 0.01},
    }


def test_a_requirement_above_the_first_evaluable_size_is_identified() -> None:
    curve = [_row(100, False, 0.9), _row(125, True, 0.3), _row(600, True, 0.82)]
    required = plan.required_from_curve(curve, 50.0, 0.8)
    assert required is not None
    assert required["episodes"] == 600 and required["censored"] is False
    hit_first = plan.required_from_curve([_row(100, False, 0.9), _row(125, True, 0.85)], 50, 0.8)
    assert hit_first is not None and hit_first["censored"] is True


def test_all_censored_schemes_give_an_upper_bound() -> None:
    cell = {"episodes": 125, "censored": True, "simulated_power": 0.85}
    block = {
        "simulation": {
            "asset": {"status": "simulated", "required": {"100": {"0.8": cell}}},
            "utc_day": {
                "status": "simulated",
                "required": {"100": {"0.8": {**cell, "episodes": 1_500}}},
            },
        }
    }
    bound = plan.conservative_required(block, 100.0, 0.8)
    assert bound["censored"] is True and bound["episodes"] == 125
    assert plan._episodes(bound) == "<=125"
