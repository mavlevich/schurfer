from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from schurfer_analytics.extreme_mover_replay import DISCOVERY_END, Report, build_report
from schurfer_analytics.extreme_mover_replay_report import render_json, render_markdown
from schurfer_analytics.extreme_mover_replay_repository import extreme_mover_inputs_statement
from schurfer_analytics.replay import ReplayFilters
from sqlalchemy.dialects import postgresql

T0 = datetime(2026, 8, 27, tzinfo=UTC)


def _empty_report() -> Report:
    return build_report(
        (),
        dataset_since=T0,
        dataset_until_exclusive=DISCOVERY_END,
        database_snapshot_at=DISCOVERY_END + timedelta(hours=4),
        generated_at=DISCOVERY_END + timedelta(hours=4),
        code_revision="abc123",
        working_tree_dirty=False,
        bootstrap_iterations=100,
        bootstrap_seed=7,
    )


def test_json_contains_reproducibility_and_routing_contract() -> None:
    payload = json.loads(render_json(_empty_report()))

    assert payload["manifest"]["report_version"] == "extreme_mover_endpoint_replay_v1"
    assert payload["manifest"]["resolver_version"] == "forward_v1"
    assert payload["manifest"]["working_tree_dirty"] is False
    assert payload["manifest"]["input_fingerprint"]
    assert {row["verdict"] for row in payload["verdicts"]} == {"insufficient_discovery"}


def test_markdown_keeps_discovery_and_execution_boundaries_visible() -> None:
    rendered = render_markdown(_empty_report())

    assert "Discovery on a viewed window" in rendered
    assert "exact same-venue `forward_v1`" in rendered
    assert "cannot promote a strategy" in rendered
    assert "## Coverage and cash reasons" in rendered


def test_repository_query_pushes_both_anchor_selections_into_postgresql() -> None:
    filters = ReplayFilters(
        since=T0,
        until=DISCOVERY_END,
        strategy_versions=(
            "pump_short_measurement_v1",
            "pump_short_v1_market_quality",
        ),
        required_horizons=(15, 60, 240),
    )

    compiled = extreme_mover_inputs_statement(filters).compile(
        dialect=postgresql.dialect()  # type: ignore[no-untyped-call]
    )
    sql = str(compiled)

    assert "extreme_mover_first_decisions" in sql
    assert "extreme_mover_first_quality_decisions" in sql
    assert "extreme_mover_selected_decisions" in sql
    assert sql.count("DISTINCT ON") == 2
    assert "app.trade_decisions.strategy_version IN" in sql
    assert "app.trade_decisions.id IN" in sql
    assert "quality" in compiled.params.values()
    assert "allowed" in compiled.params.values()
