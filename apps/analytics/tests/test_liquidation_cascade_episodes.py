from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.liquidation_cascade_episodes import (
    MinuteState,
    decluster_cascade_episodes,
)

_START = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)


def _qualifying(
    minute_offset: int,
    *,
    price_drop_pct: float = -0.06,
    oi_drop_pct: float = -0.20,
    price_complete: bool = True,
    open_interest_complete: bool = True,
    symbol: str = "TESTUSDT",
    exchange: str = "bybit",
) -> MinuteState:
    return MinuteState(
        exchange=exchange,
        symbol=symbol,
        bucket_start=_START + timedelta(minutes=minute_offset),
        price_drop_pct=price_drop_pct,
        oi_drop_pct=oi_drop_pct,
        is_qualifying=True,
        price_complete=price_complete,
        open_interest_complete=open_interest_complete,
    )


def _non_qualifying(
    minute_offset: int,
    *,
    price_drop_pct: float | None = -0.01,
    oi_drop_pct: float | None = -0.01,
    symbol: str = "TESTUSDT",
    exchange: str = "bybit",
) -> MinuteState:
    return MinuteState(
        exchange=exchange,
        symbol=symbol,
        bucket_start=_START + timedelta(minutes=minute_offset),
        price_drop_pct=price_drop_pct,
        oi_drop_pct=oi_drop_pct,
        is_qualifying=False,
        price_complete=True,
        open_interest_complete=True,
    )


def test_a_20_minute_run_of_qualifying_minutes_creates_exactly_one_episode() -> None:
    minutes = [_qualifying(i) for i in range(20)]
    episodes = decluster_cascade_episodes(
        minutes, recovery_price_pct=0.02, recovery_oi_pct=0.05, cooldown_minutes=30
    )
    assert len(episodes) == 1
    (episode,) = episodes
    assert episode.trigger_minutes == 20
    assert episode.trigger_at == _START
    assert episode.last_trigger_at == _START + timedelta(minutes=19)


def test_a_gap_at_or_past_cooldown_creates_two_episodes() -> None:
    minutes = [
        _qualifying(0),
        _qualifying(1),
        # 30-minute gap with no data at all -- cooldown alone must close it.
        _qualifying(31),
        _qualifying(32),
    ]
    episodes = decluster_cascade_episodes(
        minutes, recovery_price_pct=0.02, recovery_oi_pct=0.05, cooldown_minutes=30
    )
    assert len(episodes) == 2
    assert episodes[0].trigger_minutes == 2
    assert episodes[1].trigger_minutes == 2
    assert episodes[1].trigger_at == _START + timedelta(minutes=31)


def test_recovery_before_cooldown_closes_the_episode_early() -> None:
    minutes = [
        _qualifying(0, price_drop_pct=-0.06, oi_drop_pct=-0.20),
        # Fully recovered at minute 1 -- both price and OI back above the
        # recovery thresholds -- well before the 30-minute cooldown would
        # have fired on its own.
        _non_qualifying(1, price_drop_pct=0.03, oi_drop_pct=0.06),
        # A genuinely new, unrelated cascade 5 minutes later, still inside
        # the cooldown window.
        _qualifying(6, price_drop_pct=-0.05, oi_drop_pct=-0.16),
    ]
    episodes = decluster_cascade_episodes(
        minutes, recovery_price_pct=0.02, recovery_oi_pct=0.05, cooldown_minutes=30
    )
    assert len(episodes) == 2
    assert episodes[0].trigger_minutes == 1
    assert episodes[1].trigger_at == _START + timedelta(minutes=6)


def test_partial_recovery_does_not_close_the_episode() -> None:
    minutes = [
        _qualifying(0, price_drop_pct=-0.06, oi_drop_pct=-0.20),
        # Price recovered above threshold but OI has not -- not a full
        # recovery, must not close the episode.
        _non_qualifying(1, price_drop_pct=0.03, oi_drop_pct=-0.10),
        _qualifying(2, price_drop_pct=-0.05, oi_drop_pct=-0.16),
    ]
    episodes = decluster_cascade_episodes(
        minutes, recovery_price_pct=0.02, recovery_oi_pct=0.05, cooldown_minutes=30
    )
    assert len(episodes) == 1
    assert episodes[0].trigger_minutes == 2


def test_missing_bars_between_qualifying_minutes_still_count_toward_cooldown() -> None:
    # No data at all for minutes 1..29 (a genuine gap, not a resolved
    # non-qualifying minute) -- cooldown time still elapses.
    minutes = [_qualifying(0), _qualifying(30)]
    episodes = decluster_cascade_episodes(
        minutes, recovery_price_pct=0.02, recovery_oi_pct=0.05, cooldown_minutes=30
    )
    assert len(episodes) == 2


def test_incomplete_bar_in_a_qualifying_run_marks_the_episode_data_quality_unresolved() -> None:
    minutes = [
        _qualifying(0),
        _qualifying(1, price_complete=False),
        _qualifying(2),
    ]
    episodes = decluster_cascade_episodes(
        minutes, recovery_price_pct=0.02, recovery_oi_pct=0.05, cooldown_minutes=30
    )
    assert len(episodes) == 1
    assert episodes[0].data_quality_unresolved is True


def test_start_id_offset_keeps_episode_ids_unique_across_calls() -> None:
    first = decluster_cascade_episodes(
        [_qualifying(0, symbol="AAAUSDT")],
        recovery_price_pct=0.02,
        recovery_oi_pct=0.05,
        cooldown_minutes=30,
        start_id=1,
    )
    second = decluster_cascade_episodes(
        [_qualifying(0, symbol="BBBUSDT")],
        recovery_price_pct=0.02,
        recovery_oi_pct=0.05,
        cooldown_minutes=30,
        start_id=len(first) + 1,
    )
    assert {e.episode_id for e in first}.isdisjoint({e.episode_id for e in second})


def test_different_instruments_never_merge_into_one_episode() -> None:
    minutes = [_qualifying(0, symbol="AAAUSDT"), _qualifying(0, symbol="BBBUSDT")]
    episodes = decluster_cascade_episodes(
        minutes, recovery_price_pct=0.02, recovery_oi_pct=0.05, cooldown_minutes=30
    )
    assert len(episodes) == 2
    assert {e.symbol for e in episodes} == {"AAAUSDT", "BBBUSDT"}


def test_a_qualifying_minute_must_carry_resolved_drops() -> None:
    with pytest.raises(ValueError, match="resolved price/OI drops"):
        MinuteState(
            exchange="bybit",
            symbol="TESTUSDT",
            bucket_start=_START,
            price_drop_pct=None,
            oi_drop_pct=None,
            is_qualifying=True,
            price_complete=True,
            open_interest_complete=True,
        )


def test_recovery_thresholds_must_be_positive() -> None:
    with pytest.raises(ValueError, match="recovery thresholds"):
        decluster_cascade_episodes(
            [_qualifying(0)], recovery_price_pct=0.0, recovery_oi_pct=0.05, cooldown_minutes=30
        )


def test_declustering_is_deterministic() -> None:
    minutes = [_qualifying(i) for i in range(5)] + [
        _qualifying(i, symbol="ZZZUSDT") for i in range(5)
    ]
    first = decluster_cascade_episodes(
        minutes, recovery_price_pct=0.02, recovery_oi_pct=0.05, cooldown_minutes=30
    )
    second = decluster_cascade_episodes(
        minutes, recovery_price_pct=0.02, recovery_oi_pct=0.05, cooldown_minutes=30
    )
    assert first == second
