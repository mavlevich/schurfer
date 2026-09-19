"""Pure-verdict tests for the draft HYP-015 hold12h rule.

The gate ORDER is the safety property under test: economic maturity first, then
negative EV BEFORE the diversity floor, then significance, then the portfolio win.
"""

from __future__ import annotations

import dataclasses

import pytest
from schurfer_analytics.momentum_flow_hold12h_verdict import (
    HOLD12H_VERDICT_CONTRACT,
    Hold12hVerdictContract,
    VerdictInputs,
    VerdictOutcome,
    decide_verdict,
)

_CONTRACT = HOLD12H_VERDICT_CONTRACT


def _passing() -> VerdictInputs:
    """Inputs that clear every gate -> candidate. Each test perturbs one field."""
    return VerdictInputs(
        analyzable_pairs=150,
        ci_computable=True,
        standalone_720_mean_net=0.010,
        standalone_720_ci_lower=0.005,
        paired_diff_ci_lower=0.003,
        distinct_asset_clusters=25,
        distinct_utc_weeks=6,
        max_single_asset_fraction=0.10,
        max_single_week_fraction=0.20,
        rejected_stale_fraction=0.10,
        unresolved_fraction=0.05,
        accounting_incomplete_fraction=0.05,
        identity_unresolved_fraction=0.0,
        integrity_failure_fraction=0.0,
        portfolio_720_window_pnl_usd=40.0,
        portfolio_240_window_pnl_usd=20.0,
    )


def _decide(**overrides: object) -> VerdictOutcome:
    inputs = dataclasses.replace(_passing(), **overrides)  # type: ignore[arg-type]
    return decide_verdict(_CONTRACT, inputs).outcome


def test_all_gates_cleared_is_a_candidate() -> None:
    result = decide_verdict(_CONTRACT, _passing())
    assert result.outcome is VerdictOutcome.CANDIDATE
    assert result.gate == "pass"


def test_gate_a_too_few_pairs_is_insufficient_data() -> None:
    assert _decide(analyzable_pairs=50) is VerdictOutcome.INSUFFICIENT_DATA


def test_gate_a_precedes_negative_ev() -> None:
    # Too few pairs AND a negative mean: with an immature sample the mean is not
    # trustworthy, so maturity binds first -> insufficient_data, not reject.
    assert (
        _decide(analyzable_pairs=50, standalone_720_mean_net=-0.02)
        is VerdictOutcome.INSUFFICIENT_DATA
    )


def test_gate_0_non_finite_input_fails_closed() -> None:
    # A NaN would pass every ">" comparison to "candidate"; it must fail-closed instead.
    assert _decide(portfolio_720_window_pnl_usd=float("nan")) is VerdictOutcome.INSUFFICIENT_DATA
    assert _decide(standalone_720_mean_net=float("inf")) is VerdictOutcome.INSUFFICIENT_DATA


def test_gate_0b_any_integrity_failure_fails_closed() -> None:
    # ANY integrity failure blocks, regardless of how small the fraction.
    assert _decide(integrity_failure_fraction=0.0001) is VerdictOutcome.INSUFFICIENT_DATA


def test_negative_ev_is_not_masked_by_uncomputable_ci() -> None:
    # THE P1 fix: a mature LOSING sample too narrow to bootstrap (ci_computable False)
    # must reject at Gate B, NOT hide as insufficient_data at Gate A.
    result = decide_verdict(
        _CONTRACT,
        dataclasses.replace(_passing(), ci_computable=False, standalone_720_mean_net=-0.01),
    )
    assert result.outcome is VerdictOutcome.REJECT_HOLD12H
    assert result.gate == "B"


def test_gate_d_uncomputable_ci_is_insufficient_evidence() -> None:
    # A profitable-mean mature sample that is too narrow to bootstrap is not yet evidence.
    assert _decide(ci_computable=False) is VerdictOutcome.INSUFFICIENT_EVIDENCE


@pytest.mark.parametrize("mean_net", [-0.001, 0.0])
def test_gate_b_mature_non_positive_ev_is_rejected(mean_net: float) -> None:
    assert _decide(standalone_720_mean_net=mean_net) is VerdictOutcome.REJECT_HOLD12H


