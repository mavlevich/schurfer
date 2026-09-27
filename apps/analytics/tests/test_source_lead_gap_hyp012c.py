from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics import source_lead_gap_hyp012c as g
from schurfer_analytics import source_lead_multi_source as m
from schurfer_analytics import source_lead_multi_source_report as r
from schurfer_analytics.ohlcv import ONE_MINUTE_MS

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 9, 30, tzinfo=UTC)
START, _ = m.stage_window("holdout")


def _candidate(event_id: int, source: str, *, source_price: float = 1.01) -> m.Candidate:
    at = START + timedelta(days=event_id % 20, hours=1, seconds=20)
    base = f"A{event_id}"
    return m.Candidate(event_id, base, base, source, at, f"{base}USDT", source_price)


def _rows(candidate: m.Candidate, *, reference_close: float, move_pct: float) -> list[list[str]]:
    rows = []
    ts = candidate.reference_ms
    while ts <= candidate.exit_bar_ms:
        if ts == candidate.reference_ms:
            price = reference_close
        elif ts == candidate.exit_bar_ms:
            price = 1.0 + move_pct / 100
        else:
            price = 1.0
        rows.append([str(ts), str(price), str(price), str(price), str(price), "1"])
        ts += ONE_MINUTE_MS
    return rows


@pytest.mark.parametrize(
    ("source_price", "status"),
    [
        (1.006, "in_band"),  # the lower bound is inclusive
        (1.0199, "in_band"),
        (1.02, "out_of_band"),  # the upper bound is exclusive
        (1.005, "out_of_band"),
        (0.99, "out_of_band"),
    ],
)
def test_the_band_uses_the_pre_signal_gap_only(source_price: float, status: str) -> None:
    candidate = _candidate(1, "mexc", source_price=source_price)
    # The exit move is irrelevant to the band: it is decided before the entry.
    for move in (-50.0, 0.0, 50.0):
        rows = _rows(candidate, reference_close=1.0, move_pct=move)
        assert g.band_status(candidate, rows) == status


def test_a_missing_reference_or_fetch_is_its_own_status() -> None:
    candidate = _candidate(1, "mexc")
    assert g.band_status(candidate, None) == "bar_fetch_failed"
    rows = _rows(candidate, reference_close=1.0, move_pct=0.0)[1:]  # no reference bar
    assert g.band_status(candidate, rows) == "no_gap"


def _result(**kw: Any) -> m.VenueResult:
    values: dict[str, Any] = {
        "source": g.HYPOTHESIS,
        "candidates": 150,
        "resolved": 120,
        "assets": 40,
        "max_week_share": 0.3,
        "mean_net_pct": 0.5,
        "ci_lower_pct": 0.1,
        "ci_upper_pct": 0.9,
        "p_value": 0.01,
        "unresolved": {},
    }
    values.update(kw)
    return m.VenueResult(**values)


@pytest.mark.parametrize(
    ("overrides", "unknown", "unresolved", "verdict"),
    [
        ({}, 0.0, 0.0, "candidate"),
        ({"p_value": 0.2}, 0.0, 0.0, "fail"),
        ({"mean_net_pct": -0.1, "p_value": 0.01}, 0.0, 0.0, "fail"),
        ({"assets": 20, "mean_net_pct": -0.2}, 0.0, 0.0, "fail"),  # mature negative first
        ({"assets": 20}, 0.0, 0.0, "insufficient_data"),
        ({"max_week_share": 0.5}, 0.0, 0.0, "insufficient_data"),
        ({"resolved": 60, "mean_net_pct": -0.2}, 0.0, 0.0, "insufficient_data"),
        ({"p_value": None, "mean_net_pct": None}, 0.0, 0.0, "insufficient_data"),
        # the missingness ceilings block a positive verdict...
        ({}, 0.0501, 0.0, "insufficient_data"),
        ({}, 0.0, 0.0501, "insufficient_data"),
        ({}, 0.05, 0.05, "candidate"),  # ...at the ceiling itself it still passes
        # ...but never rescue a mature negative result
        ({"mean_net_pct": -0.3}, 0.2, 0.2, "fail"),
    ],
)
def test_the_verdict_order(
    overrides: dict[str, Any], unknown: float, unresolved: float, verdict: str
) -> None:
    result = _result(**overrides)
    assert (
        g.verdict(result, unknown_gap_fraction=unknown, unresolved_in_band_fraction=unresolved)
        == verdict
    )


def _read(stage_dir: Path, now: datetime = NOW, revision: str = "rev-1") -> dict[str, Any]:
    return g.read(stage_dir, now, reader_code_revision=revision, reader_working_tree_dirty=False)


def _inputs(specs: list[tuple[str, float, float]]) -> dict[str, Any]:
    """(source, source_price with reference close 1.0, exit move %) per episode."""
    candidates, bars = [], {}
    for event_id, (source, price, move) in enumerate(specs, start=1):
        c = _candidate(event_id, source, source_price=price)
        candidates.append(r._candidate_json(c))
        bars[str(event_id)] = _rows(c, reference_close=1.0, move_pct=move)
    return {
        "family_version": g.FAMILY_VERSION,
        "stage": "holdout",
        "window": [x.isoformat() for x in m.stage_window("holdout")],
        "code_revision": "abc",
        "funnel": {},
        "route_identity_excluded": [],
        "candidates": candidates,
        "bars": bars,
    }


