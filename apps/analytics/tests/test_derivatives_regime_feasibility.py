from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.derivatives_context_resolver import DERIVATIVES_CONTEXT_RESOLVER_VERSION
from schurfer_analytics.derivatives_regime_feasibility import (
    LSR_EXCHANGE,
    LSR_EXPECTED_BASELINE_POINTS,
    LSR_EXPECTED_RECENT_POINTS,
    LSR_METHOD,
    MIN_BASES,
    MIN_FEATURE_COMPLETE_EPISODES,
    MIN_UTC_WEEKS,
    _bases_days_weeks,
    _finite_ratio,
    _funnel_step,
    _LiquidationsRunRow,
    _readiness,
    binance_sourced_event_ids_statement,
    evaluate_episode_feature,
    liquidations_runs_statement,
    lsr_runs_statement,
    lsr_window_samples_statement,
    summarize_liquidations_runs,
)
from schurfer_analytics.replay import ReplayDecision, ReplayEpisode
from sqlalchemy.dialects import postgresql

ANCHOR = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _sql(statement: object) -> str:
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
    )


def _episode(event_id: int, base: str, at: datetime) -> ReplayEpisode:
    decision = ReplayDecision(
        row_id=event_id,
        decision_id=f"00000000-0000-0000-0000-{event_id:012d}",
        pump_event_id=event_id,
        event_base=base,
        event_first_seen_at=at,
        event_closed_at=at + timedelta(hours=8),
        ts=at,
        base=base,
        exchange="binance",
        action="skipped",
        reason="measurement",
        score=5,
        pump_pct=40,
        price=100,
        strategy_version="pump_short_v1_market_quality",
        features={},
        liquidity={},
        outcomes=(),
    )
    return ReplayEpisode(
        pump_event_id=event_id,
        base=base,
        cluster_key=f"base:{base}",
        decisions=(decision,),
        exclusion_reasons=(),
    )


# --- pure funnel helpers -----------------------------------------------------


def test_bases_days_weeks_counts_distinct_values() -> None:
    episodes = (
        _episode(1, "ERA", datetime(2026, 7, 28, tzinfo=UTC)),
        _episode(2, "ERA", datetime(2026, 7, 28, tzinfo=UTC)),  # same base, same day
        _episode(3, "DIA", datetime(2026, 8, 4, tzinfo=UTC)),  # different base, different week
    )

    bases, days, weeks = _bases_days_weeks(episodes)

    assert bases == 2
    assert days == 2
    assert weeks == 2


def test_funnel_step_computes_share_and_sorts_exclusions() -> None:
    from collections import Counter

    kept = (_episode(1, "ERA", datetime(2026, 7, 28, tzinfo=UTC)),)
    reasons = Counter({"a": 1, "b": 3})

    step = _funnel_step("kept", kept, previous_count=4, exclusion_reasons=reasons)

    assert step.episodes == 1
    assert step.share_of_previous_pct == pytest.approx(25.0)
    assert [row.name for row in step.exclusion_reasons] == ["b", "a"]


def test_funnel_step_first_step_has_no_share() -> None:
    step = _funnel_step("all", (), previous_count=None)

    assert step.share_of_previous_pct is None


# --- _finite_ratio ------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"longShortRatio": 1.6048}, 1.6048),
        ({"longShortRatio": 0}, None),  # non-positive rejected
        ({"longShortRatio": -1.2}, None),
        ({"longShortRatio": True}, None),  # bool must not pass as numeric
        ({"longShortRatio": float("nan")}, None),
        ({"longShortRatio": float("inf")}, None),
        ({"longShortRatio": "1.5"}, None),  # string, not numeric
        ({}, None),  # missing key
        (None, None),  # not even a dict
        ([1, 2, 3], None),
    ],
)
def test_finite_ratio_rejects_everything_but_a_positive_finite_number(
    payload: object,
    expected: float | None,
) -> None:
    assert _finite_ratio(payload) == expected


# --- evaluate_episode_feature (the core pure decision) -----------------------


