"""Coverage for the replay-versus-paper reconciliation.

The point of these tests is that the comparison must not flatter itself. A
reconciliation that quietly counts an unreplayable trade as agreement, or that
grades a verdict by judgement rather than by the registered rule, would be worse
than no reconciliation at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.ohlcv import TIMEFRAME_MS, Candle
from schurfer_analytics.paper_replay_reconciliation import (
    Comparison,
    PaperTrade,
    ReconciliationReport,
    canonical_rule,
    reconcile,
    render_markdown,
)
from schurfer_analytics.replay import ReplayDecision, ReplayEpisode
from schurfer_analytics.virtual_strategy import MarketPath, expected_path_bounds

_ENTRY = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _decision(decision_id: str = "d-1") -> ReplayDecision:
    return ReplayDecision(
        row_id=1,
        decision_id=decision_id,
        pump_event_id=42,
        event_base="ERA",
        event_first_seen_at=_ENTRY - timedelta(minutes=5),
        event_closed_at=_ENTRY + timedelta(hours=8),
        ts=_ENTRY,
        base="ERA",
        exchange="binance",
        action="opened_dry_run",
        reason="dry_run",
        score=5,
        pump_pct=40.0,
        price=100.0,
        strategy_version="pump_short_v1_market_quality",
        features={
            "signal": {"computed_at": _ENTRY.timestamp()},
            "config": {"signal_position_usd": 50.0},
        },
        liquidity={
            "status": "sampled",
            "bid_impact_bps": {"100": 3.0},
            "ask_impact_bps": {"100": 4.0},
            "quality": {"depth_target_usd": 100.0},
        },
        outcomes=(),
    )


def _episode(decision: ReplayDecision | None = None) -> ReplayEpisode:
    return ReplayEpisode(
        pump_event_id=42,
        base="ERA",
        cluster_key="base:ERA",
        decisions=(decision or _decision(),),
        exclusion_reasons=(),
    )


def _path(close: float, *, status: str = "complete") -> MarketPath:
    """A flat path at `close` after an opening bar at 100."""
    start_ms, end_ms = expected_path_bounds(_decision())
    count = (end_ms - start_ms) // TIMEFRAME_MS
    candles = [Candle(start_ms, 100.0, 100.0, 100.0, 100.0, 1.0)]
    candles.extend(
        Candle(start_ms + i * TIMEFRAME_MS, close, close, close, close, 1.0)
        for i in range(1, count)
    )
    return MarketPath(
        pump_event_id=42,
        exchange="binance",
        base="ERA",
        status=status,
        candles=tuple(candles),
    )


def _trade(
    *,
    exchange: str = "binance",
    reason: str | None = "no_progress age=60min",
    entry_price: float = 100.0,
) -> PaperTrade:
    return PaperTrade(
        trade_id=1,
        pump_event_id=42,
        decision_id="d-1",
        exchange=exchange,
        base="ERA",
        entry_at=_ENTRY,
        entry_price=entry_price,
        exit_at=_ENTRY + timedelta(minutes=60),
        exit_price=96.0,
        reason=reason,
    )


def _reconcile(trade: PaperTrade, path: MarketPath | None) -> ReconciliationReport:
    return reconcile(
        [trade],
        {42: _episode()},
        {42: path} if path is not None else {},
        generated_at=datetime(2026, 9, 8, tzinfo=UTC),
        code_revision="abc1234",
    )


def test_matching_reason_counts_as_agreement() -> None:
    """A short sitting at 96 is up 4%, below the 8% activation, so production
    closes it at minute 60 -- the same rule the broker recorded as
    `no_progress`."""
    report = _reconcile(_trade(), _path(96.0))
    assert len(report.reconcilable) == 1
    comparison = report.reconcilable[0]
    assert comparison.replayed_reason is not None
    assert comparison.replayed_reason.startswith("not_activated")
    assert comparison.reasons_match, comparison.replayed_reason
    assert report.match_rate_pct == 100.0


def test_the_two_sides_name_the_same_rule_differently() -> None:
    """The broker writes `no_progress`; the replay writes `not_activated`,
    because `no_progress` already means a different rule in the exit-policy
    family. Comparing the raw strings would report zero agreement on the
    category that covers most trades."""
    assert canonical_rule("not_activated") == "no_progress"
    assert canonical_rule("no_progress age=60min") == "no_progress"
    assert canonical_rule("absolute_max_hold") == "max_hold"
    assert canonical_rule("initial_sl move=-8.5%") == "initial_sl"
    assert canonical_rule(None) == "unknown"


def test_reason_keyword_is_compared_without_the_numbers() -> None:
    """`no_progress age=60min` and `no_progress age=61min` are the same rule.
    Comparing the whole string would report disagreement on a one-minute
    sampling difference, which is exactly what cannot match."""
    assert _trade(reason="no_progress age=60min").recorded_reason == "no_progress"
    assert _trade(reason="initial_sl move=-8.5%").recorded_reason == "initial_sl"
    assert _trade(reason=None).recorded_reason == "unknown"


def test_unreplayable_exchange_is_coverage_not_disagreement() -> None:
    """LBank has no perpetual OHLCV at all. Counting it as a mismatch would
    make the simulator look wrong for a reason that is not about the simulator."""
    report = _reconcile(_trade(exchange="lbank"), _path(96.0))
    assert report.comparisons[0].status == "unreplayable_exchange"
    assert report.reconcilable == ()
    assert report.match_rate_pct is None


def test_missing_market_path_is_not_silently_dropped() -> None:
    report = _reconcile(_trade(), None)
    assert report.comparisons[0].status == "market_path_unavailable"
    assert report.reconcilable == ()


def test_incomplete_market_path_is_refused() -> None:
    report = _reconcile(_trade(), _path(96.0, status="incomplete"))
    assert report.comparisons[0].status == "market_path_unavailable"


def _report(rates: tuple[int, int]) -> ReconciliationReport:
    """A report with `matched` agreeing and `total - matched` disagreeing."""
    matched, total = rates
    comparisons = []
    for index in range(total):
        agrees = index < matched
        comparisons.append(
            Comparison(
                _trade(),
                "compared",
                replayed_reason="no_progress" if agrees else "initial_sl",
                replayed_exit_at=_ENTRY + timedelta(minutes=60),
                replayed_exit_price=96.0,
            )
        )
    return ReconciliationReport(
        comparisons=tuple(comparisons),
        generated_at=datetime(2026, 9, 8, tzinfo=UTC),
        code_revision="abc1234",
    )


@pytest.mark.parametrize(
    ("matched", "total", "expected"),
    [
        (85, 100, "agrees"),
        (84, 100, "inconclusive"),
        (70, 100, "inconclusive"),
        (69, 100, "disagrees"),
        # The evidence floor binds regardless of how good the rate looks.
        (79, 79, "inconclusive"),
        (80, 80, "agrees"),
    ],
)
def test_verdict_follows_the_registered_rule(matched: int, total: int, expected: str) -> None:
    assert _report((matched, total)).verdict == expected


def test_secondary_measures_are_only_taken_from_matching_trades() -> None:
    """A trade whose reason disagrees has no meaningful exit-time delta: the two
    runs exited for different reasons, so the difference is not a sampling gap."""
    disagreeing = Comparison(
        _trade(),
        "compared",
        replayed_reason="initial_sl",
        replayed_exit_at=_ENTRY + timedelta(minutes=20),
        replayed_exit_price=92.0,
    )
    assert disagreeing.exit_time_delta_minutes is None
    assert disagreeing.exit_price_delta_pct is None

    agreeing = Comparison(
        _trade(),
        "compared",
        replayed_reason="no_progress",
        replayed_exit_at=_ENTRY + timedelta(minutes=65),
        replayed_exit_price=97.0,
    )
    assert agreeing.exit_time_delta_minutes == pytest.approx(5.0)
    assert agreeing.exit_price_delta_pct == pytest.approx(1.0416, abs=1e-3)


def test_markdown_states_the_verdict_and_the_rule() -> None:
    markdown = render_markdown(_report((85, 100)))
    assert "`agrees`" in markdown
    assert "never changes production exits" in markdown
    assert "85 of 100" in markdown


def test_a_trade_is_not_compared_against_another_venue() -> None:
    """Reproduced by a colleague: a Bybit trade measured against Binance candles
    came back `compared` with a matching reason. Two venues move similarly
    enough for the same rule to fire, so the agreement was meaningless rather
    than obviously wrong."""
    report = _reconcile(_trade(exchange="bybit"), _path(96.0))
    assert report.comparisons[0].status == "exchange_mismatch"
    assert report.reconcilable == ()


def test_an_unmatched_decision_id_is_coverage_not_a_comparison() -> None:
    """An earlier version substituted the episode's first decision and counted
    the result. Exit parameters come from the decision's own pump_pct, so a
    substitution can select a different pump band and therefore different
    thresholds -- the comparison would measure a policy the broker never ran."""
    trade = PaperTrade(
        trade_id=1,
        pump_event_id=42,
        decision_id="does-not-exist",
        exchange="binance",
        base="ERA",
        entry_at=_ENTRY,
        entry_price=100.0,
        exit_at=_ENTRY + timedelta(minutes=60),
        exit_price=96.0,
        reason="no_progress age=60min",
    )
    report = _reconcile(trade, _path(96.0))
    assert report.comparisons[0].status == "decision_unmatched"
    assert report.reconcilable == ()
    assert report.match_rate_pct is None


def test_a_missing_decision_id_is_also_not_substituted() -> None:
    trade = PaperTrade(
        trade_id=1,
        pump_event_id=42,
        decision_id=None,
        exchange="binance",
        base="ERA",
        entry_at=_ENTRY,
        entry_price=100.0,
        exit_at=_ENTRY + timedelta(minutes=60),
        exit_price=96.0,
        reason="no_progress age=60min",
    )
    report = _reconcile(trade, _path(96.0))
    assert report.comparisons[0].status == "decision_unmatched"
