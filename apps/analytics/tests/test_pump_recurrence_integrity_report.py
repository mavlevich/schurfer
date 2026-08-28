from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.pump_recurrence_integrity_report import (
    CASE_STUDY_BASES,
    Episode,
    PumpRecurrenceIntegrityFilters,
    SourceIdentityObservation,
    build_report,
    classify_interval,
    compute_base_fragmentation,
    compute_input_fingerprint,
    detect_identity_collisions,
    find_cross_venue_unresolved_pairs,
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


def test_merge_episodes_into_regimes_uses_running_extent_not_previous_episode() -> None:
    # Colleague review (2026-08-28): a long first episode, a short episode
    # nested entirely inside it, then a third episode close to the FIRST
    # episode's true end but more than `cooldown` after the nested (shorter)
    # second episode's own last_seen_at. Comparing against the immediately
    # preceding raw episode's last_seen_at alone would measure the gap from
    # episode 2's early end and incorrectly split episode 3 into its own
    # regime; comparing against the regime's running maximum (episode 1's
    # true, later end) correctly keeps all three merged.
    long_first = _episode(1, minutes_after_t0=0, duration_minutes=120)  # ends at t+120
    nested_second = _episode(2, minutes_after_t0=10, duration_minutes=5)  # ends at t+15
    # 60min cooldown; third episode starts at t+140: 20min after episode 1's
    # true end (t+120, within 60min cooldown), but 125min after episode 2's
    # own end (t+15, outside a 60min cooldown) -- exposes the bug directly.
    third = _episode(3, minutes_after_t0=140, duration_minutes=1)

    regimes = merge_episodes_into_regimes((long_first, nested_second, third), timedelta(minutes=60))

    assert len(regimes) == 1
    assert regimes[0].episode_ids == (1, 2, 3)


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


def test_detect_identity_collisions_finds_alias_via_base_mismatch() -> None:
    # Colleague review (2026-08-28): the realistic 牛来/NIULAI shape is that
    # the event's own `base` label disagrees with the exchange's reported
    # `base_asset` -- exactly `identity_reason() == "base_mismatch"`. The
    # first version of this report excluded base_mismatch observations from
    # collision detection entirely, making it structurally blind to this
    # exact scenario while still reporting a reassuring "0 collisions".
    observations = (
        _observation(1, "牛来", "gate", identity_key="gate:NIULAI/USDT:USDT", base_asset="NIULAI"),
        _observation(
            2, "NIULAI", "gate", identity_key="gate:NIULAI/USDT:USDT", base_asset="NIULAI"
        ),
    )

    # The 牛来 observation reports base_mismatch (its own `base` column
    # disagrees with the shared base_asset "NIULAI"); the NIULAI observation
    # resolves cleanly on its own. Despite one side being a base_mismatch,
    # the pair must still be caught as a collision, not silently dropped.
    assert identity_reason(observations[0]) == "base_mismatch"
    assert identity_reason(observations[1]) is None
    collisions = detect_identity_collisions(observations)
    kinds = {collision.kind for collision in collisions}
    assert "shared_identity_key_across_bases" in kinds
    assert "shared_base_asset_across_bases" in kinds


def test_detect_identity_collisions_finds_shared_base_asset_when_identity_key_drifts() -> None:
    # identity_key formatting differs (e.g. a market_id timestamp suffix that
    # has not stabilized) but the exchange-reported base_asset agrees -- the
    # shared_base_asset_across_bases check catches this even when the exact
    # identity_key strings do not match.
    observations = (
        _observation(
            1, "牛来", "gate", identity_key="gate:NIULAI/USDT:USDT:v1", base_asset="NIULAI"
        ),
        _observation(
            2, "NIULAI", "gate", identity_key="gate:NIULAI/USDT:USDT:v2", base_asset="NIULAI"
        ),
    )

    collisions = detect_identity_collisions(observations)

    shared_asset = [c for c in collisions if c.kind == "shared_base_asset_across_bases"]
    assert len(shared_asset) == 1
    assert shared_asset[0].bases == ("NIULAI", "牛来")
    assert shared_asset[0].exchanges == ("gate",)
    # identity_key itself did not match, so shared_identity_key_across_bases
    # must not also fire for this pair.
    assert all(c.kind != "shared_identity_key_across_bases" for c in collisions)


def test_find_cross_venue_unresolved_pairs_flags_non_overlapping_exchanges() -> None:
    # The actual 牛来/NIULAI situation confirmed against production: 牛来 only
    # ever observed on 'gate', NIULAI only ever observed on 'bingx'/'lbank'/
    # 'mexc' -- zero exchange overlap, so neither collision check could ever
    # compare them. This must be surfaced explicitly rather than read as
    # "0 collisions found" implying they were compared and found different.
    observations = (
        _observation(1, "牛来", "gate", identity_key="gate:NIULAI/USDT:USDT", base_asset="NIULAI"),
        _observation(
            2, "NIULAI", "bingx", identity_key="bingx:NIULAI/USDT:USDT", base_asset="NIULAI"
        ),
        _observation(
            3, "NIULAI", "lbank", identity_key="lbank:NIULAI/USDT:spot", base_asset="NIULAI"
        ),
    )

    pairs = find_cross_venue_unresolved_pairs(CASE_STUDY_BASES, observations)

    assert len(pairs) == 1
    assert pairs[0].bases == ("牛来", "NIULAI")
    assert pairs[0].first_exchanges == ("gate",)
    assert pairs[0].second_exchanges == ("bingx", "lbank")


def test_find_cross_venue_unresolved_pairs_empty_when_exchanges_overlap() -> None:
    observations = (
        _observation(1, "牛来", "gate", identity_key="gate:NIULAI/USDT:USDT", base_asset="NIULAI"),
        _observation(
            2, "NIULAI", "gate", identity_key="gate:NIULAI/USDT:USDT", base_asset="NIULAI"
        ),
    )

    assert find_cross_venue_unresolved_pairs(CASE_STUDY_BASES, observations) == ()


def test_find_cross_venue_unresolved_pairs_skips_bases_with_no_coverage_at_all() -> None:
    # A base with zero usable observations anywhere is a coverage gap, not a
    # cross-venue-unresolved pair -- the two failure modes must stay distinct.
    observations = (_observation(1, "牛来", "gate", identity_key="gate:NIULAI/USDT:USDT"),)

    assert find_cross_venue_unresolved_pairs(CASE_STUDY_BASES, observations) == ()


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


def test_build_report_counts_events_without_source_observations() -> None:
    # Colleague review (2026-08-28): `identity_observations_statement` inner-
    # joins from pump_event_sources, so an event with zero source rows is
    # invisible to that query outright -- not resolved, not unresolved, just
    # absent. A report with "0 unresolved, 0 collisions" must not read as
    # full coverage when events are silently missing from the denominator.
    episodes = (
        _episode(1, base="JIMOTHY", minutes_after_t0=0),
        _episode(2, base="GME1", minutes_after_t0=0),
    )
    # Only event 1 has an identity observation; event 2 has none.
    identity_observations = (_observation(1, "JIMOTHY", "gate", identity_key="k"),)

    report = build_report(
        PumpRecurrenceIntegrityFilters(),
        episodes,
        identity_observations,
        code_revision="abc123",
        working_tree_dirty=False,
        generated_at=_T0,
    )

    assert report.population.events_without_source_observations == 1
    assert report.population.identity_audit_incomplete is True


def test_build_report_identity_audit_complete_when_every_event_has_a_source_row() -> None:
    episodes = (_episode(1, base="JIMOTHY", minutes_after_t0=0),)
    identity_observations = (_observation(1, "JIMOTHY", "gate", identity_key="k"),)

    report = build_report(
        PumpRecurrenceIntegrityFilters(),
        episodes,
        identity_observations,
        code_revision="abc123",
        working_tree_dirty=False,
        generated_at=_T0,
    )

    assert report.population.events_without_source_observations == 0
    assert report.population.identity_audit_incomplete is False


def test_build_report_counts_episodes_open_at_cutoff() -> None:
    until = _T0 + timedelta(minutes=30)
    # Episode 1 closes well before the cutoff; episode 2's last_seen_at is
    # past the cutoff -- it was still open as of the nominal --until boundary.
    episodes = (
        _episode(1, base="JIMOTHY", minutes_after_t0=0, duration_minutes=1),
        _episode(2, base="GME1", minutes_after_t0=0, duration_minutes=60),
    )

    report = build_report(
        PumpRecurrenceIntegrityFilters(until=until),
        episodes,
        (),
        code_revision="abc123",
        working_tree_dirty=False,
        generated_at=_T0,
    )

    assert report.population.episodes_open_at_cutoff == 1


def test_build_report_episodes_open_at_cutoff_is_zero_when_until_unset() -> None:
    episodes = (_episode(1, base="JIMOTHY", minutes_after_t0=0, duration_minutes=60),)

    report = build_report(
        PumpRecurrenceIntegrityFilters(),
        episodes,
        (),
        code_revision="abc123",
        working_tree_dirty=False,
        generated_at=_T0,
    )

    assert report.population.episodes_open_at_cutoff == 0


def test_compute_input_fingerprint_is_stable_and_sensitive_to_changes() -> None:
    episodes = (_episode(1, base="JIMOTHY", minutes_after_t0=0),)
    observations = (_observation(1, "JIMOTHY", "gate", identity_key="k"),)

    first = compute_input_fingerprint(episodes, observations)
    second = compute_input_fingerprint(episodes, observations)
    assert first == second

    mutated_episodes = (_episode(1, base="JIMOTHY", minutes_after_t0=0, peak_pct=99.0),)
    assert compute_input_fingerprint(mutated_episodes, observations) != first


def test_build_report_fingerprint_is_populated() -> None:
    episodes = (_episode(1, base="JIMOTHY", minutes_after_t0=0),)
    report = build_report(
        PumpRecurrenceIntegrityFilters(),
        episodes,
        (),
        code_revision="abc123",
        working_tree_dirty=False,
        generated_at=_T0,
    )

    assert len(report.input_fingerprint) == 64  # hex-encoded SHA-256
