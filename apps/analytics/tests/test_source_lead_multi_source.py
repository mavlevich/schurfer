from __future__ import annotations

import argparse
import asyncio
import json
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
LAUNCH = {"ABC": ("ABCUSDT", int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000))}


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
    return m.build_candidates(events, bybit_launch_ms=launch, stage="discovery", sources=["mexc"])


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


def test_a_perp_listed_after_the_signal_is_not_tradable_at_the_signal() -> None:
    late = {"ABC": ("ABCUSDT", int((T + timedelta(hours=1)).timestamp() * 1000))}
    assert _build(_event(1, _obs("mexc", T)), launch=late)[1] == {"bybit_not_listed_at_signal": 1}


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


def test_venue_result_clusters_by_asset() -> None:
    candidates = []
    for i, base in enumerate(["A", "B", "C", "A"]):
        event = _event(i, _obs("mexc", T + timedelta(hours=i), base_asset=base), base=base)
        launch = {base: ("X", 0)}
        built, _ = m.build_candidates(
            [event], bybit_launch_ms=launch, stage="discovery", sources=["mexc"]
        )
        candidates.append(built[0])
    outcomes = [m.Outcome(c, True, None, 0.2) for c in candidates]
    result = m.venue_result("mexc", outcomes)
    assert (result.resolved, result.assets) == (4, 3)
    assert result.mean_net_pct == pytest.approx(0.2)


# --- report guards ----------------------------------------------------------------


def test_holdout_requires_the_discovery_artifact_and_reads_only_survivors(tmp_path: Path) -> None:
    args = argparse.Namespace(stage="holdout", discovery_artifact=None)
    with pytest.raises(ValueError, match="requires --discovery-artifact"):
        asyncio.run(r.run_stage(args))
    payload = {
        "family_version": m.FAMILY_VERSION,
        "stage": "discovery",
        "verdicts": {"mexc": "survives", "gate": "does_not_survive"},
    }
    digest = r.write_artifact(tmp_path / "disc", payload)
    assert len(digest) == 64
    assert r._discovery_survivors(tmp_path / "disc") == ["mexc"]
    (tmp_path / "disc" / r.ARTIFACT_NAME).write_text(json.dumps({**payload, "verdicts": {}}))
    with pytest.raises(ValueError, match="sha256"):
        r._discovery_survivors(tmp_path / "disc")


def test_artifacts_are_write_once(tmp_path: Path) -> None:
    r.write_artifact(tmp_path / "x", {"a": 1})
    with pytest.raises(FileExistsError):
        r.write_artifact(tmp_path / "x", {"a": 2})


def test_no_stage_reads_the_v2_cohort_window() -> None:
    for stage in m.STAGES:
        assert m.stage_window(stage)[1] <= datetime(2026, 9, 28, tzinfo=UTC)


def test_a_failed_bar_fetch_is_its_own_reason_not_a_missing_bar() -> None:
    candidate = m.build_candidates(
        [_event(1, _obs("mexc", T))], bybit_launch_ms=LAUNCH, stage="discovery", sources=["mexc"]
    )[0][0]
    assert r.outcome_for(candidate, {1: None}).reason == "bar_fetch_failed"
    assert r.outcome_for(candidate, {}).reason == "missing_entry_bar"
    payload = r.funnel_only_payload("discovery", [candidate], {1: None}, {}, "x")
    assert payload["resolution"]["mexc"]["fetch_failed"] == 1
    assert "net_return_pct" not in json.dumps(payload)
