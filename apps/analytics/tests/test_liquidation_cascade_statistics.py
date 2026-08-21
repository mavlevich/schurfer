from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from schurfer_analytics.liquidation_cascade_grid_search import GridCell, GridSearchResult
from schurfer_analytics.liquidation_cascade_statistics import shuffled_label_control

_START = datetime(2026, 8, 17, tzinfo=UTC)
_MIN_FORMAL_SAMPLE = 10


def _cell(price: float, oi: float, *, mean: float, episodes: int) -> GridCell:
    return GridCell(
        price_drop_trigger_pct=price,
        oi_drop_trigger_pct=oi,
        episodes=episodes,
        resolved_episodes=episodes,
        unresolved_episodes=0,
        distinct_assets=episodes,
        mean_net_return_pct=mean,
        profit_factor=None,
        formal_sample_ready=episodes >= _MIN_FORMAL_SAMPLE,
    )


def _key(index: int) -> tuple[str, str, datetime]:
    return ("bybit", f"SYM{index}USDT", _START + timedelta(minutes=index))


def test_no_eligible_cells_reports_no_observed_best() -> None:
    result = GridSearchResult(
        cells=(_cell(-0.05, -0.15, mean=5.0, episodes=3),),
        cell_membership={(-0.05, -0.15): tuple(_key(i) for i in range(3))},
        episode_returns={_key(i): 5.0 for i in range(3)},
    )
    control = shuffled_label_control(result, min_formal_sample_episodes=_MIN_FORMAL_SAMPLE)
    assert control.observed_best_mean_net_return_pct is None
    assert control.empirical_p_value is None


def test_a_cherry_picked_lucky_cell_from_a_mean_zero_population_is_not_significant() -> None:
    # 4 cells x 12 episodes each, drawn from the SAME mean-zero pool of
    # returns (24 +5s, 24 -5s) RANDOMLY assigned across all 48 episodes
    # regardless of cell boundaries -- whichever cell happens to look best
    # is exactly what a naive grid search would report as an edge, and it
    # should NOT survive the shuffle: with only zero-mean values in
    # circulation, some cell getting a lucky excess of positives is common
    # under reshuffling too. A fixed seed keeps this deterministic.
    pool = [5.0] * 24 + [-5.0] * 24
    random.Random(7).shuffle(pool)  # noqa: S311
    keys_by_cell: dict[tuple[float, float], tuple[tuple[str, str, datetime], ...]] = {}
    episode_returns: dict[tuple[str, str, datetime], float] = {}
    cell_defs = [(-0.03, 0.0), (-0.05, -0.05), (-0.07, -0.10), (-0.10, -0.15)]
    counter = 0
    for cell_key in cell_defs:
        members = []
        for _ in range(12):
            key = _key(counter)
            episode_returns[key] = pool[counter]
            counter += 1
            members.append(key)
        keys_by_cell[cell_key] = tuple(members)

    cells = tuple(
        _cell(
            price,
            oi,
            mean=sum(episode_returns[k] for k in members) / len(members),
            episodes=len(members),
        )
        for (price, oi), members in keys_by_cell.items()
    )
    result = GridSearchResult(
        cells=cells, cell_membership=keys_by_cell, episode_returns=episode_returns
    )
    control = shuffled_label_control(
        result, min_formal_sample_episodes=_MIN_FORMAL_SAMPLE, iterations=500
    )
    assert control.observed_best_mean_net_return_pct is not None
    assert control.empirical_p_value is not None
    assert control.empirical_p_value > 0.05


def test_a_genuinely_separated_edge_is_significant() -> None:
    keys_by_cell: dict[tuple[float, float], tuple[tuple[str, str, datetime], ...]] = {}
    episode_returns: dict[tuple[str, str, datetime], float] = {}
    counter = 0
    # Winner cell: 12 episodes, every one strongly positive.
    winner_members = []
    for _ in range(12):
        key = _key(counter)
        counter += 1
        winner_members.append(key)
        episode_returns[key] = 50.0
    keys_by_cell[(-0.05, -0.15)] = tuple(winner_members)
    # Three other cells: 12 episodes each, tightly clustered near zero --
    # nowhere near the winner's magnitude even in the best case.
    for cell_index in range(3):
        members = []
        for offset in range(12):
            key = _key(counter)
            counter += 1
            members.append(key)
            episode_returns[key] = -0.5 + (offset % 3) * 0.1
        keys_by_cell[(-0.03 - cell_index * 0.01, 0.0 - cell_index * 0.01)] = tuple(members)

    cells = tuple(
        _cell(
            price,
            oi,
            mean=sum(episode_returns[k] for k in members) / len(members),
            episodes=len(members),
        )
        for (price, oi), members in keys_by_cell.items()
    )
    result = GridSearchResult(
        cells=cells, cell_membership=keys_by_cell, episode_returns=episode_returns
    )
    control = shuffled_label_control(
        result, min_formal_sample_episodes=_MIN_FORMAL_SAMPLE, iterations=500
    )
    assert control.observed_best_mean_net_return_pct == 50.0
    assert control.empirical_p_value is not None
    assert control.empirical_p_value < 0.05


def test_shuffled_label_control_is_deterministic() -> None:
    keys = [_key(i) for i in range(20)]
    episode_returns = {key: (10.0 if i < 10 else -10.0) for i, key in enumerate(keys)}
    cells = (_cell(-0.05, -0.15, mean=10.0, episodes=10),)
    result = GridSearchResult(
        cells=cells,
        cell_membership={(-0.05, -0.15): tuple(keys[:10])},
        episode_returns=episode_returns,
    )
    kwargs = {"min_formal_sample_episodes": _MIN_FORMAL_SAMPLE, "iterations": 300}
    first = shuffled_label_control(result, **kwargs)
    second = shuffled_label_control(result, **kwargs)
    assert first == second