def _points(
    baseline_value: float = 1.5,
    *,
    baseline_count: int = LSR_EXPECTED_BASELINE_POINTS,
    recent_count: int = LSR_EXPECTED_RECENT_POINTS,
    recent_value: float | None = 1.6,
    jitter: bool = True,
) -> list[tuple[datetime, float | None]]:
    points: list[tuple[datetime, float | None]] = []
    baseline_start = ANCHOR - timedelta(minutes=240)
    for index in range(baseline_count):
        ts = baseline_start + timedelta(minutes=5 * index)
        value = baseline_value + (0.001 * index if jitter else 0.0)
        points.append((ts, value))
    recent_start = ANCHOR - timedelta(minutes=30)
    for index in range(recent_count):
        ts = recent_start + timedelta(minutes=5 * index)
        points.append((ts, recent_value))
    return points


def test_feature_complete_when_exactly_42_plus_6_finite_points() -> None:
    result = evaluate_episode_feature(1, ANCHOR, _points())

    assert result.invalid_or_missing is False
    assert result.mad_zero is False
    assert result.feature_complete is True
    assert len(result.ratios) == LSR_EXPECTED_BASELINE_POINTS


def test_missing_points_are_invalid_not_feature_complete() -> None:
    short_series = _points(baseline_count=LSR_EXPECTED_BASELINE_POINTS - 1)

    result = evaluate_episode_feature(1, ANCHOR, short_series)

    assert result.invalid_or_missing is True
    assert result.feature_complete is False


def test_a_single_none_ratio_point_is_invalid() -> None:
    points = _points()
    points[10] = (points[10][0], None)

    result = evaluate_episode_feature(1, ANCHOR, points)

    assert result.invalid_or_missing is True
    assert result.feature_complete is False


def test_zero_mad_baseline_is_excluded_even_when_otherwise_complete() -> None:
    """A perfectly flat baseline series (every point identical) has MAD=0 —
    dividing by it later would be undefined, so it must never be feature-complete,
    per the registered contract ('MAD = 0' -> unresolved, not zero)."""
    result = evaluate_episode_feature(1, ANCHOR, _points(jitter=False))

    assert result.invalid_or_missing is False
    assert result.mad_zero is True
    assert result.feature_complete is False


def test_misaligned_baseline_recent_split_is_invalid_even_with_48_total_points() -> None:
    """41 baseline + 7 recent still sums to 48 — must not silently pass as if it
    were the registered 42/6 split."""
    points = _points(baseline_count=LSR_EXPECTED_BASELINE_POINTS - 1, recent_count=7)

    result = evaluate_episode_feature(1, ANCHOR, points)

    assert result.invalid_or_missing is True


def test_endpoint_staleness_is_measured_from_the_last_point_to_anchor() -> None:
    points = _points()
    last_point_at = points[-1][0]

    result = evaluate_episode_feature(1, ANCHOR, points)

    expected_minutes = (ANCHOR - last_point_at).total_seconds() / 60
    assert result.endpoint_staleness_minutes == pytest.approx(expected_minutes)


def test_no_points_at_all_is_invalid_with_no_staleness() -> None:
    result = evaluate_episode_feature(1, ANCHOR, [])

    assert result.invalid_or_missing is True
    assert result.endpoint_staleness_minutes is None


# --- summarize_liquidations_runs (pure aggregation) --------------------------


def test_summarize_liquidations_runs_separates_sampled_and_no_data() -> None:
    rows = [
        _LiquidationsRunRow(id=1, event_id=100, exchange="htx", status="sampled"),
        _LiquidationsRunRow(id=2, event_id=101, exchange="htx", status="no_data"),
        _LiquidationsRunRow(id=3, event_id=102, exchange="binance", status="sampled"),
        _LiquidationsRunRow(id=4, event_id=102, exchange="binance", status="symbol_unavailable"),
    ]

    appendix, sampled_run_ids = summarize_liquidations_runs(rows)

    assert appendix.episodes_with_data == 2
    assert appendix.episodes_no_data == 1
    assert appendix.distinct_exchanges == ("binance", "htx")
    assert sorted(sampled_run_ids) == [1, 3]
    # Sample-level fields are the caller's job to fill in after aggregating;
    # pure summarization never touches app.pump_derivatives_context_samples.
    assert appendix.total_samples == 0
    assert appendix.first_source_at is None


