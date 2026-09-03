"""Synthetic-fixture tests for source_lead_forward_cohort_report.py's own
DB-fetch/CLI/rendering plumbing (research/source-lead-forward-cohort-
plumbing-v1) -- no real qualified capture exists yet (cohort start is
today, 2026-09-03; earliest possible checkpoint ~2026-10-01), so
`aggregate_cohort` (the pure aggregation this module's own docstring
extracts specifically to make this possible) is exercised directly against
synthetic `RawQualifiedEpisode`/`Candle` fixtures, mirroring
`test_source_lead_forward_cohort.py`'s own synthetic-input discipline for
`resolve_episode`/`formal_verdict`.

Colleague review, 2026-09-03: `aggregate_cohort` now resolves episodes
itself (from raw exit-bar `Candle`s, at all of `EXIT_SLIPPAGE_SENSITIVITY_
BPS`) instead of taking pre-resolved `EpisodeResult`s, clusters by
`canonical_asset_id` instead of `base`, and only aggregates over the
STOPPING_RULE checkpoint prefix (`find_earliest_checkpoint_prefix_length`)
rather than every matured episode -- fixtures below construct real
`Candle`s so the tests exercise the actual resolution path, not a bypassed
one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.ohlcv import Candle
from schurfer_analytics.source_lead_forward_cohort import (
    EVIDENCE_FLOOR,
    EXIT_SLIPPAGE_BPS_ASSUMED,
    MAX_SINGLE_ASSET_EPISODE_SHARE,
    MAX_SINGLE_WEEK_EPISODE_SHARE,
    SOURCE_LEAD_FORWARD_COHORT_START,
    VERDICT_CANDIDATE,
    VERDICT_FAIL,
    VERDICT_INSUFFICIENT_DATA,
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
_ENTRY_PRICE = 1.0
_FAVORABLE_EXIT_PRICE = 1.1  # after costs/15bps slippage, nets positive
_UNFAVORABLE_EXIT_PRICE = 0.9  # nets negative


def _episode(
    *,
    capture_id: int,
    base: str,
    observed_at: datetime,
    canonical_asset_id: str | None = None,
    ask_vwap: float = _ENTRY_PRICE,
) -> RawQualifiedEpisode:
    return RawQualifiedEpisode(
        capture_id=capture_id,
        base=base,
        canonical_asset_id=canonical_asset_id or f"canonical:{base}",
        target_exchange="binance",
        observed_at=observed_at,
        requested_notional_usd=50.0,
        liquidity={"ask_vwap": ask_vwap},
        instrument={"unified_symbol": f"{base}/USDT:USDT"},
    )


def _exit_bar(observed_at: datetime, close: float) -> Candle:
    boundary_ms = expected_exit_boundary_ms(observed_at)
    return Candle(ts_ms=boundary_ms, open=close, high=close, low=close, close=close, volume=None)


def _floor_meeting_fixture(
    *, exit_price: float = _FAVORABLE_EXIT_PRICE
) -> tuple[list[RawQualifiedEpisode], list[Candle | None]]:
    """Exactly at EVIDENCE_FLOOR: 100 resolved episodes, 7 distinct
    canonical asset clusters (spread so no cluster or week exceeds its
    concentration cap), 4 distinct UTC weeks. Every episode shares the same
    entry/exit price, so calculate_performance produces an IDENTICAL
    net_return_pct across all of them -- the bootstrap CI collapses to a
    point, making the sign of the verdict unambiguous regardless of the
    exact fee/slippage arithmetic."""
    assets = [f"ASSET{i}" for i in range(7)]
    episodes: list[RawQualifiedEpisode] = []
    exit_bars: list[Candle | None] = []
    for i in range(EVIDENCE_FLOOR["min_resolved_episodes"]):
        base = assets[i % len(assets)]
        week_offset = i % EVIDENCE_FLOOR["min_distinct_utc_weeks"]
        observed_at = _BASE_AT + timedelta(weeks=week_offset, hours=(i % 24))
        episodes.append(_episode(capture_id=i, base=base, observed_at=observed_at))
        exit_bars.append(_exit_bar(observed_at, exit_price))
    return episodes, exit_bars


def test_aggregate_cohort_reaches_candidate_on_a_positive_floor_meeting_fixture() -> None:
    episodes, exit_bars = _floor_meeting_fixture(exit_price=_FAVORABLE_EXIT_PRICE)
    aggregate = aggregate_cohort(
        raw_episodes_count=len(episodes), matured=episodes, exit_bars=exit_bars
    )
    assert aggregate.funnel.qualified_episodes_total == len(episodes)
    assert aggregate.funnel.qualified_episodes_matured == len(episodes)
    assert aggregate.funnel.checkpoint_prefix_length == EVIDENCE_FLOOR["min_resolved_episodes"]
    assert aggregate.result.resolved_episodes == EVIDENCE_FLOOR["min_resolved_episodes"]
    assert aggregate.result.distinct_asset_clusters == 7
    assert aggregate.result.distinct_utc_weeks == 4
    assert aggregate.result.max_single_asset_share <= MAX_SINGLE_ASSET_EPISODE_SHARE
    assert aggregate.result.max_single_week_share <= MAX_SINGLE_WEEK_EPISODE_SHARE
    assert aggregate.result.mean_net_return_pct is not None
    assert aggregate.result.mean_net_return_pct > 0
    # Every observation is identical, so the bootstrap CI collapses to a
    # point at the mean -- strictly positive, so the verdict must be
    # candidate, not a coincidence of this fixture's own construction.
    assert aggregate.result.ci_lower_bound_pct == pytest.approx(
        aggregate.result.mean_net_return_pct
    )
    assert aggregate.result.verdict == VERDICT_CANDIDATE
    # Asset/week breakdowns cover every cluster/week seen.
    assert len(aggregate.result.asset_breakdown) == 7
    assert len(aggregate.result.week_breakdown) == 4
    assert {row.canonical_asset_id for row in aggregate.result.asset_breakdown} == {
        f"canonical:ASSET{i}" for i in range(7)
    }


def test_aggregate_cohort_reaches_fail_on_a_negative_floor_meeting_fixture() -> None:
    episodes, exit_bars = _floor_meeting_fixture(exit_price=_UNFAVORABLE_EXIT_PRICE)
    aggregate = aggregate_cohort(
        raw_episodes_count=len(episodes), matured=episodes, exit_bars=exit_bars
    )
    assert aggregate.result.mean_net_return_pct is not None
    assert aggregate.result.mean_net_return_pct < 0
    assert aggregate.result.ci_lower_bound_pct == pytest.approx(
        aggregate.result.mean_net_return_pct
    )
    assert aggregate.result.verdict == VERDICT_FAIL


def test_aggregate_cohort_reports_insufficient_data_below_episode_floor() -> None:
    """Below the floor, no checkpoint is ever reached -- the RESULT (not
    the funnel) reports zero resolved episodes, since CohortResult
    represents the checkpoint's own outcome specifically, not a running
    tally of whatever has matured so far (that visibility lives in
    CohortFunnel.qualified_episodes_matured instead)."""
    episodes, exit_bars = _floor_meeting_fixture()
    episodes = episodes[:10]
    exit_bars = exit_bars[:10]
    aggregate = aggregate_cohort(
        raw_episodes_count=len(episodes), matured=episodes, exit_bars=exit_bars
    )
    assert aggregate.funnel.qualified_episodes_matured == 10
    assert aggregate.funnel.checkpoint_prefix_length is None
    assert aggregate.result.resolved_episodes == 0
    assert aggregate.result.verdict == VERDICT_INSUFFICIENT_DATA


def test_aggregate_cohort_reports_insufficient_data_below_cluster_floor() -> None:
    # 100 resolved episodes, but concentrated on a single canonical asset --
    # fails the 7-distinct-asset-cluster floor even though the episode
    # count and week spread both pass, and even though the checkpoint
    # prefix-length search itself only looks at episode/week floors (not
    # clusters), so a checkpoint IS reached here -- formal_verdict is what
    # then rejects it on the cluster floor.
    episodes: list[RawQualifiedEpisode] = []
    exit_bars: list[Candle | None] = []
    for i in range(EVIDENCE_FLOOR["min_resolved_episodes"]):
        week_offset = i % EVIDENCE_FLOOR["min_distinct_utc_weeks"]
        observed_at = _BASE_AT + timedelta(weeks=week_offset, hours=(i % 24))
        episodes.append(_episode(capture_id=i, base="ONLYASSET", observed_at=observed_at))
        exit_bars.append(_exit_bar(observed_at, _FAVORABLE_EXIT_PRICE))
    aggregate = aggregate_cohort(
        raw_episodes_count=len(episodes), matured=episodes, exit_bars=exit_bars
    )
    assert aggregate.funnel.checkpoint_prefix_length == EVIDENCE_FLOOR["min_resolved_episodes"]
    assert aggregate.result.distinct_asset_clusters == 1
    assert aggregate.result.verdict == VERDICT_INSUFFICIENT_DATA


def test_aggregate_cohort_clusters_by_canonical_asset_id_not_base() -> None:
    """The exact identity bug colleague review flagged: two episodes
    sharing a `base` ticker but with DIFFERENT canonical_asset_id must be
    counted as two distinct clusters, and one canonical asset appearing
    under two different `base` tickers must be counted as ONE cluster."""
    episodes = [
        _episode(
            capture_id=1,
            base="SAMEBASE",
            canonical_asset_id="canonical:real-asset-A",
            observed_at=_BASE_AT,
        ),
        _episode(
            capture_id=2,
            base="SAMEBASE",
            canonical_asset_id="canonical:real-asset-B",
            observed_at=_BASE_AT,
        ),
        _episode(
            capture_id=3,
            base="DIFFERENTBASE",
            canonical_asset_id="canonical:real-asset-A",
            observed_at=_BASE_AT,
        ),
    ]
    exit_bars = [_exit_bar(_BASE_AT, _FAVORABLE_EXIT_PRICE) for _ in episodes]
    aggregate = aggregate_cohort(raw_episodes_count=3, matured=episodes, exit_bars=exit_bars)
    # Below the floor, so no checkpoint -- inspect the FUNNEL's matured
    # count and re-derive clustering by calling the resolver directly for
    # this narrow, cluster-identity-only assertion instead.
    assert aggregate.funnel.qualified_episodes_matured == 3
    resolved = [
        _resolve_one(episode, bar, exit_slippage_bps=EXIT_SLIPPAGE_BPS_ASSUMED)
        for episode, bar in zip(episodes, exit_bars, strict=True)
    ]
    assert all(result.resolved for result in resolved)
    distinct_clusters = len({episode.canonical_asset_id for episode in episodes})
    assert (
        distinct_clusters == 2
    )  # real-asset-A and real-asset-B, not 1 (by base) or 3 (by capture)


def test_aggregate_cohort_tracks_unresolved_reasons_within_the_checkpoint() -> None:
    # Three unresolved episodes PREPENDED before a checkpoint-reaching
    # fixture: unresolved episodes count toward the checkpoint search's
    # scan but never toward its resolved-episode floor, so 3 extra episodes
    # are needed on top of the 100-resolved fixture for the floor to still
    # be crossed (at index 103, not 100).
    floor_episodes, floor_bars = _floor_meeting_fixture()
    unresolved_episodes = [
        _episode(capture_id=-i - 1, base="UNRESOLVED", observed_at=_BASE_AT - timedelta(days=1))
        for i in range(3)
    ]
    episodes = [*unresolved_episodes, *floor_episodes]
    exit_bars: list[Candle | None] = [None, None, None, *floor_bars]
    aggregate = aggregate_cohort(
        raw_episodes_count=len(episodes), matured=episodes, exit_bars=exit_bars
    )
    assert aggregate.funnel.checkpoint_prefix_length == len(episodes)
    assert aggregate.funnel.unresolved_by_reason.get("missing_exit_bar") == 3
    assert aggregate.result.resolved_episodes == EVIDENCE_FLOOR["min_resolved_episodes"]


def test_aggregate_cohort_handles_zero_resolved_episodes() -> None:
    episodes = [_episode(capture_id=1, base="A", observed_at=_BASE_AT)]
    exit_bars: list[Candle | None] = [None]
    aggregate = aggregate_cohort(raw_episodes_count=1, matured=episodes, exit_bars=exit_bars)
    assert aggregate.result.resolved_episodes == 0
    assert aggregate.result.mean_net_return_pct is None
    assert aggregate.result.ci_lower_bound_pct is None
    assert aggregate.result.verdict == VERDICT_INSUFFICIENT_DATA


def test_aggregate_cohort_checkpoint_prefix_is_stable_once_reached() -> None:
    """The whole point of the STOPPING_RULE fix: appending MORE matured
    episodes after the checkpoint is already reached must not change the
    checkpoint's own result."""
    episodes, exit_bars = _floor_meeting_fixture()
    extra_episodes = [
        _episode(
            capture_id=1000 + i,
            base="LATEASSET",
            observed_at=_BASE_AT + timedelta(weeks=50),
        )
        for i in range(20)
    ]
    extra_bars = [
        _exit_bar(episode.observed_at, _UNFAVORABLE_EXIT_PRICE) for episode in extra_episodes
    ]

    without_extra = aggregate_cohort(
        raw_episodes_count=len(episodes), matured=episodes, exit_bars=exit_bars
    )
    with_extra = aggregate_cohort(
        raw_episodes_count=len(episodes) + len(extra_episodes),
        matured=[*episodes, *extra_episodes],
        exit_bars=[*exit_bars, *extra_bars],
    )
    assert (
        without_extra.funnel.checkpoint_prefix_length == with_extra.funnel.checkpoint_prefix_length
    )
    assert without_extra.result.verdict == with_extra.result.verdict
    assert without_extra.result.mean_net_return_pct == with_extra.result.mean_net_return_pct
    assert without_extra.result.ci_lower_bound_pct == with_extra.result.ci_lower_bound_pct
    assert without_extra.checkpoint_rows == with_extra.checkpoint_rows


