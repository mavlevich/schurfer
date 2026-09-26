from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics import source_lead_multi_source as m
from schurfer_analytics import source_lead_multi_source_report as r
from schurfer_analytics.ohlcv import ONE_MINUTE_MS, Candle
from schurfer_analytics.source_lead import SourceLeadEvent, SourceLeadObservation

if TYPE_CHECKING:
    from pathlib import Path

T = datetime(2026, 8, 12, 10, 0, 20, tzinfo=UTC)  # discovery window
JAN = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
LAUNCH = (m.BybitInstrument("ABCUSDT", "ABC", JAN, 0),)


def _obs(exchange: str, at: datetime, **overrides: Any) -> SourceLeadObservation:
    values: dict[str, Any] = {
        "exchange": exchange,
        "symbol": "ABC/USDT:USDT",
        "identity_key": f"{exchange}:swap:ABCUSDT:1",
        "unified_symbol": "ABC/USDT:USDT",
        "market_type": "swap",
        "base_asset": "ABC",
        "quote_asset": "USDT",
        "settle_asset": "USDT",
        "onboarded_at": None,
        "identity_conflict": False,
        "first_seen_at": at,
        "first_change_pct": 12.0,
        "first_price": 1.0,
        "first_volume_24h_usd": 1e6,
    }
    values.update(overrides)
    return SourceLeadObservation(**values)


def _event(
    event_id: int, *observations: SourceLeadObservation, base: str = "ABC"
) -> SourceLeadEvent:
    first = min(o.first_seen_at for o in observations) if observations else T
    return SourceLeadEvent(event_id, base, 1, first, None, tuple(observations))


def _build(*events: SourceLeadEvent, launch: Any = LAUNCH) -> tuple[Any, dict[str, int]]:
    return m.build_candidates(events, bybit_instruments=launch, stage="discovery", sources=["mexc"])


def test_the_candidate_set_never_requires_a_later_bybit_confirmation() -> None:
    # MEXC first, and Bybit never pumps: still a candidate (the standalone trade is at the signal).
    candidates, funnel = _build(_event(1, _obs("mexc", T)))
    assert [c.event_id for c in candidates] == [1]
    assert funnel == {"candidate:mexc": 1}


@pytest.mark.parametrize(
    ("events", "status"),
    [
        ((_event(2, _obs("mexc", T), _obs("gate", T)),), "tied_first_source"),
        ((_event(3, _obs("bybit", T), _obs("mexc", T + timedelta(minutes=1))),), "target_first"),
        ((_event(4, _obs("okx", T)),), "other_first_source"),
        ((_event(5, _obs("mexc", T, identity_conflict=True)),), "invalid_source:identity_conflict"),
        ((_event(6, _obs("mexc", T, base_asset="ZZZ"), base="ZZZ"),), "no_bybit_perp"),
        ((_event(7, _obs("mexc", datetime(2026, 9, 1, tzinfo=UTC))),), "outside_stage_window"),
    ],
)
def test_funnel_reasons(events: tuple[SourceLeadEvent, ...], status: str) -> None:
    candidates, funnel = _build(*events)
    assert candidates == () and funnel == {status: 1}


def _ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


@pytest.mark.parametrize(
    ("instruments", "status"),
    [
        # listed after the signal
        ((m.BybitInstrument("ABCUSDT", "ABC", _ms(T + timedelta(hours=1)), 0),), "not_live"),
        # delisted before the exit bar closed
        ((m.BybitInstrument("ABCUSDT", "ABC", JAN, _ms(T + timedelta(minutes=10))),), "not_live"),
        # delisted after the episode: still a route (no survivorship loss)
        ((m.BybitInstrument("ABCUSDT", "ABC", JAN, _ms(T + timedelta(days=5))),), "candidate"),
        # two contracts live over the episode: ambiguous, never guessed
        (
            (
                m.BybitInstrument("ABCUSDT", "ABC", JAN, 0),
                m.BybitInstrument("ABC2USDT", "ABC", JAN, 0),
            ),
            "ambiguous",
        ),
        # an old delisted contract next to a relisted one: only the live one routes
        (
            (
                m.BybitInstrument("ABCOLDUSDT", "ABC", 0, JAN),
                m.BybitInstrument("ABCUSDT", "ABC", JAN, 0),
            ),
            "candidate",
        ),
    ],
)
def test_the_route_is_the_single_bybit_perp_live_over_the_episode(
    instruments: tuple[m.BybitInstrument, ...], status: str
) -> None:
    candidates, funnel = _build(_event(1, _obs("mexc", T)), launch=instruments)
    expected = {
        "not_live": {"bybit_not_live_over_episode": 1},
        "ambiguous": {"ambiguous_bybit_route": 1},
        "candidate": {"candidate:mexc": 1},
    }[status]
    assert funnel == expected
    if status == "candidate":
        assert candidates[0].bybit_native_id == "ABCUSDT"


