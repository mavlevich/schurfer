from __future__ import annotations

import pytest
from schurfer_analytics.radar_outcome_discovery_report import (
    DEFAULT_MAX_WATCH_DECISIONS,
    build_parser,
    check_watch_decision_count,
)


def test_max_watch_decisions_defaults_and_is_overridable() -> None:
    base_args = [
        "--since",
        "2026-08-18T00:00:00Z",
        "--until",
        "2026-08-27T00:00:00Z",
        "--code-revision",
        "test",
        "--no-working-tree-dirty",
    ]
    parsed = build_parser().parse_args(base_args)
    assert parsed.max_watch_decisions == DEFAULT_MAX_WATCH_DECISIONS
    overridden = build_parser().parse_args([*base_args, "--max-watch-decisions", "5"])
    assert overridden.max_watch_decisions == 5


def test_check_watch_decision_count_raises_over_the_cap() -> None:
    # Colleague review, 2026-09-01: fetch_watch_signals had no post-fetch
    # bound at all, unlike cex_activity_discovery_report.py's own
    # check_candidate_count for the analogous burst-minute scan -- fail
    # loudly rather than silently evaluating an unexpectedly large result.
    with pytest.raises(ValueError, match="max-watch-decisions"):
        check_watch_decision_count(1001, 1000)
    check_watch_decision_count(1000, 1000)  # exactly at the cap is fine
