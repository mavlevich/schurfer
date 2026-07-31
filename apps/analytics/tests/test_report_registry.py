from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.report_registry import ReportRunRecord, report_run_statement
from sqlalchemy.dialects import postgresql


def _record(**overrides: object) -> ReportRunRecord:
    start = datetime(2026, 7, 30, tzinfo=UTC)
    values = {
        "contract": "liquid_taker_candidate_v1",
        "report_version": "liquid_taker_forward_report_v1",
        "generated_at": start + timedelta(days=1),
        "dataset_since": start,
        "dataset_until_exclusive": start + timedelta(days=1),
        "code_revision": "a" * 40,
        "working_tree_dirty": False,
        "decision_input_fingerprint": "b" * 64,
        "market_path_fingerprint": "c" * 64,
        "status": "collecting",
        "verdict": "withheld",
        "eligible_episodes": 12,
        "asset_clusters": 8,
        "calendar_weeks": 1,
        "summary": {"point_estimate_pct": None},
    }
    values.update(overrides)
    return ReportRunRecord(**values)  # type: ignore[arg-type]


def test_report_run_statement_is_append_only_metadata() -> None:
    statement = report_run_statement(_record())
    compiled = str(
        statement.compile(
            dialect=postgresql.dialect()  # type: ignore[no-untyped-call]
        )
    )

    assert "INSERT INTO app.research_report_runs" in compiled
    assert "episode_results" not in compiled
    assert "market_paths" not in compiled


@pytest.mark.parametrize(
    "overrides",
    [
        {"contract": ""},
        {"dataset_until_exclusive": datetime(2026, 7, 30, tzinfo=UTC)},
        {"eligible_episodes": -1},
    ],
)
def test_report_run_record_fails_closed(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _record(**overrides)