@pytest.mark.parametrize(
    ("source_price", "reference_close", "reason"),
    [
        (1.0, 1.0, None),
        (1.9, 1.0, None),  # a leading pump, same asset
        (1.0, 0.55, None),
        (2.5, 1.0, "price_level_mismatch"),  # a same-ticker different project
        (0.001, 1.0, "price_level_mismatch"),
        (None, 1.0, "no_source_price"),
        (1.0, None, "missing_reference_bar"),
    ],
)
def test_route_identity_uses_a_bar_closed_at_or_before_the_signal(
    source_price: float | None, reference_close: float | None, reason: str | None
) -> None:
    candidate = _build(_event(1, _obs("mexc", T, first_price=source_price)))[0][0]
    # The open (9.9) is deliberately far off: only the close of a finished minute counts.
    reference = (
        None if reference_close is None else _bar(candidate.reference_ms, 9.9, reference_close)
    )
    assert candidate.reference_ms + ONE_MINUTE_MS <= _ms(T) < candidate.entry_ms
    assert m.route_identity_reason(candidate, reference) == reason


def test_the_reference_bar_closes_no_later_than_a_signal_on_a_minute_boundary() -> None:
    on_boundary = datetime(2026, 8, 12, 10, 0, 0, tzinfo=UTC)
    candidate = _build(_event(1, _obs("mexc", on_boundary)))[0][0]
    assert candidate.reference_ms + ONE_MINUTE_MS <= _ms(on_boundary)


def test_an_exit_crossing_the_stage_boundary_is_excluded() -> None:
    near_end = m.HOLDOUT_START - timedelta(minutes=10)
    assert _build(_event(1, _obs("mexc", near_end)))[1] == {"exit_crosses_stage_end": 1}


def test_entry_is_the_next_minute_and_exit_is_the_bar_at_entry_plus_30() -> None:
    candidate = _build(_event(1, _obs("mexc", T)))[0][0]
    entry = datetime(2026, 8, 12, 10, 1, tzinfo=UTC)
    assert candidate.entry_ms == int(entry.timestamp() * 1000)
    assert candidate.exit_bar_ms == candidate.entry_ms + 30 * ONE_MINUTE_MS


def _bar(ts: int, open_: float, close: float) -> Candle:
    return Candle(ts, open_, max(open_, close), min(open_, close), close, 1.0)


def test_evaluate_uses_only_the_entry_and_exit_bars_and_charges_costs() -> None:
    candidate = _build(_event(1, _obs("mexc", T)))[0][0]
    outcome = m.evaluate(
        candidate, _bar(candidate.entry_ms, 1.0, 1.0), _bar(candidate.exit_bar_ms, 1.0, 1.1)
    )
    assert outcome.resolved
    assert outcome.net_return_pct == pytest.approx(10.0 - m.round_trip_cost_pct(31))
    assert m.evaluate(candidate, None, None).reason == "missing_entry_bar"
    assert m.evaluate(candidate, _bar(candidate.entry_ms, 1, 1), None).reason == "missing_exit_bar"
    bad = _bar(candidate.exit_bar_ms, 1.0, float("nan"))
    assert (
        m.evaluate(candidate, _bar(candidate.entry_ms, 1, 1), bad).reason == "invalid_market_data"
    )


