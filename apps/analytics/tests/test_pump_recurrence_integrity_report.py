from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.pump_recurrence_integrity_report import (
    Episode,
    PumpRecurrenceIntegrityFilters,
    SourceIdentityObservation,
    build_report,
    classify_interval,
    compute_base_fragmentation,
    detect_identity_collisions,
    identity_reason,
    interval_histogram,
    merge_episodes_into_regimes,
)

_T0 = datetime(2026, 8, 1, tzinfo=UTC)


def _episode(
    event_id: int,
    base: str = "JIMOTHY",
    *,
    minutes_after_t0: float,
    duration_minutes: float = 1.0,
    peak_pct: float = 30.0,
    episode: int = 1,
) -> Episode:
    first = _T0 + timedelta(minutes=minutes_after_t0)
    return Episode(
        event_id=event_id,
        base=base,
        episode=episode,
        first_seen_at=first,
        last_seen_at=first + timedelta(minutes=duration_minutes),
        peak_pct=peak_pct,
        closed_at=first + timedelta(minutes=duration_minutes),
    )


def test_merge_episodes_into_regimes_collapses_rapid_reopens() -> None:
    # Three episodes each 2 minutes apart -- well inside a 24h cooldown --
    # must merge into a single regime, the exact detector-flapping scenario
    # this report exists to catch.
    episodes = (
        _episode(1, minutes_after_t0=0, peak_pct=25.0),
        _episode(2, minutes_after_t0=3, peak_pct=40.0),
        _episode(3, minutes_after_t0=6, peak_pct=35.0),
    )

    regimes = merge_episodes_into_regimes(episodes, timedelta(hours=24))

    assert len(regimes) == 1
    assert regimes[0].episode_ids == (1, 2, 3)
    assert regimes[0].max_peak_pct == 40.0


def test_merge_episodes_into_regimes_keeps_genuinely_separate_recurrences() -> None:
    episodes = (
        _episode(1, minutes_after_t0=0),
        _episode(2, minutes_after_t0=60 * 24 * 10),  # 10 days later
    )

    regimes = merge_episodes_into_regimes(episodes, timedelta(hours=24))

    assert len(regimes) == 2
    assert regimes[0].episode_ids == (1,)
    assert regimes[1].episode_ids == (2,)


def test_merge_episodes_into_regimes_rejects_unsorted_input() -> None:
    episodes = (
        _episode(1, minutes_after_t0=10),
        _episode(2, minutes_after_t0=0),
    )

    with pytest.raises(ValueError, match="sorted"):
        merge_episodes_into_regimes(episodes, timedelta(hours=24))


def test_merge_episodes_into_regimes_rejects_mixed_bases() -> None:
    episodes = (
        _episode(1, base="JIMOTHY", minutes_after_t0=0),
        _episode(2, base="GME1", minutes_after_t0=5),
    )

    with pytest.raises(ValueError, match="single base"):
        merge_episodes_into_regimes(episodes, timedelta(hours=24))


def test_merge_episodes_into_regimes_empty_input() -> None:
    assert merge_episodes_into_regimes((), timedelta(hours=24)) == ()


@pytest.mark.parametrize(
    ("gap", "expected"),
    [
        (timedelta(minutes=-1), "overlapping"),
        (timedelta(minutes=2), "under_5m"),
        (timedelta(minutes=30), "under_1h"),
        (timedelta(hours=12), "under_24h"),
        (timedelta(days=3), "d1_to_7d"),
        (timedelta(days=10), "over_7d"),
    ],
)
def test_classify_interval(gap: timedelta, expected: str) -> None:
    assert classify_interval(gap) == expected


def test_interval_histogram_counts_consecutive_gaps() -> None:
    episodes = (
        _episode(1, minutes_after_t0=0),
        _episode(2, minutes_after_t0=3),  # gap ~2min after episode 1 ends -> under_5m
        _episode(3, minutes_after_t0=60 * 24 * 10),  # gap far over 7d
    )

    histogram = interval_histogram(episodes)

    assert histogram == {"under_5m": 1, "over_7d": 1}


def test_compute_base_fragmentation_ratio_reflects_detector_flapping() -> None:
    # 5 raw episodes, all within minutes of each other -> 1 independent regime
    # at both cooldowns -> fragmentation_ratio == raw_episode_count.
    episodes = tuple(
        _episode(index, minutes_after_t0=index * 2, peak_pct=20.0 + index) for index in range(1, 6)
    )

    fragmentation = compute_base_fragmentation(episodes)

    assert fragmentation.raw_episode_count == 5
    assert fragmentation.regime_counts["24h"] == 1
    assert fragmentation.fragmentation_ratios["24h"] == 5.0
    assert fragmentation.max_peak_pct == 25.0


def test_compute_base_fragmentation_requires_at_least_one_episode() -> None:
    with pytest.raises(ValueError, match="at least one episode"):
        compute_base_fragmentation(())


def _observation(
    event_id: int,
    base: str,
    exchange: str,
    *,
    identity_key: str | None,
    unified_symbol: str | None = "X/USDT:USDT",
    base_asset: str | None = None,
    identity_conflict: bool = False,
) -> SourceIdentityObservation:
    return SourceIdentityObservation(
        event_id=event_id,
        base=base,
        exchange=exchange,
        identity_key=identity_key,
        unified_symbol=unified_symbol,
        base_asset=base_asset if base_asset is not None else base,
        identity_conflict=identity_conflict,
    )


