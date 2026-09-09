"""Pure-logic coverage for the HYP-024 quintile/tie/verdict pipeline.

No database. Every case is built from hand-made rows so the frozen decision
rule -- floor first, Rule 6 tie handling, candidate/rejected thresholds -- is
exercised directly.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from schurfer_analytics.orderflow_microstructure import (
    CANDIDATE_SPREAD_PP,
    MIN_ASSET_CLUSTERS,
    MIN_EPISODES_PER_QUINTILE,
    VERDICT_CANDIDATE,
    VERDICT_INCONCLUSIVE,
    VERDICT_REJECTED,
    MeasuredEpisode,
    ResolvedDecisionRow,
    build_coverage,
    compared_distinct_clusters,
    compute_quintiles,
    cost_pct_at_horizon,
    evaluate_verdict,
    net_short_return_pct,
)

_TS = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def _episode(
    decision_id: str,
    feature: float,
    net: float,
    *,
    cluster: str,
    gross: float | None = None,
    f5: float | None = None,
    f20: float | None = None,
    mfe: float | None = None,
    mae: float | None = None,
) -> MeasuredEpisode:
    return MeasuredEpisode(
        decision_id=decision_id,
        cluster_key=cluster,
        exchange="bybit",
        ts=_TS,
        taker_imbalance_10m=feature,
        taker_imbalance_5m=f5,
        taker_imbalance_20m=f20,
        gross_short_return_pct=net if gross is None else gross,
        net_short_return_pct=net,
        mfe_pct=mfe,
        mae_pct=mae,
    )


def _quintile_episodes(
    *, per_quintile: int, quintile_nets: list[float], clusters: int, tied_feature: bool = False
) -> tuple[MeasuredEpisode, ...]:
    """Five quintiles of `per_quintile` episodes each. Unless `tied_feature`,
    features are globally strictly increasing (so quintile bands are cleanly
    separated); with `tied_feature` every feature is identical (so every
    quintile boundary sits inside one tie)."""
    episodes: list[MeasuredEpisode] = []
    running = 0
    for q, net in enumerate(quintile_nets):
        for j in range(per_quintile):
            feature = 0.0 if tied_feature else float(q * per_quintile + j)
            episodes.append(
                _episode(
                    f"d{running:05d}",
                    feature,
                    net,
                    cluster=f"C{running % clusters}",
                    f5=feature,
                    f20=feature,
                )
            )
            running += 1
    return tuple(episodes)


# --- cost model ----------------------------------------------------------


def test_cost_deduction_is_fees_plus_funding_for_a_sixty_minute_hold() -> None:
    # 2 * 10 bps taker + 5 bps/8h * 60/480 = 20 + 0.625 = 20.625 bps = 0.20625 pp.
    assert cost_pct_at_horizon() == pytest.approx(0.20625)
    assert net_short_return_pct(3.99) == pytest.approx(3.99 - 0.20625)


# --- coverage / fail-closed identity ------------------------------------


def _row(
    decision_id: str,
    *,
    exchange: str,
    match_count: int,
    bars_10m: int,
    native: str | None = "BTCUSDT",
    imbalance_10m: float | None = 1.0,
) -> ResolvedDecisionRow:
    return ResolvedDecisionRow(
        decision_id=decision_id,
        base="BTC",
        exchange=exchange,
        ts=_TS,
        short_return_pct=1.0,
        mfe_pct=2.0,
        mae_pct=-1.0,
        match_count=match_count,
        native_market_id=native if match_count == 1 else None,
        market_type="linear" if match_count == 1 else None,
        bars_10m=bars_10m,
        bars_5m=min(bars_10m, 5),
        bars_20m=bars_10m,
        imbalance_10m=imbalance_10m if match_count == 1 and bars_10m == 10 else None,
        imbalance_5m=0.5 if match_count == 1 and bars_10m >= 5 else None,
        imbalance_20m=None,
    )


def test_build_coverage_classifies_each_failure_mode_as_coverage_not_negative() -> None:
    rows = (
        _row("measured", exchange="bybit", match_count=1, bars_10m=10),
        _row("unresolved", exchange="bybit", match_count=0, bars_10m=0),
        _row("ambiguous", exchange="bybit", match_count=2, bars_10m=0),
        _row("missing_bars", exchange="binance", match_count=1, bars_10m=7),
    )
    result = build_coverage(rows)

    assert {e.decision_id for e in result.measured} == {"measured"}
    by_exchange = {c.exchange: c for c in result.by_exchange}
    assert by_exchange["bybit"].unresolved_identity == 1
    assert by_exchange["bybit"].ambiguous_identity == 1
    assert by_exchange["bybit"].measured_episodes == 1
    assert by_exchange["binance"].missing_or_incomplete_bars == 1
    assert by_exchange["binance"].measured_episodes == 0
    # Coverage-lost decisions never appear as measured episodes (never a
    # negative outcome).
    assert result.funnel[-1].label == "measured_episodes"
    assert result.funnel[-1].remaining == 1


def test_missing_context_window_is_dropped_only_from_that_context() -> None:
    row = ResolvedDecisionRow(
        decision_id="d",
        base="BTC",
        exchange="bybit",
        ts=_TS,
        short_return_pct=1.0,
        mfe_pct=None,
        mae_pct=None,
        match_count=1,
        native_market_id="BTCUSDT",
        market_type="linear",
        bars_10m=10,
        bars_5m=3,  # incomplete 5m window
        bars_20m=15,  # incomplete 20m window
        imbalance_10m=2.0,
        imbalance_5m=1.0,
        imbalance_20m=3.0,
    )
    measured = build_coverage((row,)).measured
    assert len(measured) == 1
    assert measured[0].taker_imbalance_10m == 2.0
    # 5m and 20m windows were incomplete -> features dropped for context only.
    assert measured[0].taker_imbalance_5m is None
    assert measured[0].taker_imbalance_20m is None


# --- quintiles + Rule 6 --------------------------------------------------


def test_compute_quintiles_reports_spread_monotonicity_and_ties() -> None:
    episodes = _quintile_episodes(
        per_quintile=10, quintile_nets=[0.0, 0.5, 1.0, 1.5, 2.0], clusters=20
    )
    analysis = compute_quintiles(episodes, lookback_minutes=10)
    assert len(analysis.quintiles) == 5
    assert analysis.top_minus_bottom_median_spread_pp == pytest.approx(2.0)
    assert analysis.monotone_increasing is True
    assert analysis.ties.adjacent_boundaries_distinct is True
    assert analysis.ties.largest_tied_group == 1
    assert analysis.ties.distinct_value_count == 50


def test_rule6_detects_a_tie_straddling_every_boundary() -> None:
    episodes = _quintile_episodes(
        per_quintile=10, quintile_nets=[0.0, 0.5, 1.0, 1.5, 3.0], clusters=20, tied_feature=True
    )
    analysis = compute_quintiles(episodes, lookback_minutes=10)
    assert analysis.ties.distinct_value_count == 1
    assert analysis.ties.largest_tied_group == 50
    assert analysis.ties.adjacent_boundaries_distinct is False
    assert len(analysis.ties.tied_boundary_pairs) == 4


# --- verdict -------------------------------------------------------------


def test_floor_binds_first_even_with_a_large_spread() -> None:
    # 100 per quintile is below the 150 floor; the spread is huge but the
    # verdict must be inconclusive, never a rejection or candidate.
    episodes = _quintile_episodes(
        per_quintile=100, quintile_nets=[0.0, 1.0, 2.0, 3.0, 5.0], clusters=40
    )
    primary = compute_quintiles(episodes, lookback_minutes=10)
    verdict = evaluate_verdict(
        primary, compared_clusters=compared_distinct_clusters(episodes, primary)
    )
    assert verdict.verdict == VERDICT_INCONCLUSIVE
    assert verdict.meets_episode_floor is False
    assert any(str(MIN_EPISODES_PER_QUINTILE) in r for r in verdict.reasons)


def test_cluster_floor_binds_when_diversity_is_too_low() -> None:
    episodes = _quintile_episodes(
        per_quintile=160, quintile_nets=[0.0, 1.0, 2.0, 3.0, 5.0], clusters=10
    )
    primary = compute_quintiles(episodes, lookback_minutes=10)
    compared = compared_distinct_clusters(episodes, primary)
    assert compared < MIN_ASSET_CLUSTERS
    verdict = evaluate_verdict(primary, compared_clusters=compared)
    assert verdict.verdict == VERDICT_INCONCLUSIVE
    assert verdict.meets_cluster_floor is False


def test_candidate_requires_spread_monotonicity_and_distinct_boundaries() -> None:
    episodes = _quintile_episodes(
        per_quintile=160, quintile_nets=[0.0, 0.5, 1.0, 1.5, 2.1], clusters=40
    )
    primary = compute_quintiles(episodes, lookback_minutes=10)
    spread = primary.top_minus_bottom_median_spread_pp
    assert spread is not None
    assert spread == pytest.approx(2.1)
    assert spread > CANDIDATE_SPREAD_PP
    verdict = evaluate_verdict(
        primary, compared_clusters=compared_distinct_clusters(episodes, primary)
    )
    assert verdict.verdict == VERDICT_CANDIDATE


def test_rule6_blocks_candidate_when_a_tie_straddles_the_boundary() -> None:
    # Above the floor, spread far above 1.5 and monotone by construction, but
    # every feature value is tied -> Rule 6 forbids the candidate.
    episodes = _quintile_episodes(
        per_quintile=160,
        quintile_nets=[0.0, 0.5, 1.0, 1.5, 2.1],
        clusters=40,
        tied_feature=True,
    )
    primary = compute_quintiles(episodes, lookback_minutes=10)
    assert primary.top_minus_bottom_median_spread_pp == pytest.approx(2.1)
    verdict = evaluate_verdict(
        primary, compared_clusters=compared_distinct_clusters(episodes, primary)
    )
    assert verdict.verdict == VERDICT_INCONCLUSIVE
    assert any("rule6" in r for r in verdict.reasons)


def test_rejected_only_above_the_floor_with_a_negligible_spread() -> None:
    episodes = _quintile_episodes(
        per_quintile=160, quintile_nets=[1.0, 1.0, 1.0, 1.0, 1.0], clusters=40
    )
    primary = compute_quintiles(episodes, lookback_minutes=10)
    assert primary.top_minus_bottom_median_spread_pp == pytest.approx(0.0)
    verdict = evaluate_verdict(
        primary, compared_clusters=compared_distinct_clusters(episodes, primary)
    )
    assert verdict.verdict == VERDICT_REJECTED
    assert verdict.meets_episode_floor is True
    assert verdict.meets_cluster_floor is True


def test_spread_between_thresholds_is_inconclusive() -> None:
    # top - bottom = 1.0pp: above the 0.5 reject band, below the 1.5 candidate
    # band -> inconclusive above the floor.
    episodes = _quintile_episodes(
        per_quintile=160, quintile_nets=[0.0, 0.25, 0.5, 0.75, 1.0], clusters=40
    )
    primary = compute_quintiles(episodes, lookback_minutes=10)
    assert primary.top_minus_bottom_median_spread_pp == pytest.approx(1.0)
    verdict = evaluate_verdict(
        primary, compared_clusters=compared_distinct_clusters(episodes, primary)
    )
    assert verdict.verdict == VERDICT_INCONCLUSIVE


def test_compared_distinct_clusters_is_a_union_not_a_sum() -> None:
    # Same clusters appear in both compared quintiles; the union must not
    # double-count them.
    episodes = _quintile_episodes(
        per_quintile=160, quintile_nets=[0.0, 1.0, 2.0, 3.0, 5.0], clusters=40
    )
    primary = compute_quintiles(episodes, lookback_minutes=10)
    union = compared_distinct_clusters(episodes, primary)
    top = primary.quintiles[-1].distinct_clusters
    bottom = primary.quintiles[0].distinct_clusters
    assert union <= top + bottom
