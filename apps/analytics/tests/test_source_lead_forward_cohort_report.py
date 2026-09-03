"""Synthetic-fixture tests for source_lead_forward_cohort_report.py's own
DB-fetch/CLI/rendering plumbing (research/source-lead-forward-cohort-
plumbing-v1) -- no real qualified capture exists yet (cohort start is
today, 2026-09-03; earliest possible checkpoint ~2026-10-01), so
`aggregate_cohort` (the pure aggregation this module's own docstring
extracts specifically to make this possible) is exercised directly against
synthetic `RawQualifiedEpisode`/`EpisodeResult` fixtures, mirroring
`test_source_lead_forward_cohort.py`'s own synthetic-input discipline for
`resolve_episode`/`formal_verdict`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.ohlcv import Candle
from schurfer_analytics.source_lead_forward_cohort import (
    EVIDENCE_FLOOR,
    MAX_SINGLE_ASSET_EPISODE_SHARE,
    MAX_SINGLE_WEEK_EPISODE_SHARE,
    SOURCE_LEAD_FORWARD_COHORT_START,
    VERDICT_CANDIDATE,
    VERDICT_FAIL,
    VERDICT_INSUFFICIENT_DATA,
    EpisodeResult,
    expected_exit_boundary_ms,
)
from schurfer_analytics.source_lead_forward_cohort_report import (
    _resolve_one,
    aggregate_cohort,
    build_parser,
    check_qualified_episode_count,
    generate_report,
)
from schurfer_analytics.source_lead_forward_cohort_repository import RawQualifiedEpisode

_BASE_AT = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)


def _episode(
    *, capture_id: int, base: str, observed_at: datetime, ask_vwap: float = 1.0
) -> RawQualifiedEpisode:
    return RawQualifiedEpisode(
        capture_id=capture_id,
        base=base,
        target_exchange="binance",
        observed_at=observed_at,
        requested_notional_usd=50.0,
        liquidity={"ask_vwap": ask_vwap},
        instrument={"unified_symbol": f"{base}/USDT:USDT"},
    )


def _resolved(base: str, net_return_pct: float) -> EpisodeResult:
    return EpisodeResult(base, True, None, net_return_pct)


def _unresolved(base: str, reason: str) -> EpisodeResult:
    return EpisodeResult(base, False, reason, None)


def _floor_meeting_fixture() -> tuple[list[RawQualifiedEpisode], list[EpisodeResult]]:
    """Exactly at EVIDENCE_FLOOR: 100 resolved episodes, 7 distinct asset
    clusters (spread so no cluster or week exceeds its concentration cap),
    4 distinct UTC weeks, one week apart so isocalendar() weeks differ."""
    assets = [f"ASSET{i}" for i in range(7)]
    episodes: list[RawQualifiedEpisode] = []
    results: list[EpisodeResult] = []
    for i in range(EVIDENCE_FLOOR["min_resolved_episodes"]):
        base = assets[i % len(assets)]
        week_offset = i % EVIDENCE_FLOOR["min_distinct_utc_weeks"]
        observed_at = _BASE_AT + timedelta(weeks=week_offset, hours=(i % 24))
        episodes.append(_episode(capture_id=i, base=base, observed_at=observed_at))
        results.append(_resolved(base, 0.5))
    return episodes, results


def test_aggregate_cohort_reaches_candidate_on_a_positive_floor_meeting_fixture() -> None:
    episodes, results = _floor_meeting_fixture()
    aggregate = aggregate_cohort(
        raw_episodes_count=len(episodes), matured=episodes, results=results
    )
    assert aggregate.funnel.qualified_episodes_total == len(episodes)
    assert aggregate.funnel.qualified_episodes_matured == len(episodes)
    assert aggregate.result.resolved_episodes == EVIDENCE_FLOOR["min_resolved_episodes"]
    assert aggregate.result.distinct_asset_clusters == 7
    assert aggregate.result.distinct_utc_weeks == 4
    assert aggregate.result.max_single_asset_share <= MAX_SINGLE_ASSET_EPISODE_SHARE
    assert aggregate.result.max_single_week_share <= MAX_SINGLE_WEEK_EPISODE_SHARE
    assert aggregate.result.mean_net_return_pct == pytest.approx(0.5)
    # Every observation is identical (0.5), so the bootstrap CI collapses
    # to a point at 0.5 -- strictly positive, so the verdict must be
    # candidate, not a coincidence of this fixture's own construction.
    assert aggregate.result.ci_lower_bound_pct == pytest.approx(0.5)
    assert aggregate.result.verdict == VERDICT_CANDIDATE


def test_aggregate_cohort_reaches_fail_on_a_negative_floor_meeting_fixture() -> None:
    episodes, results = _floor_meeting_fixture()
    results = [EpisodeResult(result.base, True, None, -0.5) for result in results]
    aggregate = aggregate_cohort(
        raw_episodes_count=len(episodes), matured=episodes, results=results
    )
    assert aggregate.result.ci_lower_bound_pct == pytest.approx(-0.5)
    assert aggregate.result.verdict == VERDICT_FAIL


def test_aggregate_cohort_reports_insufficient_data_below_episode_floor() -> None:
    episodes, results = _floor_meeting_fixture()
    episodes = episodes[:10]
    results = results[:10]
    aggregate = aggregate_cohort(
        raw_episodes_count=len(episodes), matured=episodes, results=results
    )
    assert aggregate.result.resolved_episodes == 10
    assert aggregate.result.verdict == VERDICT_INSUFFICIENT_DATA


def test_aggregate_cohort_reports_insufficient_data_below_cluster_floor() -> None:
    # 100 resolved episodes, but concentrated on a single asset -- fails
    # the 7-distinct-asset-cluster floor even though the episode count and
    # week spread both pass.
    episodes: list[RawQualifiedEpisode] = []
    results: list[EpisodeResult] = []
    for i in range(EVIDENCE_FLOOR["min_resolved_episodes"]):
        week_offset = i % EVIDENCE_FLOOR["min_distinct_utc_weeks"]
        observed_at = _BASE_AT + timedelta(weeks=week_offset, hours=(i % 24))
        episodes.append(_episode(capture_id=i, base="ONLYASSET", observed_at=observed_at))
        results.append(_resolved("ONLYASSET", 0.5))
    aggregate = aggregate_cohort(
        raw_episodes_count=len(episodes), matured=episodes, results=results
    )
    assert aggregate.result.distinct_asset_clusters == 1
    assert aggregate.result.verdict == VERDICT_INSUFFICIENT_DATA


def test_aggregate_cohort_tracks_unresolved_reasons_separately_from_resolved() -> None:
    episodes = [
        _episode(capture_id=1, base="A", observed_at=_BASE_AT),
        _episode(capture_id=2, base="B", observed_at=_BASE_AT),
        _episode(capture_id=3, base="C", observed_at=_BASE_AT),
    ]
    results = [
        _resolved("A", 1.0),
        _unresolved("B", "missing_exit_bar"),
        _unresolved("C", "missing_exit_bar"),
    ]
    aggregate = aggregate_cohort(raw_episodes_count=5, matured=episodes, results=results)
    assert aggregate.funnel.qualified_episodes_total == 5
    assert aggregate.funnel.qualified_episodes_matured == 3
    assert aggregate.funnel.resolved_episodes == 1
    assert aggregate.funnel.unresolved_by_reason == {"missing_exit_bar": 2}


def test_aggregate_cohort_handles_zero_resolved_episodes() -> None:
    episodes = [_episode(capture_id=1, base="A", observed_at=_BASE_AT)]
    results = [_unresolved("A", "missing_exit_bar")]
    aggregate = aggregate_cohort(raw_episodes_count=1, matured=episodes, results=results)
    assert aggregate.result.resolved_episodes == 0
    assert aggregate.result.mean_net_return_pct is None
    assert aggregate.result.ci_lower_bound_pct is None
    assert aggregate.result.verdict == VERDICT_INSUFFICIENT_DATA


def test_aggregate_cohort_is_deterministic_and_fingerprint_reflects_content() -> None:
    episodes, results = _floor_meeting_fixture()
    first = aggregate_cohort(raw_episodes_count=len(episodes), matured=episodes, results=results)
    second = aggregate_cohort(raw_episodes_count=len(episodes), matured=episodes, results=results)
    assert first.input_fingerprint == second.input_fingerprint

    mutated_results = [*results[:-1], _resolved(results[-1].base, 999.0)]
    third = aggregate_cohort(
        raw_episodes_count=len(episodes), matured=episodes, results=mutated_results
    )
    assert third.input_fingerprint != first.input_fingerprint


def test_aggregate_cohort_rejects_mismatched_lengths() -> None:
    episodes = [_episode(capture_id=1, base="A", observed_at=_BASE_AT)]
    with pytest.raises(ValueError, match="same length"):
        aggregate_cohort(raw_episodes_count=1, matured=episodes, results=[])


def test_check_qualified_episode_count_rejects_over_limit() -> None:
    with pytest.raises(ValueError, match="over --max-qualified-episodes"):
        check_qualified_episode_count(11, 10)
    check_qualified_episode_count(10, 10)  # exactly at the cap does not raise


def test_build_parser_defaults_since_to_the_frozen_cohort_start() -> None:
    args = build_parser().parse_args(["--no-working-tree-dirty"])
    assert args.since == SOURCE_LEAD_FORWARD_COHORT_START
    assert args.working_tree_dirty is False


def test_build_parser_accepts_working_tree_dirty_flag() -> None:
    args = build_parser().parse_args(["--working-tree-dirty"])
    assert args.working_tree_dirty is True


def test_build_parser_requires_working_tree_dirty_to_be_stated() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


async def test_generate_report_rejects_a_since_other_than_the_frozen_start() -> None:
    args = build_parser().parse_args(
        [
            "--since",
            "2026-01-01T00:00:00+00:00",
            "--code-revision",
            "deadbeef",
            "--no-working-tree-dirty",
        ]
    )
    with pytest.raises(ValueError, match="must equal the frozen"):
        await generate_report(args)


def test_resolve_one_resolves_a_genuine_episode_with_valid_liquidity() -> None:
    episode = _episode(capture_id=1, base="ABC", observed_at=_BASE_AT, ask_vwap=2.0)
    boundary_ms = expected_exit_boundary_ms(_BASE_AT)
    exit_bar = Candle(ts_ms=boundary_ms, open=2.1, high=2.1, low=2.1, close=2.1, volume=None)
    result = _resolve_one(episode, exit_bar)
    assert result.resolved is True
    assert result.net_return_pct is not None


def test_resolve_one_flags_missing_ask_vwap_as_invalid_market_data() -> None:
    episode = _episode(capture_id=1, base="ABC", observed_at=_BASE_AT)
    episode = RawQualifiedEpisode(
        capture_id=episode.capture_id,
        base=episode.base,
        target_exchange=episode.target_exchange,
        observed_at=episode.observed_at,
        requested_notional_usd=episode.requested_notional_usd,
        liquidity={},  # no ask_vwap key at all
        instrument=episode.instrument,
    )
    result = _resolve_one(episode, None)
    assert result.resolved is False
    assert result.unresolved_reason == "invalid_market_data"


def test_resolve_one_flags_non_positive_ask_vwap_as_invalid_market_data() -> None:
    episode = _episode(capture_id=1, base="ABC", observed_at=_BASE_AT, ask_vwap=0.0)
    result = _resolve_one(episode, None)
    assert result.resolved is False
    assert result.unresolved_reason == "invalid_market_data"
