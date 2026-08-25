from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from schurfer_analytics.early_momentum_unused_flow_features import (
    CANDIDATE_IMBALANCE_MAX,
    CANDIDATE_IMBALANCE_MIN,
    RawFeatureRow,
    analyze,
    build_observations,
)

T0 = datetime(2026, 8, 24, tzinfo=UTC)


def _notionals(imbalance: float, total: float = 100.0) -> tuple[float, float]:
    return total * (1 + imbalance) / 2, total * (1 - imbalance) / 2


def _row(
    trade_id: int,
    *,
    imbalance: float = 0.3,
    prior_imbalance: float = 0.0,
    burst_imbalance: float = 0.3,
    turnover: float = 0.03,
    net_return: float = 1.0,
) -> RawFeatureRow:
    decision = T0 + timedelta(minutes=trade_id)
    buy_15m, sell_15m = _notionals(imbalance)
    buy_prior, sell_prior = _notionals(prior_imbalance, 500.0)
    buy_burst, sell_burst = _notionals(burst_imbalance, 50.0)
    return RawFeatureRow(
        trade_id=trade_id,
        episode_id=f"episode-{trade_id}",
        cluster_key=f"cluster-{trade_id}",
        source_exchange="bybit",
        source_native_id=f"S{trade_id}USDT",
        decision_bucket=decision,
        entry_at=decision + timedelta(minutes=1),
        exit_at=decision + timedelta(hours=4),
        net_pnl_pct=net_return,
        net_pnl_usd=net_return,
        bars_observed=121,
        distinct_buckets=121,
        first_bucket=decision - timedelta(minutes=120),
        last_bucket=decision,
        max_gap_seconds=60.0,
        complete_bars=121,
        buy_15m=buy_15m,
        sell_15m=sell_15m,
        buy_prior=buy_prior,
        sell_prior=sell_prior,
        buy_burst_15m=buy_burst,
        sell_burst_15m=sell_burst,
        oi_value_latest=100.0 / turnover,
    )


def test_features_are_normalized_and_point_in_time_window_is_exact() -> None:
    observations, exclusions = build_observations((_row(1),))
    assert exclusions == {}
    assert len(observations) == 1
    observation = observations[0]
    assert round(observation.imbalance_15m, 6) == 0.3
    assert round(observation.imbalance_acceleration, 6) == 0.3
    assert round(observation.burst_imbalance_15m, 6) == 0.3
    assert round(observation.turnover_to_oi_15m, 6) == 0.03


def test_internal_gap_or_partial_window_fails_closed() -> None:
    gapped = replace(_row(1), max_gap_seconds=120.0)
    partial = replace(_row(2), bars_observed=120, distinct_buckets=120)
    observations, exclusions = build_observations((gapped, partial))
    assert observations == ()
    assert exclusions == {
        "non_contiguous_window": 1,
        "not_exactly_121_distinct_bars": 1,
    }


def test_zero_flow_or_missing_oi_value_never_becomes_a_zero_feature() -> None:
    zero_flow = replace(_row(1), buy_15m=0.0, sell_15m=0.0)
    missing_oi = replace(_row(2), oi_value_latest=None)
    observations, exclusions = build_observations((zero_flow, missing_oi))
    assert observations == ()
    assert exclusions == {
        "missing_or_invalid_oi_value": 1,
        "zero_or_invalid_feature_denominator": 1,
    }


def test_candidate_bounds_are_inclusive_low_and_exclusive_high() -> None:
    rows = (
        _row(1, imbalance=CANDIDATE_IMBALANCE_MIN, net_return=1.0),
        _row(2, imbalance=CANDIDATE_IMBALANCE_MAX - 0.001, net_return=2.0),
        _row(3, imbalance=CANDIDATE_IMBALANCE_MAX, net_return=-3.0),
        _row(4, imbalance=CANDIDATE_IMBALANCE_MIN - 0.001, net_return=-4.0),
    )
    result = analyze(rows)
    assert result.candidate.selected_trades == 2
    assert result.candidate.rejected_to_cash == 2
    assert result.candidate.selected_total_net_pnl_usd == 3.0


def test_quartiles_expose_non_monotonic_shape_instead_of_assuming_correlation() -> None:
    returns = (-1.0, -1.0, 2.0, 2.0, 1.0, 1.0, -3.0, -3.0)
    rows = tuple(
        _row(index, imbalance=-0.4 + index * 0.15, net_return=net_return)
        for index, net_return in enumerate(returns, start=1)
    )
    result = analyze(rows)
    imbalance_quartiles = [q for q in result.quartiles if q.feature == "imbalance_15m"]
    assert len(imbalance_quartiles) == 4
    assert imbalance_quartiles[1].mean_net_return_pct > imbalance_quartiles[0].mean_net_return_pct
    assert imbalance_quartiles[3].mean_net_return_pct < imbalance_quartiles[2].mean_net_return_pct


def test_dataset_fingerprint_changes_when_one_source_value_changes() -> None:
    original = analyze((_row(1),))
    changed = analyze((replace(_row(1), buy_15m=80.0),))
    assert original.dataset_fingerprint != changed.dataset_fingerprint
