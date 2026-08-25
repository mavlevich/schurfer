from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from schurfer_analytics import early_momentum_unused_flow_features_report as report_mod
from schurfer_analytics.early_momentum_unused_flow_features import DISCOVERY_END


async def test_report_rejects_database_snapshot_before_maturity() -> None:
    repository = AsyncMock()
    repository.fetch.return_value = (DISCOVERY_END + timedelta(hours=5), ())
    with (
        patch(
            "schurfer_analytics.early_momentum_unused_flow_features_report."
            "EarlyMomentumUnusedFlowFeaturesRepository.from_url",
            return_value=repository,
        ),
        pytest.raises(report_mod.DiscoveryWindowNotMatureError),
    ):
        await report_mod.generate_report(
            db_url="postgresql://fake",
            code_revision="abc123",
            working_tree_dirty=False,
        )
    repository.close.assert_awaited_once()


async def test_report_uses_fixed_discovery_bounds_and_marks_dirty_run_non_formal() -> None:
    repository = AsyncMock()
    repository.fetch.return_value = (DISCOVERY_END + timedelta(hours=7), ())
    with patch(
        "schurfer_analytics.early_momentum_unused_flow_features_report."
        "EarlyMomentumUnusedFlowFeaturesRepository.from_url",
        return_value=repository,
    ):
        report = await report_mod.generate_report(
            db_url="postgresql://fake",
            code_revision="abc123",
            working_tree_dirty=True,
        )
    assert report.formal_run is False
    repository.fetch.assert_awaited_once_with(
        cohort_start=report.analysis.discovery_start,
        cohort_end=DISCOVERY_END,
    )
    assert "discovery_only_requires_new_prospective_registration" in (
        report.analysis.verdict_reasons
    )

    markdown = report_mod.render_markdown(report)
    assert "Discovery only" in markdown
    assert "Frozen discovery candidate" in markdown
    assert "0.20 <= imbalance_15m < 0.50" in markdown

    payload = json.loads(report_mod.render_json(report))
    assert payload["formal_run"] is False
    assert payload["analysis"]["verdict"] == "insufficient_data"
