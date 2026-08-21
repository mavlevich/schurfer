"""Shuffled-label robustness control for analysis/liquidation-cascade-validation-v2.

Answers the plan's own "shuffled labels don't create durable alpha" check.
A naive shuffle of a fixed set of realized returns leaves its mean
unchanged (a permutation of a fixed multiset never changes the multiset's
own mean) -- so the meaningful null here is not "shuffle the returns" in
isolation, it is "how often would the grid search's OWN cell-selection step
(arg-max mean return, subject to the materiality floor) surface a result
this good out of label noise". This re-runs that selection step many times
against episode returns whose (exchange, symbol, trigger_at) identity has
been randomly reassigned among themselves -- cell membership (which
episodes a given price/OI-threshold combination groups together) stays
fixed, only which episode "owns" which realized return is shuffled. A real
predictive relationship between the causal thresholds and the outcome
should make the true best-cell mean look unusual against this null; if it
does not, the apparent edge is a multiple-comparisons artifact of the grid
search itself, not a real signal -- exactly what the flawed
`feature/alpha-research` script (`583213f`) had no way to detect.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from statistics import fmean
from typing import TYPE_CHECKING

from .clustered_inference import derived_seed

if TYPE_CHECKING:
    from .liquidation_cascade_grid_search import GridSearchResult

DEFAULT_ITERATIONS = 2_000
DEFAULT_SEED = 20_260_729
SHUFFLE_LABEL = "liquidation_cascade_grid_selection_shuffle_v1"


@dataclass(frozen=True)
class ShuffledLabelControl:
    observed_best_mean_net_return_pct: float | None
    iterations: int
    shuffled_at_or_above_observed: int
    empirical_p_value: float | None


def shuffled_label_control(
    result: GridSearchResult,
    *,
    min_formal_sample_episodes: int,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int = DEFAULT_SEED,
) -> ShuffledLabelControl:
    if iterations < 100:
        raise ValueError("shuffled label control requires at least 100 iterations")
    eligible_means = [
        cell.mean_net_return_pct
        for cell in result.cells
        if cell.formal_sample_ready and cell.mean_net_return_pct is not None
    ]
    observed_best = max(eligible_means) if eligible_means else None
    episode_keys = tuple(result.episode_returns)
    if observed_best is None or not episode_keys:
        return ShuffledLabelControl(
            observed_best_mean_net_return_pct=observed_best,
            iterations=iterations,
            shuffled_at_or_above_observed=0,
            empirical_p_value=None,
        )

    values = [result.episode_returns[key] for key in episode_keys]
    rng = random.Random(derived_seed(seed, SHUFFLE_LABEL))  # noqa: S311
    extreme = 0
    for _ in range(iterations):
        shuffled_values = values[:]
        rng.shuffle(shuffled_values)
        shuffled_returns = dict(zip(episode_keys, shuffled_values, strict=True))
        best_this_iteration = float("-inf")
        for members in result.cell_membership.values():
            resolved = [shuffled_returns[key] for key in members if key in shuffled_returns]
            if len(resolved) < min_formal_sample_episodes:
                continue
            best_this_iteration = max(best_this_iteration, fmean(resolved))
        if best_this_iteration >= observed_best:
            extreme += 1

    return ShuffledLabelControl(
        observed_best_mean_net_return_pct=observed_best,
        iterations=iterations,
        shuffled_at_or_above_observed=extreme,
        empirical_p_value=(extreme + 1) / (iterations + 1),
    )
