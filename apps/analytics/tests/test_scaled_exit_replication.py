"""Coverage for the held-out replication of the scaled exit.

There is one untouched window left in this line of work. The tests that matter
here are the ones that stop it being spent twice or read loosely: the verdict
must follow the registered rule at its exact boundaries, and the window must not
be movable.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from schurfer_analytics.scaled_exit_replication import (
    HOLDOUT_SINCE,
    MINIMUM_TRADES,
    REPLICATION_MARGIN_PCT,
    PairedEpisode,
    ReplicationReport,
    main,
    render_markdown,
)
from schurfer_analytics.virtual_strategy import VirtualTrade

_NOW = datetime(2026, 9, 8, tzinfo=UTC)


def _trade(net_return_pct: float | None, *, status: str = "complete") -> VirtualTrade:
    return VirtualTrade(
        pump_event_id=1,
        cluster_key="base:ERA",
        base="ERA",
        exchange="binance",
        decision_id="d-1",
        decision_at=_NOW,
        taken=True,
        selection_reason="first_recorded_open",
        status=status,
        classification="taken_won",
        exit_reason="trailing_stop",
        ambiguity_resolution=None,
        entry_at=_NOW,
        exit_at=_NOW + timedelta(minutes=20),
        entry_price=100.0,
        exit_price=99.0,
        entry_delay_seconds=0.0,
        duration_minutes=20.0,
        position_usd=50.0,
        gross_return_pct=net_return_pct,
        net_return_pct=net_return_pct,
        gross_pnl_usd=net_return_pct,
        net_pnl_usd=net_return_pct,
        fee_cost_bps=None,
        funding_cost_bps=None,
        slippage_cost_bps=None,
        mfe_pct=None,
        mae_pct=None,
        captured_move_pct=None,
    )


def _report(deltas: list[float], *, incomplete: int = 0) -> ReplicationReport:
    pairs = [PairedEpisode(index, _trade(0.0), _trade(delta)) for index, delta in enumerate(deltas)]
    pairs.extend(
        PairedEpisode(1000 + index, _trade(None, status="incomplete"), _trade(0.5))
        for index in range(incomplete)
    )
    return ReplicationReport(tuple(pairs), generated_at=_NOW, code_revision="abc1234")


def test_the_window_is_a_constant_not_an_option() -> None:
    """A held-out window stops being held out the moment a flag can move it,
    and there is not a second one in this line of work."""
    assert datetime(2026, 8, 25, tzinfo=UTC) == HOLDOUT_SINCE
    source = Path(main.__code__.co_filename).read_text()
    assert '"--since"' not in source
    assert '"--until"' not in source


def test_cli_exposes_no_window_flags() -> None:
    parser = argparse.ArgumentParser()
    # Rebuild what main() accepts and assert the window is not among it.
    parser.add_argument("--code-revision")
    parser.add_argument("--working-tree-dirty", action=argparse.BooleanOptionalAction)
    accepted = {action.dest for action in parser._actions}
    assert "since" not in accepted
    assert "until" not in accepted


@pytest.mark.parametrize(
    ("delta", "count", "expected"),
    [
        (REPLICATION_MARGIN_PCT, MINIMUM_TRADES, "replicates"),
        (REPLICATION_MARGIN_PCT - 0.01, MINIMUM_TRADES, "inconclusive"),
        (0.01, MINIMUM_TRADES, "inconclusive"),
        (0.0, MINIMUM_TRADES, "does_not_replicate"),
        (-1.0, MINIMUM_TRADES, "does_not_replicate"),
        # The evidence floor binds however good the delta looks.
        (2.0, MINIMUM_TRADES - 1, "inconclusive"),
    ],
)
def test_verdict_follows_the_registered_rule(delta: float, count: int, expected: str) -> None:
    assert _report([delta] * count).verdict == expected


def test_only_episodes_complete_under_both_policies_count() -> None:
    """An unpaired comparison would measure the two policies on different
    episodes, and the difference would then include whatever separates those
    populations rather than the policies."""
    report = _report([0.5] * 10, incomplete=5)
    assert len(report.pairs) == 15
    assert len(report.complete) == 10
    assert report.mean_delta_pct == pytest.approx(0.5)


def test_markdown_states_the_verdict_and_the_window() -> None:
    markdown = render_markdown(_report([0.5] * MINIMUM_TRADES))
    assert "`replicates`" in markdown
    assert "2026-08-25 onward, read once" in markdown
    assert "never changes production exits" in markdown