def _result(source: str, **kw: Any) -> m.VenueResult:
    values: dict[str, Any] = {
        "source": source,
        "candidates": 150,
        "resolved": 120,
        "assets": 40,
        "max_week_share": 0.3,
        "mean_net_pct": 0.5,
        "ci_lower_pct": 0.1,
        "ci_upper_pct": 0.9,
        "p_value": 0.001,
        "unresolved": {},
    }
    values.update(kw)
    return m.VenueResult(**values)


def test_discovery_needs_holm_rejection_and_a_positive_mean() -> None:
    verdicts = m.family_verdicts(
        [
            _result("mexc"),
            _result("gate", p_value=0.2),  # not rejected by Holm
            _result("bingx", mean_net_pct=-0.5),  # significant but negative
        ],
        stage="discovery",
    )
    assert verdicts == {"mexc": "survives", "gate": "does_not_survive", "bingx": "does_not_survive"}


@pytest.mark.parametrize(
    "floor_break",
    [{"resolved": 99}, {"assets": 29}, {"max_week_share": 0.46}],
)
def test_holdout_below_the_floor_is_insufficient_data(floor_break: dict[str, Any]) -> None:
    verdicts = m.family_verdicts([_result("mexc", **floor_break)], stage="holdout")
    assert verdicts == {"mexc": "insufficient_data"}
    assert m.family_verdicts([_result("mexc")], stage="holdout") == {"mexc": "candidate"}


@pytest.mark.parametrize(
    ("overrides", "verdict"),
    [
        # mature and negative: the missing diversification cannot rescue it
        ({"assets": 20, "mean_net_pct": -0.4}, "fail"),
        ({"max_week_share": 0.6, "mean_net_pct": 0.0}, "fail"),
        # mature and positive but under-diversified: not a candidate, not a fail
        ({"assets": 20, "mean_net_pct": 0.4}, "insufficient_data"),
        # immature: negative is not yet a fail
        ({"resolved": 60, "mean_net_pct": -0.4}, "insufficient_data"),
        # meets the floor, negative: fail through Holm as before
        ({"mean_net_pct": -0.4}, "fail"),
    ],
)
def test_a_mature_negative_holdout_is_a_fail_before_the_floor(
    overrides: dict[str, Any], verdict: str
) -> None:
    result = _result("mexc", **overrides)
    assert m.family_verdicts([result], stage="holdout") == {"mexc": verdict}


def test_venue_result_clusters_by_asset() -> None:
    candidates = []
    for i, base in enumerate(["A", "B", "C", "A"]):
        event = _event(i, _obs("mexc", T + timedelta(hours=i), base_asset=base), base=base)
        launch = (m.BybitInstrument("X", base, 0, 0),)
        built, _ = m.build_candidates(
            [event], bybit_instruments=launch, stage="discovery", sources=["mexc"]
        )
        candidates.append(built[0])
    outcomes = [m.Outcome(c, True, None, 0.2) for c in candidates]
    result = m.venue_result("mexc", outcomes)
    assert (result.resolved, result.assets) == (4, 3)
    assert result.mean_net_pct == pytest.approx(0.2)


# --- report: maturity, snapshot, claim, replay -------------------------------------


def test_no_stage_reads_the_v2_cohort_window() -> None:
    for stage in m.STAGES:
        assert m.stage_window(stage)[1] <= datetime(2026, 9, 28, tzinfo=UTC)


def test_a_stage_is_refused_before_its_window_end_plus_the_maturation_lag() -> None:
    with pytest.raises(ValueError, match="can run from 2026-09-29"):
        m.assert_stage_mature("holdout", datetime(2026, 9, 26, 12, tzinfo=UTC))
    with pytest.raises(ValueError, match="can run from"):
        m.assert_stage_mature("holdout", datetime(2026, 9, 28, 23, 59, tzinfo=UTC))
    m.assert_stage_mature("holdout", datetime(2026, 9, 29, tzinfo=UTC))
    m.assert_stage_mature("discovery", datetime(2026, 9, 26, tzinfo=UTC))


