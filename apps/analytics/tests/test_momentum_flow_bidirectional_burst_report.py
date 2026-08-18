from __future__ import annotations

from datetime import UTC, datetime

import pytest
from schurfer_analytics.challenger_inference import (
    DEFAULT_INFERENCE_SETTINGS,
    ChallengerFormalResult,
    ChallengerInference,
    InferenceReadiness,
    PairedInference,
    StrategyInference,
)
from schurfer_analytics.clustered_inference import BootstrapEstimate
from schurfer_analytics.momentum_flow_bidirectional_burst_report import (
    BidirectionalBurstReport,
    BurstStudyWindow,
    DirectionSummary,
    check_candidate_count,
    render_json,
    render_markdown,
)
from schurfer_analytics.momentum_flow_bidirectional_burst_study import HorizonEconomics

_SINCE = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
_UNTIL = datetime(2026, 8, 18, 0, 0, tzinfo=UTC)


def _window(**overrides: object) -> BurstStudyWindow:
    defaults: dict[str, object] = {
        "since": _SINCE,
        "until": _UNTIL,
        "extreme_threshold_pct": 10.0,
        "refractory_minutes": 60,
        "min_volume_24h_usd": 50_000.0,
    }
    defaults.update(overrides)
    return BurstStudyWindow(**defaults)  # type: ignore[arg-type]


def test_window_rejects_since_after_until() -> None:
    with pytest.raises(ValueError, match="since must be earlier"):
        _window(since=_UNTIL, until=_SINCE)


def test_window_rejects_non_positive_threshold() -> None:
    with pytest.raises(ValueError, match="extreme_threshold_pct"):
        _window(extreme_threshold_pct=0)


def test_window_rejects_non_positive_refractory() -> None:
    with pytest.raises(ValueError, match="refractory_minutes"):
        _window(refractory_minutes=0)


def test_window_rejects_negative_min_volume() -> None:
    with pytest.raises(ValueError, match="min_volume_24h_usd"):
        _window(min_volume_24h_usd=-1.0)


def test_check_candidate_count_raises_over_the_cap() -> None:
    with pytest.raises(ValueError, match="max-candidate-minutes"):
        check_candidate_count(1001, 1000)
    check_candidate_count(1000, 1000)  # exactly at the cap is fine


def _economics(*, n: int = 5, inference: ChallengerInference | None) -> HorizonEconomics:
    return HorizonEconomics(
        horizon_minutes=60,
        side="long",
        n=n,
        mean_gross_pct=3.0,
        mean_net_pct=2.5,
        win_rate_pct=60.0,
        baseline_mean_gross_pct=1.0,
        inference=inference,
    )


def _readiness(status: str) -> InferenceReadiness:
    return InferenceReadiness(
        status=status,
        eligible_episodes=5,
        formal_sample_episodes=5,
        formal_sample_clusters=1,
        baseline_resolved=5,
        completely_paired_episodes=5,
    )


def _report(economics: tuple[HorizonEconomics, ...]) -> BidirectionalBurstReport:
    return BidirectionalBurstReport(
        generated_at=datetime(2026, 8, 18, 0, 0, tzinfo=UTC),
        exchange="bybit",
        window=_window(),
        candidate_minutes=100,
        directions=(
            DirectionSummary(direction="buy", episodes=5, clusters=1, weeks=1, resolved_outcomes=5),
            DirectionSummary(
                direction="sell", episodes=0, clusters=0, weeks=0, resolved_outcomes=0
            ),
        ),
        economics=economics,
    )


def test_render_markdown_shows_n_a_n_lt_2_only_when_inference_never_ran() -> None:
    # Regression: a code-review finding caught render_markdown defaulting
    # every row to "n/a (n<2)" regardless of why no verdict was reached --
    # this must only appear when build_horizon_economics genuinely skipped
    # inference for having fewer than 2 challenger episodes (inference is
    # None), not whenever the readiness status happens not to be ready.
    report = _report((_economics(inference=None),))
    markdown = render_markdown(report)
    assert "n/a (n<2)" in markdown
    # And the verdict column for that same row must read the plain "n/a",
    # not also carry the "(n<2)" qualifier.
    lines = [line for line in markdown.splitlines() if "| long |" in line]
    assert len(lines) == 1
    cells = [cell.strip() for cell in lines[0].split("|")]
    assert cells[-3] == "n/a (n<2)"  # Readiness column
    assert cells[-2] == "n/a"  # Verdict column


def test_render_markdown_shows_the_real_readiness_reason_when_inference_ran_but_not_ready() -> None:
    # Regression: inference CAN run (n>=2) and still not be ready for a
    # reason other than sample size -- e.g. every episode came from a
    # single symbol cluster ("insufficient_diversity"). That real reason
    # must be shown, not the generic "n/a (n<2)" sample-size message.
    inference = ChallengerInference(
        inference_version="v1",
        bootstrap_version="v1",
        holm_version="v1",
        seed_derivation="v1",
        settings=DEFAULT_INFERENCE_SETTINGS,
        readiness=_readiness("insufficient_diversity"),
        formal_sample_event_ids=(1, 2, 3, 4, 5),
        cluster_concentration=(),
        baseline=None,
        challengers=(),
    )
    report = _report((_economics(inference=inference),))
    markdown = render_markdown(report)
    assert "insufficient_diversity" in markdown
    assert "n/a (n<2)" not in markdown


def test_render_markdown_shows_the_verdict_when_a_formal_result_exists() -> None:
    estimate = BootstrapEstimate(
        episodes=5, clusters=2, point_estimate=1.0, lower_bound=0.1, upper_bound=2.0
    )
    strategy = StrategyInference(
        strategy_key="burst_entry",
        estimate=estimate,
        verdict="evidence_of_edge",
        minimum_leave_one_cluster_out_pct=100.0,
        leave_one_cluster_out=(),
    )
    paired = PairedInference(
        variant_key="burst_entry",
        estimate=estimate,
        holm_rank=1,
        raw_p_value=0.01,
        holm_adjusted_p_value=0.01,
        holm_critical_alpha=0.05,
        holm_rejected=True,
        familywise_confidence_level=0.95,
        familywise_lower_bound=0.1,
        familywise_upper_bound=2.0,
    )
    inference = ChallengerInference(
        inference_version="v1",
        bootstrap_version="v1",
        holm_version="v1",
        seed_derivation="v1",
        settings=DEFAULT_INFERENCE_SETTINGS,
        readiness=_readiness("formal_sample_ready"),
        formal_sample_event_ids=tuple(range(1, 101)),
        cluster_concentration=(),
        baseline=None,
        challengers=(
            ChallengerFormalResult(
                variant_key="burst_entry",
                strategy=strategy,
                paired=paired,
                verdict="shadow_candidate",
            ),
        ),
    )
    report = _report((_economics(inference=inference),))
    markdown = render_markdown(report)
    assert "formal_sample_ready" in markdown
    assert "shadow_candidate" in markdown


def test_render_json_round_trips_candidate_minutes_and_window() -> None:
    report = _report((_economics(inference=None),))
    rendered = render_json(report)
    assert '"candidate_minutes": 100' in rendered
    assert '"exchange": "bybit"' in rendered
