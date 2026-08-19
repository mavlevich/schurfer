from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.momentum_flow_bidirectional_burst_study import (
    BurstEpisode,
    BurstMinute,
    build_horizon_economics,
    compute_episode_outcomes,
    decluster_episodes,
)

START = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)


def _minute(
    offset_minutes: int,
    *,
    buy: float = 0.0,
    sell: float = 0.0,
    price: float = 100.0,
    symbol: str = "BTCUSDT",
    exchange: str = "bybit",
) -> BurstMinute:
    return BurstMinute(
        exchange=exchange,
        symbol=symbol,
        bucket_start=START + timedelta(minutes=offset_minutes),
        close_price=price,
        buy_burst_pct_5m=buy,
        sell_burst_pct_5m=sell,
    )


def test_decluster_merges_a_contiguous_run_into_one_episode() -> None:
    minutes = (
        _minute(0, buy=12.0),
        _minute(1, buy=15.0),
        _minute(2, buy=11.0),
    )
    episodes = decluster_episodes(
        minutes, direction="buy", threshold_pct=10.0, refractory_minutes=60
    )
    assert len(episodes) == 1
    (episode,) = episodes
    assert episode.trigger_at == START
    assert episode.peak_burst_pct == 15.0
    assert episode.extreme_minutes == 3


def test_decluster_splits_on_a_gap_past_the_refractory_window() -> None:
    minutes = (
        _minute(0, buy=12.0),
        _minute(200, buy=13.0),  # 200min later, past a 60min refractory
    )
    episodes = decluster_episodes(
        minutes, direction="buy", threshold_pct=10.0, refractory_minutes=60
    )
    assert len(episodes) == 2
    assert episodes[0].trigger_at == START
    assert episodes[1].trigger_at == START + timedelta(minutes=200)


def test_decluster_splits_when_gap_exactly_equals_the_refractory_window() -> None:
    # Regression (code review, 2026-08-18): the boundary used a strict `>`,
    # so a gap of EXACTLY refractory_minutes stayed inside the same episode
    # -- contradicting the module's own doc comment ("a new episode starts
    # only once refractory_minutes has passed").
    minutes = (
        _minute(0, buy=12.0),
        _minute(60, buy=13.0),  # exactly 60min later, refractory_minutes=60
    )
    episodes = decluster_episodes(
        minutes, direction="buy", threshold_pct=10.0, refractory_minutes=60
    )
    assert len(episodes) == 2
    assert episodes[0].trigger_at == START
    assert episodes[1].trigger_at == START + timedelta(minutes=60)


def test_decluster_keeps_one_episode_when_gap_is_within_refractory() -> None:
    # Regression: this is the exact bug the 2026-08-17 first-pass screen had
    # -- declustering only by symbol, with no time-based refractory window
    # at all, so any two extreme minutes for the same symbol (even weeks
    # apart) collapsed into "one symbol", and any two ADJACENT extreme
    # minutes counted as fully independent. Neither is right: a real
    # refractory window is what actually defines "the same episode."
    minutes = (
        _minute(0, buy=12.0),
        _minute(45, buy=13.0),  # 45min later, inside a 60min refractory
    )
    episodes = decluster_episodes(
        minutes, direction="buy", threshold_pct=10.0, refractory_minutes=60
    )
    assert len(episodes) == 1
    assert episodes[0].extreme_minutes == 2
    assert episodes[0].peak_burst_pct == 13.0


def test_decluster_ignores_minutes_below_threshold() -> None:
    minutes = (_minute(0, buy=9.99),)
    episodes = decluster_episodes(
        minutes, direction="buy", threshold_pct=10.0, refractory_minutes=60
    )
    assert episodes == ()


def test_decluster_tracks_symbols_independently() -> None:
    minutes = (
        _minute(0, buy=12.0, symbol="AAAUSDT"),
        _minute(0, buy=12.0, symbol="BBBUSDT"),
    )
    episodes = decluster_episodes(
        minutes, direction="buy", threshold_pct=10.0, refractory_minutes=60
    )
    assert {e.symbol for e in episodes} == {"AAAUSDT", "BBBUSDT"}