NOW = datetime(2026, 9, 30, tzinfo=UTC)


def _rows(candidate: m.Candidate, entry_open: float, exit_close: float) -> list[list[str]]:
    rows = []
    ts = candidate.reference_ms
    while ts <= candidate.exit_bar_ms:
        price = exit_close if ts == candidate.exit_bar_ms else entry_open
        rows.append([str(ts), str(price), str(price), str(price), str(price), "1"])
        ts += ONE_MINUTE_MS
    return rows


def _inputs(stage: str, moves: dict[str, list[float]]) -> dict[str, Any]:
    """Stored inputs for a stage: per source, one episode per asset with the given move."""
    start, _ = m.stage_window(stage)
    candidates, bars, event_id = [], {}, 0
    for source, returns in moves.items():
        for i, move in enumerate(returns):
            event_id += 1
            at = start + timedelta(days=i % 20, hours=1, seconds=20)
            base = f"A{i}"
            candidate = m.Candidate(event_id, base, base, source, at, f"{base}USDT", 1.0)
            candidates.append(r._candidate_json(candidate))
            bars[str(event_id)] = _rows(candidate, 1.0, 1.0 + move / 100)
    return {
        "family_version": m.FAMILY_VERSION,
        "stage": stage,
        "window": [x.isoformat() for x in m.stage_window(stage)],
        "code_revision": "abc",
        "funnel": {},
        "candidates": candidates,
        "bars": bars,
    }


def _prepared(tmp_path: Path, stage: str, moves: dict[str, list[float]]) -> Path:
    stage_dir = tmp_path / stage
    stage_dir.mkdir()
    r.write_once(stage_dir / r.INPUTS_NAME, _inputs(stage, moves))
    return stage_dir


def test_the_read_claims_before_computing_and_never_reads_twice(tmp_path: Path) -> None:
    stage_dir = _prepared(tmp_path, "discovery", {"mexc": [3.0] * 40})
    payload = r.read_stage("discovery", stage_dir, None, NOW)
    claim = json.loads((stage_dir / r.CLAIM_NAME).read_text())
    assert claim["inputs_sha256"] == payload["inputs_sha256"]
    assert payload["verdicts"]["mexc"] == "survives"
    with pytest.raises(r.AlreadyReadError):
        r.read_stage("discovery", stage_dir, None, NOW)


def test_a_crashed_read_resumes_on_the_same_stored_inputs(tmp_path: Path) -> None:
    stage_dir = _prepared(tmp_path, "discovery", {"mexc": [3.0] * 40, "gate": [-1.0] * 40})
    first = r.read_stage("discovery", stage_dir, None, NOW)
    first_bytes = (stage_dir / r.RESULT_NAME).read_bytes()
    # Simulate a crash after the claim, before the result.
    (stage_dir / r.RESULT_NAME).unlink()
    (stage_dir / f"{r.RESULT_NAME}.sha256").unlink()
    replay = r.read_stage("discovery", stage_dir, None, NOW + timedelta(hours=3))
    assert replay == first
    assert (stage_dir / r.RESULT_NAME).read_bytes() == first_bytes


def test_a_claim_on_other_inputs_or_tampered_inputs_is_refused(tmp_path: Path) -> None:
    stage_dir = _prepared(tmp_path, "discovery", {"mexc": [3.0] * 40})
    (stage_dir / r.CLAIM_NAME).write_text(json.dumps({"inputs_sha256": "0" * 64}))
    with pytest.raises(ValueError, match="another"):
        r.read_stage("discovery", stage_dir, None, NOW)
    (stage_dir / r.CLAIM_NAME).unlink()
    (stage_dir / r.INPUTS_NAME).write_text("{}")
    with pytest.raises(ValueError, match="sha256"):
        r.read_stage("discovery", stage_dir, None, NOW)


def test_inputs_are_write_once(tmp_path: Path) -> None:
    r.write_once(tmp_path / "x.json", {"a": 1})
    with pytest.raises(FileExistsError):
        r.write_once(tmp_path / "x.json", {"a": 2})


