"""Coverage for HYP-023's score-component study.

The tests that matter here are the ones a colleague's review implies: that the
study cannot count a hundred views of one pump as a hundred observations, that
it cannot turn missing data into a negative finding, and that it cannot measure
an outcome outside its own window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.episode_selection import episode_decision_query
from schurfer_analytics.research_contract import ResearchContract
from schurfer_analytics.score_component_study import (
    component_value,
    horizon_cost_pct,
    incomplete_outcome_episodes,
    select_one_per_episode,
    study_component,
    within_window,
)

_SINCE = datetime(2026, 7, 29, tzinfo=UTC)
_UNTIL = datetime(2026, 8, 25, tzinfo=UTC)


def _contract(**over: object) -> ResearchContract:
    defaults: dict[str, object] = {
        "hypothesis_id": "HYP-023",
        "contract_version": "v1",
        "window_since": _SINCE,
        "window_until": _UNTIL,
        "strategy_versions": ("pump_short_v1_market_quality",),
        "allow_fallback": False,
        "outcome_horizon_minutes": 60,
        "metric": "median",
        "comparison": "difference_of_metric",
        "baseline_policy": "bottom_quintile",
        "challenger_policies": ("top_quintile",),
        "cost_model_version": "conservative_costs_v1",
        "minimum_completed_trades": 3,
        "minimum_clusters": 2,
        "candidate_margin": 1.5,
        "rejection_margin": -1.5,
    }
    defaults.update(over)
    return ResearchContract(**defaults)  # type: ignore[arg-type]


def _row(
    episode: int,
    *,
    minutes: int = 0,
    action: str = "skipped",
    oi_trend: float = 0.0,
    ret: float = 0.0,
    base: str | None = None,
) -> dict[str, object]:
    return {
        "pump_event_id": episode,
        "base": base or f"TOK{episode}",
        "ts": _SINCE + timedelta(minutes=minutes),
        "action": action,
        # The real shape: five components are objects carrying both the raw
        # measurement and the 0-2 points the score sums; mad_score is a bare
        # number. Faking only one shape would have hidden the crash that found
        # this.
        "components": {
            "oi_trend": {"value": oi_trend, "points": 1, "max": 2, "note": ""},
            "pump_age": {"value": float(minutes), "points": 0, "max": 2, "note": ""},
        },
        "short_return_pct": ret,
    }


# --- one observation per episode --------------------------------------------


def test_hundreds_of_views_of_one_pump_are_one_observation() -> None:
    """The scanner evaluates a live pump about once a minute, so an episode
    yields hundreds of decisions whose 60-minute outcomes overlap almost
    entirely. Counting rows would clear any evidence floor on a handful of
    market events."""
    rows = [_row(1, minutes=index) for index in range(200)]
    assert len(select_one_per_episode(rows)) == 1


def test_an_opened_decision_wins_over_an_earlier_skip() -> None:
    """The registered rule, and the same one the replay uses: the first decision
    that opened something, else the earliest. Two research lines that disagree
    about which decision represents an episode cannot be compared."""
    rows = [
        _row(1, minutes=0, action="skipped", oi_trend=1.0),
        _row(1, minutes=5, action="opened_dry_run", oi_trend=2.0),
        _row(1, minutes=9, action="opened", oi_trend=3.0),
    ]
    selected = select_one_per_episode(rows)
    assert len(selected) == 1
    # The first opened one, not the last and not the earliest overall.
    assert selected[0].components["oi_trend"] == 2.0


def test_the_earliest_decision_is_used_when_nothing_opened() -> None:
    rows = [_row(1, minutes=7, oi_trend=7.0), _row(1, minutes=2, oi_trend=2.0)]
    assert select_one_per_episode(rows)[0].components["oi_trend"] == 2.0


# --- the window boundary ----------------------------------------------------


def test_an_outcome_reaching_past_the_window_is_dropped() -> None:
    """A decision forty minutes before the window closes has a 60-minute outcome
    measured partly outside it. In a discovery and holdout split those minutes
    belong to the other side."""
    inside_minutes = int((_UNTIL - _SINCE).total_seconds() / 60) - 61
    straddling_minutes = int((_UNTIL - _SINCE).total_seconds() / 60) - 40
    observations = select_one_per_episode(
        [_row(1, minutes=inside_minutes), _row(2, minutes=straddling_minutes)]
    )
    kept = within_window(observations, _contract())
    assert {item.pump_event_id for item in kept} == {1}


# --- sufficiency before either verdict --------------------------------------


def test_thin_coverage_is_inconclusive_not_a_negative_finding() -> None:
    """The asymmetry a colleague found: the first draft required data to call
    something a candidate and nothing at all to call it dead. mad_score is
    recorded on 4,067 of 62,168 decisions and would have been the first
    casualty."""
    rows = [_row(episode, oi_trend=float(episode), ret=0.0) for episode in range(1, 8)]
    result = study_component(
        select_one_per_episode(rows), "oi_trend", _contract(minimum_completed_trades=50)
    )
    assert result.verdict == "inconclusive"
    assert "below the floor" in result.detail


def test_too_few_clusters_is_also_inconclusive() -> None:
    """Fifty episodes of one token that pumped repeatedly is one observation
    about that token, and no amount of row counting distinguishes them."""
    rows = [
        _row(episode, oi_trend=float(episode), ret=0.0, base="SAME") for episode in range(1, 30)
    ]
    result = study_component(
        select_one_per_episode(rows), "oi_trend", _contract(minimum_clusters=10)
    )
    assert result.verdict == "inconclusive"
    assert "clusters" in result.detail


def test_a_component_absent_from_every_row_cannot_be_judged() -> None:
    rows = [_row(episode, oi_trend=float(episode)) for episode in range(1, 30)]
    result = study_component(select_one_per_episode(rows), "mad_score", _contract())
    assert result.verdict == "inconclusive"
    assert result.coverage_episodes == 0


# --- what a signal has to look like -----------------------------------------


def _monotone_rows(step: float) -> list[dict[str, object]]:
    """Thirty episodes whose outcome rises with the component."""
    return [_row(episode, oi_trend=float(episode), ret=episode * step) for episode in range(1, 31)]


def test_a_strong_monotone_relationship_is_a_candidate() -> None:
    result = study_component(select_one_per_episode(_monotone_rows(0.5)), "oi_trend", _contract())
    assert result.verdict == "candidate"
    assert result.monotone
    assert result.spread_pct is not None
    assert result.spread_pct > 1.5


def test_a_flat_relationship_is_no_signal() -> None:
    result = study_component(select_one_per_episode(_monotone_rows(0.0)), "oi_trend", _contract())
    assert result.verdict == "no_signal"


def test_a_non_monotone_spread_is_not_promoted() -> None:
    """Two tails can differ for reasons that have nothing to do with the
    ordering the component claims to impose, so a relationship that is strong
    only at the extremes is reported rather than believed."""
    # Quintile medians run -10, +5, -5, +2, +10: a large end-to-end spread that
    # reverses twice in between, so the ordering the component claims to impose
    # is not what the outcome follows.
    by_quintile = {0: -10.0, 1: 5.0, 2: -5.0, 3: 2.0, 4: 10.0}
    rows = [
        _row(episode, oi_trend=float(episode), ret=by_quintile[(episode - 1) // 6])
        for episode in range(1, 31)
    ]
    result = study_component(select_one_per_episode(rows), "oi_trend", _contract())
    assert not result.monotone
    assert result.verdict == "inconclusive"
    assert "monotone" in result.detail


# --- costs ------------------------------------------------------------------


def test_the_shared_cost_model_is_used_as_is() -> None:
    """Two taker fees plus funding prorated over the horizon. A study that
    invents its own costs is not comparable with the replay, which is the whole
    point of having one model."""
    assert horizon_cost_pct(60) == pytest.approx(0.2 + 5.0 * (60 / 480) / 100, abs=1e-9)
    assert horizon_cost_pct(480) == pytest.approx(0.25, abs=1e-9)


def test_net_return_is_gross_minus_the_horizon_cost() -> None:
    observation = select_one_per_episode([_row(1, ret=1.0)])[0]
    assert observation.net_short_return_pct(60) == pytest.approx(1.0 - horizon_cost_pct(60))


# --- the two shapes a component can take ------------------------------------


def test_the_raw_measurement_is_read_not_the_score_points() -> None:
    """Five components record both a `value` -- the measurement -- and `points`,
    the 0-2 contribution the composite sums. Reading points would be asking the
    question HYP-019 already answered; the ingredient is the measurement."""
    assert component_value({"value": 321.74, "points": 0, "max": 2, "note": "x"}) == 321.74


def test_a_bare_number_component_is_read_directly() -> None:
    """mad_score is recorded as a plain float rather than an object. The first
    run crashed on exactly this: float() of a dict."""
    assert component_value(1.3883495145631297) == pytest.approx(1.38834951)


def test_a_component_with_no_measurement_is_absent_rather_than_zero() -> None:
    """Absent and zero are different things, and treating one as the other would
    put every episode missing a component into the same quintile."""
    assert component_value(None) is None
    assert component_value({"points": 1, "max": 2, "note": "no value recorded"}) is None
    assert component_value("not a number") is None


# --- a partition the component does not impose ------------------------------


def _tied_rows() -> list[dict[str, object]]:
    """Thirty episodes whose outcome rises steadily while the component is a
    single value across the middle three quintiles."""
    tiers = {0: 0.0, 1: 1.0, 2: 1.0, 3: 1.0, 4: 2.0}
    return [
        _row(
            episode,
            oi_trend=tiers[(episode - 1) // 6],
            ret=float((episode - 1) // 6),
        )
        for episode in range(1, 31)
    ]


def test_a_partition_split_through_tied_values_is_not_a_candidate() -> None:
    """The defect that produced HYP-023's first pump_age result. 368 of 821
    episodes read exactly 0.6 minutes, so quintiles two and three sat wholly
    inside one value and the difference between their medians was decided by the
    sort order. Monotone and wide, and still not a finding."""
    result = study_component(select_one_per_episode(_tied_rows()), "oi_trend", _contract())
    assert result.monotone
    assert result.spread_pct is not None
    assert result.spread_pct > 1.5
    assert not result.separated
    assert result.tied_boundaries == 2
    assert result.verdict == "inconclusive"
    assert "tied value" in result.detail


def test_the_tie_check_only_ever_withdraws_a_candidate() -> None:
    """Stated as a test because it is the argument for fixing this after the
    first result was read: the check can turn a candidate into inconclusive and
    can never turn anything into a candidate."""
    separated = study_component(
        select_one_per_episode(_monotone_rows(0.5)), "oi_trend", _contract()
    )
    assert separated.separated
    assert separated.verdict == "candidate"


def test_the_distribution_behind_a_verdict_is_reported() -> None:
    """Coverage alone hid this: 821 episodes carrying 142 distinct values looks
    identical to 821 carrying 821 until the numbers are printed."""
    result = study_component(select_one_per_episode(_tied_rows()), "oi_trend", _contract())
    assert result.coverage_episodes == 30
    assert result.distinct_values == 3
    assert result.largest_tied_group == 18


# --- the episode's decision is chosen before its outcome is known ------------


def test_an_episode_whose_decision_has_no_outcome_is_dropped_not_substituted() -> None:
    """The defect a colleague reproduced on a real Postgres. The SQL used to
    filter to decisions with a completed outcome and only then apply the
    "first opened, else earliest" rule, so an episode whose first `opened`
    decision was still unresolved came back represented by a later `skipped` one
    -- a different decision, at a different age, in a different group. On the
    HYP-023 discovery window it fired on 24 of 822 episodes."""
    rows = [
        {**_row(1, minutes=0, action="opened", oi_trend=1.0), "short_return_pct": None},
        _row(1, minutes=6, action="skipped", oi_trend=9.0, ret=3.0),
    ]
    # Both rows belong to episode 1; the opened one represents it and has no
    # outcome, so the episode contributes nothing rather than contributing the
    # skipped one's components.
    assert select_one_per_episode(rows) == ()


def test_the_dropped_episode_is_counted_as_coverage() -> None:
    """Dropping it silently would shrink the denominator invisibly, which reads
    as a population that simply is that size."""
    rows = [
        {**_row(1, action="opened"), "short_return_pct": None},
        _row(2, ret=1.0),
    ]
    assert incomplete_outcome_episodes(rows) == 1
    assert len(select_one_per_episode(rows)) == 1


def test_a_complete_episode_is_unaffected() -> None:
    rows = [_row(1, ret=1.0), _row(2, ret=2.0)]
    assert incomplete_outcome_episodes(rows) == 0
    assert len(select_one_per_episode(rows)) == 2


def test_the_sql_selects_the_episode_before_joining_the_outcome() -> None:
    """Read as text because the ordering of these two operations is the whole
    defect, and a test that mocked the database would assert nothing about it."""
    query = episode_decision_query("short_return_pct")
    selection = query.index("DISTINCT ON (d.pump_event_id)")
    outcome_join = query.index("LEFT JOIN app.trade_decision_outcomes")
    assert selection < outcome_join
    # A LEFT join, so an episode with no completed outcome still comes back and
    # can be counted, rather than vanishing from the population.
    assert "LEFT JOIN app.trade_decision_outcomes" in query
    assert query.count("JOIN app.trade_decision_outcomes") == 1