def test_decluster_direction_is_independent_buy_vs_sell() -> None:
    minutes = (_minute(0, buy=12.0, sell=2.0),)
    buy_episodes = decluster_episodes(
        minutes, direction="buy", threshold_pct=10.0, refractory_minutes=60
    )
    sell_episodes = decluster_episodes(
        minutes, direction="sell", threshold_pct=10.0, refractory_minutes=60
    )
    assert len(buy_episodes) == 1
    assert sell_episodes == ()


def test_decluster_rejects_invalid_direction() -> None:
    with pytest.raises(ValueError, match="direction"):
        decluster_episodes((), direction="up", threshold_pct=10.0, refractory_minutes=60)


def test_decluster_rejects_non_positive_start_id() -> None:
    with pytest.raises(ValueError, match="start_id"):
        decluster_episodes(
            (), direction="buy", threshold_pct=10.0, refractory_minutes=60, start_id=0
        )


def test_decluster_start_id_keeps_two_separate_calls_from_colliding() -> None:
    # Regression: found running the real report against real prod data --
    # buy and sell are always two separate decluster_episodes calls, and
    # both restarted episode_id at 1 by default, so a buy episode and a
    # sell episode could share an id. build_horizon_economics' downstream
    # ChallengerEpisode uniqueness check rejects that outright once both
    # directions' episodes are combined into one inference call.
    buy_minutes = (_minute(0, buy=12.0, symbol="AAAUSDT"), _minute(0, buy=12.0, symbol="BBBUSDT"))
    sell_minutes = (_minute(0, sell=12.0, symbol="CCCUSDT"),)
    buy_episodes = decluster_episodes(
        buy_minutes, direction="buy", threshold_pct=10.0, refractory_minutes=60, start_id=1
    )
    sell_episodes = decluster_episodes(
        sell_minutes,
        direction="sell",
        threshold_pct=10.0,
        refractory_minutes=60,
        start_id=1 + len(buy_episodes),
    )
    all_ids = [e.episode_id for e in (*buy_episodes, *sell_episodes)]
    assert len(all_ids) == len(set(all_ids))
    assert sorted(all_ids) == [1, 2, 3]


def _episode(trigger_offset: int = 0, symbol: str = "BTCUSDT", episode_id: int = 1) -> BurstEpisode:
    return BurstEpisode(
        episode_id=episode_id,
        exchange="bybit",
        symbol=symbol,
        direction="buy",
        trigger_at=START + timedelta(minutes=trigger_offset),
        peak_burst_pct=12.0,
        extreme_minutes=1,
    )


def test_compute_outcomes_excludes_episode_with_no_trigger_price() -> None:
    episode = _episode()
    outcomes = compute_episode_outcomes((episode,), price_at={})
    assert outcomes == ()


def test_compute_outcomes_precursor_and_horizons_use_exact_timestamps_only() -> None:
    episode = _episode()
    price_at = {
        ("BTCUSDT", episode.trigger_at): 100.0,
        ("BTCUSDT", episode.trigger_at - timedelta(minutes=60)): 90.0,
        ("BTCUSDT", episode.trigger_at + timedelta(minutes=15)): 110.0,
        # 60min horizon deliberately missing -- must be absent, not guessed
        # from a neighboring bar (the entire point of exact-timestamp
        # lookup over LEAD/LAG row-position windows).
        ("BTCUSDT", episode.trigger_at + timedelta(minutes=240)): 95.0,
    }
    (outcome,) = compute_episode_outcomes((episode,), price_at=price_at)
    assert outcome.trigger_price == 100.0
    assert outcome.precursor_return_pct == pytest.approx((100.0 / 90.0 - 1) * 100)
    assert 15 in outcome.horizon_returns_pct
    assert 60 not in outcome.horizon_returns_pct
    assert 240 in outcome.horizon_returns_pct

    long_15, short_15 = outcome.horizon_returns_pct[15]
    assert long_15 == pytest.approx(10.0)
    assert short_15 == pytest.approx(-10.0)

    long_240, short_240 = outcome.horizon_returns_pct[240]
    assert long_240 == pytest.approx(-5.0)
    assert short_240 == pytest.approx(5.0)


def test_compute_outcomes_precursor_is_none_without_an_exact_precursor_price() -> None:
    episode = _episode()
    price_at = {("BTCUSDT", episode.trigger_at): 100.0}
    (outcome,) = compute_episode_outcomes((episode,), price_at=price_at)
    assert outcome.precursor_return_pct is None
    assert outcome.horizon_returns_pct == {}