def _discovery(tmp_path: Path, name: str, verdicts: dict[str, str]) -> Path:
    directory = tmp_path / name
    directory.mkdir()
    r.write_once(
        directory / r.RESULT_NAME,
        {
            "family_version": m.FAMILY_VERSION,
            "stage": "discovery",
            "tested_family": list(m.FORMAL_SOURCES),
            "verdicts": verdicts,
        },
    )
    return directory


def test_the_holdout_evaluates_only_discovery_survivors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moves = {"mexc": [2.0] * 120, "gate": [2.0] * 120, "toobit": [2.0] * 120}
    holdout_dir = _prepared(tmp_path, "holdout", moves)
    with pytest.raises(ValueError, match="requires --discovery-artifact"):
        r.read_stage("holdout", holdout_dir, None, NOW)
    assert not (holdout_dir / r.CLAIM_NAME).exists()
    discovery_dir = _discovery(tmp_path, "disc", {"mexc": "survives", "gate": "does_not_survive"})
    evaluated: set[str] = set()
    real = r.outcome_for

    def spy(candidate: m.Candidate, raw: Any, fetched: bool) -> m.Outcome:
        evaluated.add(candidate.source_exchange)
        return real(candidate, raw, fetched)

    monkeypatch.setattr(r, "outcome_for", spy)
    payload = r.read_stage("holdout", holdout_dir, discovery_dir, NOW)
    # Neither the non-survivor nor an exploratory venue has any holdout outcome computed.
    assert evaluated == {"mexc"}
    assert payload["tested_family"] == ["mexc"]
    assert set(payload["verdicts"]) == {"mexc"}
    assert payload["exploratory_results"] == []


def test_a_holdout_replay_with_another_discovery_result_is_refused(tmp_path: Path) -> None:
    # The colleague's reproduction: first attempt tests mexc, the retry passes another
    # valid discovery artifact whose survivor is gate, under the same holdout claim.
    holdout_dir = _prepared(tmp_path, "holdout", {"mexc": [2.0] * 120, "gate": [2.0] * 120})
    first = _discovery(tmp_path, "d1", {"mexc": "survives", "gate": "does_not_survive"})
    other = _discovery(tmp_path, "d2", {"mexc": "does_not_survive", "gate": "survives"})
    r.read_stage("holdout", holdout_dir, first, NOW)
    (holdout_dir / r.RESULT_NAME).unlink()  # crash before the result
    (holdout_dir / f"{r.RESULT_NAME}.sha256").unlink()
    claim = json.loads((holdout_dir / r.CLAIM_NAME).read_text())
    assert claim["tested_family"] == ["mexc"]
    assert len(claim["discovery_sha256"]) == 64
    with pytest.raises(ValueError, match="another tested_family"):
        r.read_stage("holdout", holdout_dir, other, NOW)
    # Same survivors from a different discovery file: refused on the discovery hash.
    same_survivors = _discovery(
        tmp_path, "d3", {"mexc": "survives", "gate": "does_not_survive", "bingx": "n/a"}
    )
    with pytest.raises(ValueError, match="another discovery_sha256"):
        r.read_stage("holdout", holdout_dir, same_survivors, NOW)
    assert r.read_stage("holdout", holdout_dir, first, NOW)["tested_family"] == ["mexc"]


def test_files_are_published_whole_or_not_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A crash while writing the claim leaves no claim at all, never a truncated one.
    stage_dir = _prepared(tmp_path, "discovery", {"mexc": [3.0] * 40})

    def crash(*_: Any) -> None:
        raise OSError("killed")

    monkeypatch.setattr(os, "link", crash)
    with pytest.raises(OSError, match="killed"):
        r.read_stage("discovery", stage_dir, None, NOW)
    monkeypatch.undo()
    assert not (stage_dir / r.CLAIM_NAME).exists()
    assert [p.name for p in stage_dir.iterdir() if p.name.endswith(".tmp")] == []
    assert r.read_stage("discovery", stage_dir, None, NOW)["verdicts"]["mexc"] == "survives"