def test_gate_b_negative_ev_binds_before_diversity_floor() -> None:
    # THE central pre-registration guarantee: a mature but narrow LOSING sample is a
    # rejection, never excused as "insufficient data" by low diversity.
    result = decide_verdict(
        _CONTRACT,
        dataclasses.replace(
            _passing(),
            standalone_720_mean_net=-0.005,
            distinct_asset_clusters=1,
            distinct_utc_weeks=1,
            max_single_asset_fraction=0.99,
        ),
    )
    assert result.outcome is VerdictOutcome.REJECT_HOLD12H
    assert result.gate == "B"


def test_gate_c_too_few_clusters_is_insufficient_data() -> None:
    assert _decide(distinct_asset_clusters=5) is VerdictOutcome.INSUFFICIENT_DATA


def test_gate_c_too_few_weeks_is_insufficient_data() -> None:
    assert _decide(distinct_utc_weeks=2) is VerdictOutcome.INSUFFICIENT_DATA


def test_gate_c_single_asset_concentration_is_insufficient_data() -> None:
    assert _decide(max_single_asset_fraction=0.50) is VerdictOutcome.INSUFFICIENT_DATA


@pytest.mark.parametrize(
    "field",
    ["rejected_stale_fraction", "unresolved_fraction", "accounting_incomplete_fraction"],
)
def test_gate_c_missingness_ceiling_is_insufficient_data(field: str) -> None:
    assert _decide(**{field: 0.95}) is VerdictOutcome.INSUFFICIENT_DATA


@pytest.mark.parametrize("ci_lower", [0.0, -0.01])
def test_gate_d_standalone_ci_crossing_zero_is_insufficient_evidence(ci_lower: float) -> None:
    assert _decide(standalone_720_ci_lower=ci_lower) is VerdictOutcome.INSUFFICIENT_EVIDENCE


def test_gate_e_no_paired_edge_is_no_duration_improvement() -> None:
    assert _decide(paired_diff_ci_lower=0.0) is VerdictOutcome.NO_DURATION_IMPROVEMENT


def test_gate_e_portfolio_does_not_beat_240_by_enough() -> None:
    # +$5 improvement < the $15 minimum.
    assert (
        _decide(portfolio_720_window_pnl_usd=25.0, portfolio_240_window_pnl_usd=20.0)
        is VerdictOutcome.NO_DURATION_IMPROVEMENT
    )


def test_gate_e_losing_portfolio_is_not_a_candidate_even_if_it_beats_240() -> None:
    # THE P1 fix: 720m loses ($-5) but "beats" a worse-losing 240m ($-25) by $20 -- not
    # an edge. A profitable absolute window is required.
    assert (
        _decide(portfolio_720_window_pnl_usd=-5.0, portfolio_240_window_pnl_usd=-25.0)
        is VerdictOutcome.NO_DURATION_IMPROVEMENT
    )


def test_contract_sha_is_deterministic_and_stable() -> None:
    a = Hold12hVerdictContract().sha256_hex()
    b = Hold12hVerdictContract().sha256_hex()
    assert a == b == HOLD12H_VERDICT_CONTRACT.sha256_hex()
    assert len(a) == 64


def test_contract_rejects_invalid_thresholds() -> None:
    with pytest.raises(ValueError, match="min_analyzable_pairs"):
        Hold12hVerdictContract(min_analyzable_pairs=0)
    with pytest.raises(ValueError, match="confidence_level"):
        Hold12hVerdictContract(confidence_level=1.5)
    with pytest.raises(ValueError, match="max_single_asset_fraction"):
        Hold12hVerdictContract(max_single_asset_fraction=0.0)


def test_contract_rejects_portfolio_exceeding_the_bank() -> None:
    # 6 x $100 = $600 cannot fit a $300 bank.
    with pytest.raises(ValueError, match="must not exceed bank_usd"):
        Hold12hVerdictContract(position_usd=100.0, max_concurrent_slots=6)