def test_identity_reason_flags_conflict_missing_and_mismatch() -> None:
    conflict = _observation(1, "BTR", "gate", identity_key="k", identity_conflict=True)
    assert identity_reason(conflict) == "identity_conflict"

    missing_key = _observation(1, "BTR", "gate", identity_key=None)
    assert identity_reason(missing_key) == "missing_identity"

    missing_symbol = _observation(1, "BTR", "gate", identity_key="k", unified_symbol=None)
    assert identity_reason(missing_symbol) == "missing_identity"

    mismatched = _observation(1, "BTR", "gate", identity_key="k", base_asset="OTHER")
    assert identity_reason(mismatched) == "base_mismatch"

    assert identity_reason(_observation(1, "BTR", "gate", identity_key="k")) is None


def test_detect_identity_collisions_finds_shared_key_across_bases() -> None:
    # The exact 牛来/NIULAI scenario: two different `base` strings resolving
    # to the same underlying instrument on the same exchange.
    observations = (
        _observation(1, "牛来", "gate", identity_key="gate:NIULAI/USDT:USDT"),
        _observation(2, "NIULAI", "gate", identity_key="gate:NIULAI/USDT:USDT"),
    )

    collisions = detect_identity_collisions(observations)

    assert len(collisions) == 1
    assert collisions[0].kind == "shared_identity_key_across_bases"
    assert collisions[0].bases == ("NIULAI", "牛来")


def test_detect_identity_collisions_finds_relist_on_one_exchange() -> None:
    # Same base, same exchange, two different identity_keys across episodes
    # -- a relist/redenomination/contract change on that one venue.
    observations = (
        _observation(1, "CATE", "mexc", identity_key="mexc:swap:CATEUSDT:1700000000000"),
        _observation(2, "CATE", "mexc", identity_key="mexc:swap:CATEUSDT:1720000000000"),
    )

    collisions = detect_identity_collisions(observations)

    assert len(collisions) == 1
    assert collisions[0].kind == "base_maps_to_multiple_instruments_on_one_exchange"
    assert collisions[0].bases == ("CATE",)
    assert collisions[0].exchanges == ("mexc",)
    assert set(collisions[0].identity_keys) == {
        "mexc:swap:CATEUSDT:1700000000000",
        "mexc:swap:CATEUSDT:1720000000000",
    }


def test_detect_identity_collisions_ignores_unresolved_observations() -> None:
    observations = (
        _observation(1, "牛来", "gate", identity_key=None),
        _observation(2, "NIULAI", "gate", identity_key=None),
    )

    assert detect_identity_collisions(observations) == ()


def test_detect_identity_collisions_normal_cross_exchange_listing_is_not_a_collision() -> None:
    # The same instrument legitimately listed on several exchanges gets a
    # distinct identity_key per exchange by construction (the key embeds the
    # venue) -- this must never be flagged. Confirmed against real
    # production data before this report shipped: grouping by base alone
    # across exchanges produced hundreds of false positives exactly like
    # this before the fix.
    observations = (
        _observation(1, "ADA", "binance", identity_key="binance:swap:ADAUSDT:1580457600000"),
        _observation(2, "ADA", "bybit", identity_key="bybit:swap:ADAUSDT:1610668800000"),
        _observation(3, "ADA", "gate", identity_key="gate:swap:ADA_USDT:1600000000000"),
    )

    assert detect_identity_collisions(observations) == ()


def test_detect_identity_collisions_no_false_positive_on_clean_population() -> None:
    observations = (
        _observation(1, "BTR", "binance", identity_key="binance:BTR/USDT:USDT:swap"),
        _observation(2, "GME1", "lbank", identity_key="lbank:GME1/USDT:spot"),
    )

    assert detect_identity_collisions(observations) == ()


def test_build_report_population_stats_cover_every_base_not_just_case_studies() -> None:
    # One case-study base (JIMOTHY) heavily fragmented, one non-case-study
    # base (RANDOMCOIN) with a single clean episode -- population statistics
    # must reflect both, not just the named tickers.
    episodes = (
        *(_episode(index, base="JIMOTHY", minutes_after_t0=index * 2) for index in range(1, 11)),
        _episode(100, base="RANDOMCOIN", minutes_after_t0=0),
    )
    report = build_report(
        PumpRecurrenceIntegrityFilters(),
        episodes,
        (),
        code_revision="abc123",
        working_tree_dirty=False,
        generated_at=_T0,
    )

    assert report.population.total_bases == 2
    assert report.population.total_raw_episodes == 11
    bases_in_case_studies = {row.base for row in report.case_studies}
    assert bases_in_case_studies == {"JIMOTHY"}
    bases_in_full_population = {row.base for row in report.fragmentation_by_base}
    assert bases_in_full_population == {"JIMOTHY", "RANDOMCOIN"}
    # JIMOTHY: 10 raw episodes -> 1 regime -> ratio 10.0; RANDOMCOIN: 1/1 -> 1.0
    assert report.population.max_fragmentation_ratio_24h == 10.0
    assert report.population.median_fragmentation_ratio_24h == 5.5