def test_aggregate_cohort_rejects_mismatched_lengths() -> None:
    episodes = [_episode(capture_id=1, base="A", observed_at=_BASE_AT)]
    with pytest.raises(ValueError, match="same length"):
        aggregate_cohort(raw_episodes_count=1, matured=episodes, exit_bars=[])


def test_aggregate_cohort_computes_the_slippage_sensitivity_family() -> None:
    episodes, exit_bars = _floor_meeting_fixture(exit_price=_FAVORABLE_EXIT_PRICE)
    aggregate = aggregate_cohort(
        raw_episodes_count=len(episodes), matured=episodes, exit_bars=exit_bars
    )
    bps_seen = {point.exit_slippage_bps for point in aggregate.result.slippage_sensitivity}
    assert bps_seen == {0.0, EXIT_SLIPPAGE_BPS_ASSUMED, 2 * EXIT_SLIPPAGE_BPS_ASSUMED}
    by_bps = {point.exit_slippage_bps: point for point in aggregate.result.slippage_sensitivity}
    # Higher assumed exit slippage must never produce a HIGHER net return
    # than lower assumed slippage, for the same underlying price path.
    at_zero = by_bps[0.0].mean_net_return_pct
    at_primary = by_bps[EXIT_SLIPPAGE_BPS_ASSUMED].mean_net_return_pct
    at_double = by_bps[2 * EXIT_SLIPPAGE_BPS_ASSUMED].mean_net_return_pct
    assert at_zero is not None
    assert at_primary is not None
    assert at_double is not None
    assert at_zero > at_primary > at_double
    # The primary point (at EXIT_SLIPPAGE_BPS_ASSUMED) matches the result's
    # own headline mean_net_return_pct exactly.
    primary = by_bps[EXIT_SLIPPAGE_BPS_ASSUMED]
    assert primary.is_primary is True
    assert primary.mean_net_return_pct == aggregate.result.mean_net_return_pct


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