def test_only_in_band_formal_candidates_are_evaluated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    specs = [("mexc", 1.01, 3.0)] * 5 + [("mexc", 1.05, 3.0)] * 3 + [("toobit", 1.01, 3.0)] * 2
    stage_dir = tmp_path / "holdout"
    stage_dir.mkdir()
    r.write_once(stage_dir / r.INPUTS_NAME, _inputs(specs))
    evaluated: list[int] = []
    real = r.outcome_for

    def spy(candidate: m.Candidate, raw: Any, fetched: bool) -> m.Outcome:
        evaluated.append(candidate.event_id)
        return real(candidate, raw, fetched)

    monkeypatch.setattr(g, "outcome_for", spy)
    payload = _read(stage_dir)
    assert sorted(evaluated) == [1, 2, 3, 4, 5]
    assert payload["band_funnel"] == {"in_band": 5, "other_source": 2, "out_of_band": 3}
    assert payload["pooled"]["resolved"] == 5
    assert payload["verdict"] == "insufficient_data"
    assert payload["cost_function_version"] == g.COST_FUNCTION_VERSION
    assert payload["cost_pct_per_trade"] == pytest.approx(0.40322916666666664)
    claim = json.loads((stage_dir / r.CLAIM_NAME).read_text())
    assert claim["tested_family"] == [g.HYPOTHESIS]
    assert claim["family_version"] == g.FAMILY_VERSION
    assert claim["contract_sha256"] == g.contract_sha256()
    assert claim["reader_code_revision"] == "rev-1"
    assert payload["reader_code_revision"] == "rev-1"
    assert payload["inputs_code_revision"] == "abc"
    # Descriptive rows carry no test.
    for row in payload["descriptive_by_source"]:
        assert "p_value" not in row and "ci_lower_pct" not in row
    mexc = next(row for row in payload["descriptive_by_source"] if row["source"] == "mexc")
    assert (mexc["in_band"], mexc["resolved"]) == (5, 5)
    with pytest.raises(r.AlreadyReadError):
        _read(stage_dir)


def test_the_read_is_refused_before_the_holdout_matures(tmp_path: Path) -> None:
    stage_dir = tmp_path / "holdout"
    stage_dir.mkdir()
    r.write_once(stage_dir / r.INPUTS_NAME, _inputs([("mexc", 1.01, 3.0)]))
    with pytest.raises(ValueError, match="can run from"):
        _read(stage_dir, datetime(2026, 9, 28, 12, tzinfo=UTC))
    assert not (stage_dir / r.CLAIM_NAME).exists()


def test_hyp012b_inputs_are_not_accepted_as_hyp012c_inputs(tmp_path: Path) -> None:
    stage_dir = tmp_path / "holdout"
    stage_dir.mkdir()
    r.write_once(
        stage_dir / r.INPUTS_NAME,
        {**_inputs([("mexc", 1.01, 3.0)]), "family_version": m.FAMILY_VERSION},
    )
    with pytest.raises(ValueError, match="does not hold holdout inputs of this family"):
        _read(stage_dir)


def _stage(tmp_path: Path, inputs: dict[str, Any]) -> Path:
    stage_dir = tmp_path / "holdout"
    stage_dir.mkdir()
    r.write_once(stage_dir / r.INPUTS_NAME, inputs)
    return stage_dir


def test_a_resumed_read_with_another_reader_or_contract_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_dir = _stage(tmp_path, _inputs([("mexc", 1.01, 3.0)] * 3))
    first = _read(stage_dir)
    (stage_dir / r.RESULT_NAME).unlink()  # crash after the claim, before the result
    (stage_dir / f"{r.RESULT_NAME}.sha256").unlink()
    with pytest.raises(ValueError, match="another reader_code_revision"):
        _read(stage_dir, revision="rev-2")
    monkeypatch.setitem(g.CONTRACT, "band", [0.005, 0.02])
    with pytest.raises(ValueError, match="another contract_sha256"):
        _read(stage_dir)
    monkeypatch.undo()
    assert _read(stage_dir) == first


def test_missingness_counts_identity_exclusions_and_unknown_gaps_by_source_and_week(
    tmp_path: Path,
) -> None:
    inputs = _inputs([("mexc", 1.01, 3.0)] * 4 + [("gate", 1.01, 3.0)])
    inputs["bars"]["5"] = None  # a failed fetch: band membership unknown
    week = m.Candidate(1, "A1", "A1", "mexc", START + timedelta(hours=1), "X", 1.0).week
    inputs["route_identity_excluded"] = [
        {"source": "mexc", "week": week, "reason": "missing_reference_bar"},
        {"source": "mexc", "week": week, "reason": "price_level_mismatch"},  # gap known
        {"source": "toobit", "week": week, "reason": "no_source_price"},  # not a source
    ]
    payload = _read(_stage(tmp_path, inputs))
    miss = payload["missingness"]
    # eligible: 5 candidates + 2 mexc identity exclusions; unknown: 1 missing ref + 1 fetch
    assert (miss["eligible"], miss["unknown_gap"]) == (7, 2)
    assert miss["unknown_gap_fraction"] == pytest.approx(2 / 7)
    assert sum(c["eligible"] for c in miss["by_source_week"]["mexc"].values()) == 6
    assert sum(c["unknown_gap"] for c in miss["by_source_week"]["gate"].values()) == 1
    assert payload["verdict"] == "insufficient_data"