def test_build_horizon_economics_computes_after_cost_and_baseline() -> None:
    episode = _episode(episode_id=1, symbol="BTCUSDT")
    episode2 = _episode(episode_id=2, symbol="ETHUSDT")
    price_at = {
        ("BTCUSDT", episode.trigger_at): 100.0,
        ("BTCUSDT", episode.trigger_at + timedelta(minutes=15)): 110.0,
        ("ETHUSDT", episode2.trigger_at): 200.0,
        ("ETHUSDT", episode2.trigger_at + timedelta(minutes=15)): 190.0,
    }
    outcomes = compute_episode_outcomes(
        (episode, episode2), price_at=price_at, horizons_minutes=(15,)
    )
    baseline = {"BTCUSDT": {15: 1.0}, "ETHUSDT": {15: -0.5}}
    economics = build_horizon_economics(outcomes, baseline, horizons_minutes=(15,))

    long_15 = next(e for e in economics if e.horizon_minutes == 15 and e.side == "long")
    assert long_15.n == 2
    # Gross: BTCUSDT +10%, ETHUSDT -5% -> mean +2.5%
    assert long_15.mean_gross_pct == pytest.approx(2.5)
    # Net must be strictly less than gross once fees/funding are deducted.
    assert long_15.mean_net_pct < long_15.mean_gross_pct
    assert long_15.win_rate_pct == pytest.approx(50.0)
    assert long_15.baseline_mean_gross_pct == pytest.approx((1.0 + -0.5) / 2)

    short_15 = next(e for e in economics if e.horizon_minutes == 15 and e.side == "short")
    assert short_15.mean_gross_pct == pytest.approx(-2.5)


def test_build_horizon_economics_sign_flips_the_short_side_baseline_for_inference_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression (code review, 2026-08-18): the short side's matched
    # control was sign-flipped for the report's own display value
    # (baseline_mean_gross_pct) but NOT for the ChallengerEpisode fed into
    # the cluster-bootstrap inference engine -- silently scoring every
    # short-side horizon's verdict against the un-flipped long baseline.
    # Reaching build_challenger_inference's own "formal_sample_ready" gate
    # needs 100+ episodes across 30+ clusters, so this instead captures the
    # exact ChallengerEpisode tuple build_horizon_economics constructs and
    # checks its baseline_return_pct values directly, rather than running
    # the full bootstrap.
    import schurfer_analytics.momentum_flow_bidirectional_burst_study as study
    from schurfer_analytics.challenger_inference import (
        ChallengerEpisode,
        ChallengerInference,
    )
    from schurfer_analytics.challenger_inference import (
        build_challenger_inference as real_build_challenger_inference,
    )

    captured: dict[str, tuple[ChallengerEpisode, ...]] = {}

    def _spy(
        episodes: tuple[ChallengerEpisode, ...], variant_keys: tuple[str, ...], **kwargs: object
    ) -> ChallengerInference:
        captured["episodes"] = episodes
        return real_build_challenger_inference(episodes, variant_keys, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(study, "build_challenger_inference", _spy)

    episode = _episode(episode_id=1, symbol="BTCUSDT")
    episode2 = _episode(episode_id=2, symbol="ETHUSDT")
    price_at = {
        ("BTCUSDT", episode.trigger_at): 100.0,
        ("BTCUSDT", episode.trigger_at + timedelta(minutes=15)): 110.0,
        ("ETHUSDT", episode2.trigger_at): 200.0,
        ("ETHUSDT", episode2.trigger_at + timedelta(minutes=15)): 190.0,
    }
    outcomes = compute_episode_outcomes(
        (episode, episode2), price_at=price_at, horizons_minutes=(15,)
    )
    baseline = {"BTCUSDT": {15: 1.0}, "ETHUSDT": {15: -0.5}}
    study.build_horizon_economics(outcomes, baseline, horizons_minutes=(15,))

    assert "episodes" in captured
    by_cluster = {ep.cluster_key: ep.baseline_return_pct for ep in captured["episodes"]}
    # Both long and short calls share the spy/captured dict across the four
    # (horizon, side) iterations -- only the LAST call (short, since it
    # runs after long in the fixed ("long", "short") iteration order) is
    # what's left in `captured` by the time the loop finishes, which is
    # exactly the side this regression targets.
    assert by_cluster == {"BTCUSDT": pytest.approx(-1.0), "ETHUSDT": pytest.approx(0.5)}