def test_a_result_published_without_its_digest_is_finished_by_replay(tmp_path: Path) -> None:
    stage_dir = _prepared(tmp_path, "discovery", {"mexc": [3.0] * 40})
    first = r.read_stage("discovery", stage_dir, None, NOW)
    digest = (stage_dir / f"{r.RESULT_NAME}.sha256").read_text()
    (stage_dir / f"{r.RESULT_NAME}.sha256").unlink()  # crash between result and digest
    assert r.read_stage("discovery", stage_dir, None, NOW + timedelta(hours=1)) == first
    assert (stage_dir / f"{r.RESULT_NAME}.sha256").read_text() == digest
    with pytest.raises(r.AlreadyReadError):
        r.read_stage("discovery", stage_dir, None, NOW)
    # A stored result that is not its replay is never blessed with a digest.
    (stage_dir / f"{r.RESULT_NAME}.sha256").unlink()
    (stage_dir / r.RESULT_NAME).write_text("{}")
    with pytest.raises(ValueError, match="differs from its replay"):
        r.read_stage("discovery", stage_dir, None, NOW)
    assert not (stage_dir / f"{r.RESULT_NAME}.sha256").exists()


def test_inputs_published_without_their_digest_are_completed(tmp_path: Path) -> None:
    stage_dir = _prepared(tmp_path, "discovery", {"mexc": [3.0] * 40})
    digest = (stage_dir / f"{r.INPUTS_NAME}.sha256").read_text()
    (stage_dir / f"{r.INPUTS_NAME}.sha256").unlink()
    r.read_stage("discovery", stage_dir, None, NOW)
    assert (stage_dir / f"{r.INPUTS_NAME}.sha256").read_text() == digest


def test_a_holdout_read_is_refused_before_it_matures(tmp_path: Path) -> None:
    holdout_dir = _prepared(tmp_path, "holdout", {"mexc": [2.0] * 120})
    discovery_dir = _discovery(tmp_path, "disc", {"mexc": "survives"})
    with pytest.raises(ValueError, match="can run from"):
        r.read_stage("holdout", holdout_dir, discovery_dir, datetime(2026, 9, 27, tzinfo=UTC))
    assert not (holdout_dir / r.CLAIM_NAME).exists()


def test_route_identity_drops_mismatches_but_keeps_fetch_failures() -> None:
    same = _build(_event(1, _obs("mexc", T)))[0][0]
    other = _build(_event(2, _obs("mexc", T, first_price=40.0)))[0][0]
    failed = _build(_event(3, _obs("mexc", T)))[0][0]
    bars = {1: _rows(same, 1.0, 1.0), 2: _rows(other, 1.0, 1.0), 3: None}
    kept, statuses = r.apply_route_identity([same, other, failed], bars)
    assert [c.event_id for c in kept] == [1, 3]
    assert statuses == {"route_identity:price_level_mismatch": 1}
    assert r.outcome_for(failed, None, fetched=True).reason == "bar_fetch_failed"


def test_the_resolution_summary_computes_no_return() -> None:
    inputs = _inputs("discovery", {"mexc": [3.0, -2.0]})
    inputs["bars"]["2"] = None
    summary = r.resolution_summary(inputs)
    assert summary["mexc"]["candidates"] == 2
    assert summary["mexc"]["bars_present"] == 1
    assert summary["mexc"]["fetch_failed"] == 1
    assert "net" not in json.dumps(summary)


def test_the_catalogue_parser_keeps_delisted_contracts() -> None:
    raw = [
        {"symbol": "ABCUSDT", "baseCoin": "abc", "launchTime": "5", "deliveryTime": "0"},
        {"symbol": "OLDUSDT", "baseCoin": "OLD", "launchTime": "1", "deliveryTime": "9"},
    ]
    assert r.parse_instruments(raw) == (
        m.BybitInstrument("ABCUSDT", "ABC", 5, 0),
        m.BybitInstrument("OLDUSDT", "OLD", 1, 9),
    )
    assert set(r.CATALOGUE_STATUSES) >= {"Trading", "Closed"}