def test_summarize_liquidations_runs_handles_empty_input() -> None:
    appendix, sampled_run_ids = summarize_liquidations_runs([])

    assert appendix.episodes_with_data == 0
    assert appendix.distinct_exchanges == ()
    assert sampled_run_ids == []


# --- readiness ----------------------------------------------------------------


def _feature_complete_episodes(count: int, bases: int, weeks: int) -> tuple[ReplayEpisode, ...]:
    episodes = []
    week_start = datetime(2026, 7, 6, tzinfo=UTC)  # a Monday
    for index in range(count):
        base = f"BASE{index % bases}"
        at = week_start + timedelta(weeks=index % weeks, days=index % 3)
        episodes.append(_episode(index + 1, base, at))
    return tuple(episodes)


def test_readiness_is_collecting_below_any_single_threshold() -> None:
    below_episode_count = _feature_complete_episodes(
        MIN_FEATURE_COMPLETE_EPISODES - 1, MIN_BASES, MIN_UTC_WEEKS
    )

    verdict = _readiness(below_episode_count)

    assert verdict.status == "collecting"


def test_readiness_is_coverage_ready_once_all_three_thresholds_clear() -> None:
    episodes = _feature_complete_episodes(MIN_FEATURE_COMPLETE_EPISODES, MIN_BASES, MIN_UTC_WEEKS)

    verdict = _readiness(episodes)

    assert verdict.status == "coverage_ready"
    assert verdict.feature_complete_episodes == MIN_FEATURE_COMPLETE_EPISODES
    assert verdict.bases == MIN_BASES
    assert verdict.utc_weeks == MIN_UTC_WEEKS


def test_readiness_collecting_when_bases_are_below_threshold_despite_enough_episodes() -> None:
    episodes = _feature_complete_episodes(
        MIN_FEATURE_COMPLETE_EPISODES, MIN_BASES - 1, MIN_UTC_WEEKS
    )

    verdict = _readiness(episodes)

    assert verdict.status == "collecting"
    assert verdict.bases == MIN_BASES - 1


# --- SQL scope assertions (no live database; mirrors the existing repository
# test convention of compiling statements against the postgres dialect) ------


def test_binance_sourced_statement_scopes_exchange_and_conflict() -> None:
    sql = _sql(binance_sourced_event_ids_statement([1, 2, 3]))

    assert f"pump_event_sources.exchange = '{LSR_EXCHANGE}'" in sql
    assert "pump_event_sources.identity_conflict IS false" in sql
    assert "pump_event_sources.event_id IN (1, 2, 3)" in sql


def test_lsr_runs_statement_scopes_exchange_method_and_resolver_version() -> None:
    sql = _sql(lsr_runs_statement([42]))

    assert f"pump_derivatives_context_runs.exchange = '{LSR_EXCHANGE}'" in sql
    assert f"pump_derivatives_context_runs.method = '{LSR_METHOD}'" in sql
    assert (
        "pump_derivatives_context_runs.resolver_version = "
        f"'{DERIVATIVES_CONTEXT_RESOLVER_VERSION}'" in sql
    )


def test_lsr_window_samples_statement_never_touches_post_anchor_rows() -> None:
    sql = _sql(lsr_window_samples_statement([42]))

    assert f"pump_derivatives_context_runs.method = '{LSR_METHOD}'" in sql
    assert (
        "pump_derivatives_context_samples.source_at < app.pump_derivatives_context_runs.anchor_at"
        in sql
    )
    assert "pump_derivatives_context_samples.source_at >=" in sql


def test_liquidations_statement_is_not_scoped_to_a_single_exchange() -> None:
    """Deliberately unscoped by exchange — the whole point of this appendix is to
    show which venues have any liquidation coverage at all."""
    sql = _sql(liquidations_runs_statement([42]))

    assert "pump_derivatives_context_runs.method = 'liquidations'" in sql
    assert "pump_derivatives_context_runs.exchange = " not in sql
