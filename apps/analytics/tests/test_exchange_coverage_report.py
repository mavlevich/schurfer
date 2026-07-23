import json
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.exchange_coverage_report import (
    CoverageFilters,
    SourceObservation,
    build_report,
    render_json,
    render_markdown,
)


def _at(minutes: int) -> datetime:
    return datetime(2026, 7, 23, tzinfo=UTC) + timedelta(minutes=minutes)


def test_build_report_attributes_first_sole_confirmed_and_lead() -> None:
    filters = CoverageFilters(since=_at(0), until=_at(120))
    observations = [
        SourceObservation(1, "binance", _at(0)),
        SourceObservation(1, "xt", _at(1)),
        SourceObservation(2, "xt", _at(10)),
        SourceObservation(3, "bitmart", _at(20)),
        SourceObservation(3, "lbank", _at(20)),
    ]

    report = build_report(filters, total_episodes=4, observations=observations)

    assert report.attributed_episodes == 3
    by_exchange = {row.exchange: row for row in report.sources}
    assert by_exchange["xt"].episodes == 2
    assert by_exchange["xt"].sole_source_episodes == 1
    assert by_exchange["xt"].first_source_episodes == 1
    assert by_exchange["xt"].confirmed_episodes == 1
    assert by_exchange["xt"].lead_p50_seconds == 30.0
    assert by_exchange["xt"].lead_p95_seconds == pytest.approx(57.0)
    assert by_exchange["binance"].first_source_episodes == 1
    assert by_exchange["bitmart"].first_source_episodes == 1
    assert by_exchange["lbank"].first_source_episodes == 1
    assert {(row.first_exchange, row.second_exchange, row.episodes) for row in report.overlaps} == {
        ("binance", "xt", 1),
        ("bitmart", "lbank", 1),
    }


def test_build_report_handles_empty_dataset() -> None:
    report = build_report(CoverageFilters(), total_episodes=0, observations=[])

    assert report.attributed_episodes == 0
    assert report.sources == ()
    assert report.overlaps == ()
    assert "Attributed episodes: 0 (0.00%)" in render_markdown(report)


def test_renderers_include_health_and_source_rows() -> None:
    report = build_report(
        CoverageFilters(),
        total_episodes=1,
        observations=[SourceObservation(1, "lbank", _at(0))],
    )

    markdown = render_markdown(report)
    payload = json.loads(render_json(report))

    assert "# Exchange Coverage Report" in markdown
    assert "left-censored" in markdown
    assert "| lbank | 1 | 1 | 1 | 0 | 0.0s | 0.0s |" in markdown
    assert payload["sources"][0]["exchange"] == "lbank"


def test_filters_reject_empty_or_reversed_window() -> None:
    with pytest.raises(ValueError, match="earlier"):
        CoverageFilters(since=_at(10), until=_at(10))
