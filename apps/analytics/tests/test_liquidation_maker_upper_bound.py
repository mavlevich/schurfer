"""Frozen-contract and pure-evaluator tests for research/liquidation-
maker-upper-bound-v1.

decluster_cascade_episodes/resolve_episode/formal_verdict are pure (no
I/O, no DB, no network) -- exercised here with synthetic inputs, matching
this codebase's established discipline for a frozen contract: the
resolution mechanics must be testable before/independent of the real
Postgres data they will eventually run against.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from schurfer_analytics.liquidation_maker_upper_bound import (
    CASCADE_COOLDOWN_MINUTES,
    EVIDENCE_FLOOR,
    EXIT_SLIPPAGE_BPS_ASSUMED,
    MAX_EXIT_BAR_GAP_MINUTES,
    MAX_POSITION_HOLD_MINUTES,
    MAX_SINGLE_ASSET_EPISODE_SHARE,
    MAX_SINGLE_WEEK_EPISODE_SHARE,
    PRIMARY_CASCADE_NOTIONAL_USD,
    SENSITIVITY_CASCADE_NOTIONAL_USD_FAMILY,
    VERDICT_INSUFFICIENT_DATA,
    VERDICT_POSITIVE_WARRANTS_SHADOW_TEST,
    VERDICT_REJECT,
    CascadeEpisode,
    EpisodeInputs,
    LiquidationTriggerMinute,
    decluster_cascade_episodes,
    formal_verdict,
    resolve_episode,
)
from schurfer_analytics.ohlcv import Candle

_EXCHANGE = "bybit"
_MARKET = "BTCUSDT"
_T0 = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)


def _bar(ts: datetime, *, o: float, h: float, low: float, c: float) -> Candle:
    return Candle(ts_ms=int(ts.timestamp() * 1000), open=o, high=h, low=low, close=c, volume=None)


# --- frozen values ----------------------------------------------------------


def test_cascade_parameters_are_frozen_as_specified() -> None:
    assert PRIMARY_CASCADE_NOTIONAL_USD == 250_000.0
    assert SENSITIVITY_CASCADE_NOTIONAL_USD_FAMILY == (100_000.0, 250_000.0, 500_000.0)
    assert CASCADE_COOLDOWN_MINUTES == 60


def test_exit_and_hold_parameters_are_frozen() -> None:
    assert MAX_POSITION_HOLD_MINUTES == 60
    assert EXIT_SLIPPAGE_BPS_ASSUMED == 15.0
    assert MAX_EXIT_BAR_GAP_MINUTES == 2.0


def test_evidence_floor_matches_this_codebase_usual_convention() -> None:
    assert EVIDENCE_FLOOR == {
        "min_resolved_episodes": 100,
        "min_distinct_asset_clusters": 30,
        "min_distinct_utc_weeks": 4,
    }
    assert MAX_SINGLE_ASSET_EPISODE_SHARE == 0.35
    assert MAX_SINGLE_WEEK_EPISODE_SHARE == 0.45


# --- decluster_cascade_episodes ----------------------------------------------


def test_decluster_merges_trigger_minutes_within_cooldown_into_one_episode() -> None:
    minutes = tuple(
        LiquidationTriggerMinute(_EXCHANGE, _MARKET, "long", _T0 + timedelta(minutes=i))
        for i in (0, 10, 30, 59)
    )
    episodes = decluster_cascade_episodes(minutes)
    assert len(episodes) == 1
    (episode,) = episodes
    assert episode.first_trigger_at == _T0
    assert episode.last_trigger_at == _T0 + timedelta(minutes=59)


def test_decluster_splits_episodes_once_cooldown_is_exceeded() -> None:
    minutes = (
        LiquidationTriggerMinute(_EXCHANGE, _MARKET, "long", _T0),
        LiquidationTriggerMinute(_EXCHANGE, _MARKET, "long", _T0 + timedelta(minutes=61)),
    )
    episodes = decluster_cascade_episodes(minutes)
    assert len(episodes) == 2
    assert episodes[0].first_trigger_at == episodes[0].last_trigger_at == _T0
    assert episodes[1].first_trigger_at == _T0 + timedelta(minutes=61)


def test_decluster_keeps_exchange_market_and_side_independent() -> None:
    minutes = (
        LiquidationTriggerMinute(_EXCHANGE, _MARKET, "long", _T0),
        LiquidationTriggerMinute(_EXCHANGE, _MARKET, "short", _T0),  # same instant, other side
        LiquidationTriggerMinute("binance", _MARKET, "long", _T0),  # same instant, other exchange
        LiquidationTriggerMinute(_EXCHANGE, "ETHUSDT", "long", _T0),  # other market
    )
    episodes = decluster_cascade_episodes(minutes)
    assert len(episodes) == 4
    keys = {(e.exchange, e.native_market_id, e.position_side) for e in episodes}
    assert keys == {
        (_EXCHANGE, _MARKET, "long"),
        (_EXCHANGE, _MARKET, "short"),
        ("binance", _MARKET, "long"),
        (_EXCHANGE, "ETHUSDT", "long"),
    }


def test_decluster_assigns_unique_ids_across_multiple_groups() -> None:
    minutes = (
        LiquidationTriggerMinute(_EXCHANGE, "AAAUSDT", "long", _T0),
        LiquidationTriggerMinute(_EXCHANGE, "BBBUSDT", "short", _T0),
    )
    episodes = decluster_cascade_episodes(minutes)
    assert len({e.episode_id for e in episodes}) == len(episodes)


def test_decluster_rejects_non_positive_cooldown() -> None:
    with pytest.raises(ValueError, match="cooldown_minutes must be positive"):
        decluster_cascade_episodes((), cooldown_minutes=0)


def test_decluster_rejects_non_positive_start_id() -> None:
    with pytest.raises(ValueError, match="start_id must be positive"):
        decluster_cascade_episodes((), start_id=0)


def test_decluster_handles_empty_input() -> None:
    assert decluster_cascade_episodes(()) == ()


# --- resolve_episode: long-liquidation (buy reversion) -----------------------


def test_resolve_episode_long_liquidation_computes_buy_reversion_from_the_low() -> None:
    episode = CascadeEpisode(1, _EXCHANGE, _MARKET, "long", _T0, _T0 + timedelta(minutes=1))
    entry_at = _T0 + timedelta(minutes=1)
    boundary = entry_at + timedelta(minutes=MAX_POSITION_HOLD_MINUTES)
    candles = [
        _bar(_T0, o=100, h=101, low=99, c=100),
        _bar(entry_at, o=100, h=100, low=90, c=95),  # the low: 90
    ]
    # Hold window from entry_at up to (not including) the boundary --
    # the exit bar itself is appended separately below, at a distinct
    # timestamp, so there is exactly one candle at the boundary.
    for i in range(1, MAX_POSITION_HOLD_MINUTES):
        candles.append(_bar(entry_at + timedelta(minutes=i), o=95, h=96, low=94, c=95))
    candles.append(_bar(boundary, o=95, h=97, low=95, c=99))  # exit close: 99

    result = resolve_episode(EpisodeInputs(episode, tuple(candles)))
    assert result.resolved is True
    assert result.direction == "buy"
    assert result.entry_price == pytest.approx(90.0)
    assert result.entry_at == entry_at
    assert result.net_return_pct is not None
    # Gross: (99 - 90) / 90 * 100 ~= 10.0%; net must be strictly less once
    # fees/funding/exit slippage are charged.
    assert 0 < result.net_return_pct < 10.0
    assert result.mfe_pct is not None and result.mfe_pct > 0
    assert result.mae_pct is not None and result.mae_pct <= 0


def test_resolve_episode_short_liquidation_computes_sell_reversion_from_the_high() -> None:
    episode = CascadeEpisode(1, _EXCHANGE, _MARKET, "short", _T0, _T0 + timedelta(minutes=1))
    entry_at = _T0 + timedelta(minutes=1)
    boundary = entry_at + timedelta(minutes=MAX_POSITION_HOLD_MINUTES)
    candles = [
        _bar(_T0, o=100, h=101, low=99, c=100),
        _bar(entry_at, o=100, h=110, low=100, c=105),  # the high: 110
    ]
    for i in range(1, MAX_POSITION_HOLD_MINUTES):
        candles.append(_bar(entry_at + timedelta(minutes=i), o=105, h=106, low=104, c=105))
    candles.append(_bar(boundary, o=105, h=105, low=100, c=101))  # exit close: 101, price fell

    result = resolve_episode(EpisodeInputs(episode, tuple(candles)))
    assert result.resolved is True
    assert result.direction == "sell"
    assert result.entry_price == pytest.approx(110.0)
    # Gross: (110 - 101) / 110 * 100 ~= 8.18%; net must be strictly less.
    assert result.net_return_pct is not None
    assert 0 < result.net_return_pct < 8.18


def test_resolve_episode_unresolved_when_trigger_window_has_no_candles() -> None:
    episode = CascadeEpisode(1, _EXCHANGE, _MARKET, "long", _T0, _T0 + timedelta(minutes=4))
    result = resolve_episode(EpisodeInputs(episode, ()))
    assert result.resolved is False
    assert result.unresolved_reason == "missing_trigger_window_candles"


def test_resolve_episode_unresolved_when_exit_bar_is_missing() -> None:
    episode = CascadeEpisode(1, _EXCHANGE, _MARKET, "long", _T0, _T0)
    candles = (_bar(_T0, o=100, h=101, low=99, c=100),)  # nothing beyond the trigger bar
    result = resolve_episode(EpisodeInputs(episode, candles))
    assert result.resolved is False
    assert result.unresolved_reason == "missing_exit_bar"


def test_resolve_episode_unresolved_when_exit_bar_gap_exceeds_tolerance() -> None:
    episode = CascadeEpisode(1, _EXCHANGE, _MARKET, "long", _T0, _T0)
    boundary = _T0 + timedelta(minutes=MAX_POSITION_HOLD_MINUTES)
    late_ts = boundary + timedelta(minutes=MAX_EXIT_BAR_GAP_MINUTES + 5)
    candles = (
        _bar(_T0, o=100, h=101, low=99, c=100),
        # First available bar is well past the gap tolerance.
        _bar(late_ts, o=100, h=101, low=99, c=100),
    )
    result = resolve_episode(EpisodeInputs(episode, candles))
    assert result.resolved is False
    assert result.unresolved_reason == "exit_bar_gap_exceeded"


def test_resolve_episode_unresolved_when_hold_window_has_an_internal_gap() -> None:
    episode = CascadeEpisode(1, _EXCHANGE, _MARKET, "long", _T0, _T0)
    boundary = _T0 + timedelta(minutes=MAX_POSITION_HOLD_MINUTES)
    candles = [_bar(_T0, o=100, h=101, low=99, c=100)]
    for i in range(1, MAX_POSITION_HOLD_MINUTES):
        if i == 30:
            continue  # a real gap inside the hold window
        candles.append(_bar(_T0 + timedelta(minutes=i), o=100, h=101, low=99, c=100))
    candles.append(_bar(boundary, o=100, h=101, low=99, c=100))
    result = resolve_episode(EpisodeInputs(episode, tuple(candles)))
    assert result.resolved is False
    assert result.unresolved_reason == "hold_window_has_gaps"


def test_exit_boundary_is_ceil_aligned_never_before_the_horizon() -> None:
    # entry exactly minute-aligned -> boundary is exactly entry + horizon.
    episode = CascadeEpisode(1, _EXCHANGE, _MARKET, "long", _T0, _T0)
    boundary = _T0 + timedelta(minutes=MAX_POSITION_HOLD_MINUTES)
    candles = [_bar(_T0, o=100, h=101, low=99, c=100)]
    candles += [
        _bar(_T0 + timedelta(minutes=i), o=100, h=101, low=99, c=100)
        for i in range(1, MAX_POSITION_HOLD_MINUTES - 1)
    ]
    # A bar one minute BEFORE the boundary must never be treated as the exit.
    candles.append(_bar(boundary - timedelta(minutes=1), o=100, h=200, low=1, c=150))
    candles.append(_bar(boundary, o=100, h=101, low=99, c=100))
    result = resolve_episode(EpisodeInputs(episode, tuple(candles)))
    assert result.resolved is True
    assert result.net_return_pct is not None
    # If the pre-boundary spike bar had been used as exit, net_return_pct
    # would be far from 0; the actual boundary bar closes flat at 100.
    assert abs(result.net_return_pct) < 1.0


# --- formal_verdict -----------------------------------------------------


def _stats(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "resolved_episodes": EVIDENCE_FLOOR["min_resolved_episodes"],
        "distinct_asset_clusters": EVIDENCE_FLOOR["min_distinct_asset_clusters"],
        "distinct_utc_weeks": EVIDENCE_FLOOR["min_distinct_utc_weeks"],
        "max_single_asset_share": 0.1,
        "max_single_week_share": 0.1,
        "ci_upper_bound_pct": 1.0,
    }
    base.update(overrides)
    return base


def test_formal_verdict_insufficient_data_below_episode_floor() -> None:
    assert formal_verdict(**_stats(resolved_episodes=99)) == VERDICT_INSUFFICIENT_DATA


def test_formal_verdict_insufficient_data_below_cluster_floor() -> None:
    assert formal_verdict(**_stats(distinct_asset_clusters=29)) == VERDICT_INSUFFICIENT_DATA


def test_formal_verdict_insufficient_data_on_concentration_breach() -> None:
    assert formal_verdict(**_stats(max_single_asset_share=0.5)) == VERDICT_INSUFFICIENT_DATA
    assert formal_verdict(**_stats(max_single_week_share=0.5)) == VERDICT_INSUFFICIENT_DATA


def test_formal_verdict_insufficient_data_when_ci_is_unresolved() -> None:
    assert formal_verdict(**_stats(ci_upper_bound_pct=None)) == VERDICT_INSUFFICIENT_DATA


def test_formal_verdict_positive_warrants_shadow_test_when_ci_upper_bound_is_positive() -> None:
    assert (
        formal_verdict(**_stats(ci_upper_bound_pct=0.01)) == VERDICT_POSITIVE_WARRANTS_SHADOW_TEST
    )


def test_formal_verdict_rejects_when_ci_upper_bound_is_not_positive() -> None:
    assert formal_verdict(**_stats(ci_upper_bound_pct=0.0)) == VERDICT_REJECT
    assert formal_verdict(**_stats(ci_upper_bound_pct=-0.5)) == VERDICT_REJECT
