from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.episode_replay import (
    CONFIRMATION_COHORT_START,
    ReplayReadinessReport,
    build_parser,
    build_report,
    render_json,
    render_markdown,
)
from schurfer_analytics.replay import (
    ReplayDecision,
    ReplayFilters,
    ReplayOutcome,
    build_replay_dataset,
)


def _decision(row_id: int, event_id: int, base: str) -> ReplayDecision:
    ts = datetime(2026, 7, 26, tzinfo=UTC) + timedelta(minutes=row_id)
    return ReplayDecision(
        row_id=row_id,
        decision_id=f"00000000-0000-0000-0000-{row_id:012d}",
        pump_event_id=event_id,
        event_base=base,
        event_first_seen_at=datetime(2026, 7, 26, tzinfo=UTC),
        event_closed_at=ts + timedelta(hours=1),
        ts=ts,
        base=base,
        exchange="binance",
        action="skipped",
        reason="score 5 < threshold 6",
        score=5,
        pump_pct=40.0,
        price=100.0,
        strategy_version="pump_short_v1_market_quality",
        features={
            "signal": {"computed_at": ts.timestamp()},
            "config": {"score_threshold": 6},
        },
        liquidity={"status": "sampled"},
        outcomes=(
            ReplayOutcome(
                horizon_minutes=480,
                status="complete",
                anchor_exchange="binance",
                source_exchange="binance",
                entry_price=100,
                forward_price=90,
                mfe_pct=12,
                mae_pct=3,
                short_return_pct=10,
                coverage_ratio=1,
            ),
        ),
    )


def _report(
    count: int,
    clusters: int,
    *,
    working_tree_dirty: bool = False,
) -> ReplayReadinessReport:
    since = datetime(2026, 7, 26, tzinfo=UTC)
    filters = ReplayFilters(since=since, until=since + timedelta(days=10))
    decisions = [
        _decision(index + 1, index + 1, f"TOKEN{index % clusters}") for index in range(count)
    ]
    dataset = build_replay_dataset(decisions, filters)
    return build_report(
        dataset,
        filters,
        generated_at=since + timedelta(days=10),
        code_revision="4709bd6",
        working_tree_dirty=working_tree_dirty,
    )


def test_report_locks_first_formal_sample_and_cluster_diversity() -> None:
    report = _report(120, 35)

    assert report.health.eligible_episodes == 120
    assert report.health.formal_sample_episodes == 100
    assert report.health.formal_sample_clusters == 35
    assert report.health.readiness == "formal_sample_ready"


def test_report_rejects_concentrated_formal_sample() -> None:
    report = _report(100, 10)

    assert report.health.readiness == "insufficient_diversity"


def test_directional_sample_cannot_be_formal_ready() -> None:
    report = _report(50, 35)

    assert report.health.readiness == "directional_only"


def test_renderers_expose_manifest_health_and_concentration() -> None:
    report = _report(2, 1)

    markdown = render_markdown(report)
    payload = json.loads(render_json(report))

    assert "# Episode Replay Readiness" in markdown
    assert "does not simulate entries" in markdown
    assert "`4709bd6`" in markdown
    assert "Working tree dirty: no" in markdown
    assert "| base:TOKEN0 | 2 | 100.00% |" in markdown
    assert payload["manifest"]["query_version"] == "replay_inputs_v2"
    assert payload["manifest"]["working_tree_dirty"] is False
    assert payload["health"]["readiness"] == "collecting"


def test_parser_defaults_to_locked_confirmation_cohort() -> None:
    args = build_parser().parse_args(["--code-revision", "4709bd6", "--no-working-tree-dirty"])

    assert args.since == CONFIRMATION_COHORT_START
    assert args.horizon is None
    assert args.allow_fallback is False
    assert args.working_tree_dirty is False


def test_dirty_working_tree_is_machine_readable_in_manifest() -> None:
    report = _report(2, 1, working_tree_dirty=True)

    markdown = render_markdown(report)
    payload = json.loads(render_json(report))

    assert report.manifest.code_revision == "4709bd6"
    assert report.manifest.working_tree_dirty is True
    assert "Working tree dirty: yes" in markdown
    assert payload["manifest"]["working_tree_dirty"] is True


def test_parser_accepts_explicit_dirty_working_tree_flag() -> None:
    args = build_parser().parse_args(["--code-revision", "4709bd6", "--working-tree-dirty"])

    assert args.code_revision == "4709bd6"
    assert args.working_tree_dirty is True


def test_parser_requires_explicit_working_tree_state() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--code-revision", "4709bd6"])


def test_report_rejects_missing_code_revision() -> None:
    report = _report(1, 1)
    filters = ReplayFilters(
        since=report.manifest.dataset_since,
        until=report.manifest.dataset_until_exclusive,
    )
    dataset = build_replay_dataset([_decision(1, 1, "ERA")], filters)

    with pytest.raises(ValueError, match="revision"):
        build_report(
            dataset,
            filters,
            generated_at=report.manifest.generated_at,
            code_revision=" ",
        )
