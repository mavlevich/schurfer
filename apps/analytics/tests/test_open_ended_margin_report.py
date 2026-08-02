from datetime import UTC, datetime

import pytest
from schurfer_analytics.open_ended_margin_report import (
    OPEN_ENDED_MARGIN_COHORT_START,
    OPEN_ENDED_MARGIN_ELIGIBILITY_VERSION,
    OPEN_ENDED_MARGIN_GATE_SPEC,
    OPEN_ENDED_MARGIN_HORIZONS,
    OPEN_ENDED_MARGIN_REPORT_VERSION,
    build_parser,
)


def test_contract_is_forward_locked_after_discovery() -> None:
    assert datetime(2026, 8, 3, tzinfo=UTC) == OPEN_ENDED_MARGIN_COHORT_START
    assert OPEN_ENDED_MARGIN_HORIZONS == (20_160, 30_240, 40_320)
    assert OPEN_ENDED_MARGIN_REPORT_VERSION == "open_ended_margin_report_v1"
    assert OPEN_ENDED_MARGIN_ELIGIBILITY_VERSION == "prospective_no_time_exit_margin_buffer_v1"
    assert OPEN_ENDED_MARGIN_GATE_SPEC.interim_horizon_minutes == 20_160
    assert OPEN_ENDED_MARGIN_GATE_SPEC.final_horizon_minutes == 40_320
    assert OPEN_ENDED_MARGIN_GATE_SPEC.collateral_cap_pct == 100
    assert OPEN_ENDED_MARGIN_GATE_SPEC.minimum_survival_rate_pct == 80


def test_cli_requires_explicit_dirty_state() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])
    args = parser.parse_args(["--no-working-tree-dirty"])

    assert args.since == OPEN_ENDED_MARGIN_COHORT_START
    assert args.working_tree_dirty is False
    assert not hasattr(args, "strategy_version")
    assert not hasattr(args, "resolver_version")
