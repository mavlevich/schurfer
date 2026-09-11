"""Pure-logic coverage for the net-buy accumulation discovery verdict."""

from __future__ import annotations

from schurfer_analytics.net_buy_accumulation import (
    VERDICT_CANDIDATE,
    VERDICT_STOP,
    VERDICT_TOO_RARE_OR_ILLIQUID,
    BootstrapResult,
    FiredEpisode,
    block_bootstrap_by_day,
    cost_pct_at_horizon,
    evaluate_joint,
    holm,
)


def _episode(
    *,
    primary: str = "P-MAG",
    score: float,
    ret_pct: float | None,
    cluster: str,
    day: str,
    week: str,
    idx: int,
    exchange: str = "bybit",
    tradable: bool = True,
    week_covered: bool = True,
) -> FiredEpisode:
    entry = 100.0
    # Choose exit so the adjusted return equals ret_pct exactly (gross = ret_pct +
    # cost); None ret means unresolved.
    if ret_pct is None:
        exit_close: float | None = None
    else:
        gross = ret_pct + cost_pct_at_horizon()
        exit_close = entry * (1 + gross / 100.0)
    return FiredEpisode(
        primary=primary,
        instrument=f"{exchange}:{cluster}USDT-{idx}",
        cluster=cluster,
        exchange=exchange,
        fire_ts=f"2026-08-{18 + (idx % 10):02d}T{idx % 24:02d}:00:00Z",
        utc_day=day,
        utc_week=week,
        week_fully_covered=week_covered,
        score=score,
        entry_close=entry,
        exit_close=exit_close,
        baseline_daily_activity_usd=10_000_000.0 if tradable else 1_000.0,
    )


def test_adj_return_subtracts_fees_and_funding() -> None:
    ep = _episode(score=1.0, ret_pct=3.0, cluster="AAA", day="2026-08-20", week="2026-W34", idx=1)
    # ret_pct was baked in as the adjusted return.
    assert ep.gross_return_pct is not None
    assert abs(ep.adj_return_pct - 3.0) < 1e-9  # type: ignore[operator]
    assert ep.gross_return_pct > ep.adj_return_pct  # costs are positive


def test_unresolved_episode_has_no_return() -> None:
    ep = _episode(score=1.0, ret_pct=None, cluster="AAA", day="2026-08-20", week="2026-W34", idx=1)
    assert ep.resolved is False
    assert ep.adj_return_pct is None


def test_block_bootstrap_is_reproducible() -> None:
    by_day = {f"d{i}": [1.0, 2.0, -0.5] for i in range(8)}
    a = block_bootstrap_by_day(by_day)
    b = block_bootstrap_by_day(by_day)
    assert a == b
    assert isinstance(a, BootstrapResult)


def test_holm_family_of_two() -> None:
    rejected = holm({"P-MAG": 0.001, "P-SHAPE": 0.6})
    assert rejected["P-MAG"] is True
    assert rejected["P-SHAPE"] is False


def _cohort(mean_ret: float, *, primary: str) -> list[FiredEpisode]:
    eps: list[FiredEpisode] = []
    idx = 0
    # 5 score bands x 160 = 800 fires, 40 clusters, 4 weeks, many days, tradable.
    for band in range(5):
        for j in range(160):
            idx += 1
            cluster = f"C{idx % 40:02d}"
            week = f"2026-W{34 + (idx % 4)}"
            day = f"2026-08-{18 + (idx % 20):02d}"
            # returns rise with the score band so the strategy is positive and
            # monotone; add small variation.
            ret = mean_ret + band * 0.2 + (j % 3) * 0.05
            eps.append(
                _episode(
                    primary=primary,
                    score=float(band * 1000 + j),
                    ret_pct=ret,
                    cluster=cluster,
                    day=day,
                    week=week,
                    idx=idx,
                    exchange="bybit" if idx % 2 else "binance",
                )
            )
    return eps


def test_mature_negative_is_stop_without_diversity() -> None:
    # 120 resolved fires, all negative, only 2 clusters / 1 week: still a stop.
    eps = [
        _episode(
            score=float(i),
            ret_pct=-1.0,
            cluster="A" if i % 2 else "B",
            day="2026-08-20",
            week="2026-W34",
            idx=i,
        )
        for i in range(120)
    ]
    final = evaluate_joint({"P-MAG": eps})
    assert final["P-MAG"].verdict_prebonf == VERDICT_STOP


def test_illiquid_cohort_is_too_rare_or_illiquid() -> None:
    eps = _cohort(1.0, primary="P-MAG")
    eps = [FiredEpisode(**{**e.__dict__, "baseline_daily_activity_usd": 1_000.0}) for e in eps]
    final = evaluate_joint({"P-MAG": eps})
    assert final["P-MAG"].verdict_prebonf == VERDICT_TOO_RARE_OR_ILLIQUID


def test_strong_positive_cohort_is_candidate() -> None:
    final = evaluate_joint({"P-MAG": _cohort(1.0, primary="P-MAG")})
    res = final["P-MAG"]
    assert res.verdict_prebonf == VERDICT_CANDIDATE
    assert res.bootstrap is not None
    assert res.bootstrap.lower_bound > 0
    assert res.min_fires_per_quantile >= 150
    assert res.distinct_clusters >= 30