def test_build_parser_has_bounded_concurrency_and_wall_seconds_defaults() -> None:
    args = build_parser().parse_args(["--no-working-tree-dirty"])
    assert args.max_concurrent_exchange_fetches > 0
    assert args.exchange_fetch_wall_seconds > 0


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
    exit_bar = _exit_bar(_BASE_AT, 2.1)
    result = _resolve_one(episode, exit_bar, exit_slippage_bps=EXIT_SLIPPAGE_BPS_ASSUMED)
    assert result.resolved is True
    assert result.net_return_pct is not None


def test_resolve_one_flags_missing_ask_vwap_as_invalid_market_data() -> None:
    episode = _episode(capture_id=1, base="ABC", observed_at=_BASE_AT)
    episode = RawQualifiedEpisode(
        capture_id=episode.capture_id,
        base=episode.base,
        canonical_asset_id=episode.canonical_asset_id,
        target_exchange=episode.target_exchange,
        observed_at=episode.observed_at,
        requested_notional_usd=episode.requested_notional_usd,
        liquidity={},  # no ask_vwap key at all
        instrument=episode.instrument,
    )
    result = _resolve_one(episode, None, exit_slippage_bps=EXIT_SLIPPAGE_BPS_ASSUMED)
    assert result.resolved is False
    assert result.unresolved_reason == "invalid_market_data"


def test_resolve_one_flags_non_positive_ask_vwap_as_invalid_market_data() -> None:
    episode = _episode(capture_id=1, base="ABC", observed_at=_BASE_AT, ask_vwap=0.0)
    result = _resolve_one(episode, None, exit_slippage_bps=EXIT_SLIPPAGE_BPS_ASSUMED)
    assert result.resolved is False
    assert result.unresolved_reason == "invalid_market_data"
