"""Deterministic cluster-bootstrap inference for episode-level research."""

from __future__ import annotations

import hashlib
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from statistics import fmean

CLUSTER_BOOTSTRAP_VERSION = "asset_cluster_percentile_bootstrap_v1"
HOLM_CORRECTION_VERSION = "holm_step_down_v1"
BOOTSTRAP_SEED_DERIVATION = "sha256_label_u64_v1"
DEFAULT_BOOTSTRAP_ITERATIONS = 10_000
DEFAULT_BOOTSTRAP_SEED = 20_260_729
DEFAULT_CONFIDENCE_LEVEL = 0.95
DEFAULT_FAMILY_ALPHA = 0.05


@dataclass(frozen=True)
class ClusterObservation:
    cluster_key: str
    value: float

    def __post_init__(self) -> None:
        if not self.cluster_key.strip():
            raise ValueError("cluster key must not be empty")
        if not math.isfinite(self.value):
            raise ValueError("observation value must be finite")


@dataclass(frozen=True)
class BootstrapEstimate:
    episodes: int
    clusters: int
    point_estimate: float
    lower_bound: float
    upper_bound: float


@dataclass(frozen=True)
class BootstrapComputation:
    estimate: BootstrapEstimate
    samples: tuple[float, ...]


@dataclass(frozen=True)
class HolmDecision:
    key: str
    rank: int
    raw_p_value: float
    adjusted_p_value: float
    critical_alpha: float
    rejected: bool


def derived_seed(base_seed: int, label: str) -> int:
    if base_seed < 0:
        raise ValueError("base seed must be non-negative")
    if not label.strip():
        raise ValueError("seed label must not be empty")
    digest = hashlib.sha256(f"{base_seed}:{label}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False)


def percentile(sorted_values: tuple[float, ...], probability: float) -> float:
    if not sorted_values:
        raise ValueError("percentile requires at least one value")
    if not 0 <= probability <= 1:
        raise ValueError("percentile probability must be between zero and one")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * probability
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return sorted_values[lower_index]
    weight = position - lower_index
    return sorted_values[lower_index] * (1 - weight) + sorted_values[upper_index] * weight


def confidence_interval(
    sorted_samples: tuple[float, ...],
    *,
    confidence_level: float,
) -> tuple[float, float]:
    if not 0 < confidence_level < 1:
        raise ValueError("confidence level must be between zero and one")
    tail = (1 - confidence_level) / 2
    return (
        percentile(sorted_samples, tail),
        percentile(sorted_samples, 1 - tail),
    )


def cluster_bootstrap_mean(
    observations: tuple[ClusterObservation, ...],
    *,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
) -> BootstrapComputation:
    """Bootstrap whole asset clusters while retaining episode-weighted expectancy."""
    if not observations:
        raise ValueError("cluster bootstrap requires observations")
    if iterations < 100:
        raise ValueError("cluster bootstrap requires at least 100 iterations")
    if seed < 0:
        raise ValueError("bootstrap seed must be non-negative")
    if not 0 < confidence_level < 1:
        raise ValueError("confidence level must be between zero and one")

    grouped: dict[str, list[float]] = defaultdict(list)
    for observation in observations:
        grouped[observation.cluster_key].append(observation.value)
    clusters = tuple(sorted(grouped))
    values_by_cluster = tuple(tuple(grouped[key]) for key in clusters)
    # A deterministic research PRNG is required for reproducible bootstrap samples.
    random_source = random.Random(seed)  # noqa: S311
    samples: list[float] = []
    for _ in range(iterations):
        sampled_values: list[float] = []
        for _ in clusters:
            sampled_values.extend(
                values_by_cluster[random_source.randrange(len(values_by_cluster))]
            )
        samples.append(fmean(sampled_values))

    ordered_samples = tuple(sorted(samples))
    lower, upper = confidence_interval(
        ordered_samples,
        confidence_level=confidence_level,
    )
    return BootstrapComputation(
        estimate=BootstrapEstimate(
            episodes=len(observations),
            clusters=len(clusters),
            point_estimate=fmean(observation.value for observation in observations),
            lower_bound=lower,
            upper_bound=upper,
        ),
        samples=ordered_samples,
    )


def cluster_bootstrap_mean_null_p_value(
    observations: tuple[ClusterObservation, ...],
    *,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> float:
    """Test mean zero after imposing the null on whole-cluster resampling."""
    if not observations:
        raise ValueError("null bootstrap requires observations")
    observed_mean = fmean(observation.value for observation in observations)
    centered = tuple(
        ClusterObservation(
            cluster_key=observation.cluster_key,
            value=observation.value - observed_mean,
        )
        for observation in observations
    )
    null_samples = cluster_bootstrap_mean(
        centered,
        iterations=iterations,
        seed=seed,
        confidence_level=DEFAULT_CONFIDENCE_LEVEL,
    ).samples
    extreme = sum(abs(sample) >= abs(observed_mean) for sample in null_samples)
    return (extreme + 1) / (iterations + 1)


def holm_step_down(
    p_values: dict[str, float],
    *,
    family_alpha: float = DEFAULT_FAMILY_ALPHA,
) -> tuple[HolmDecision, ...]:
    if not p_values:
        raise ValueError("Holm correction requires at least one comparison")
    if not 0 < family_alpha < 1:
        raise ValueError("family alpha must be between zero and one")
    for key, value in p_values.items():
        if not key.strip():
            raise ValueError("comparison key must not be empty")
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("p-values must be finite and between zero and one")

    ordered = sorted(p_values.items(), key=lambda item: (item[1], item[0]))
    family_size = len(ordered)
    running_adjusted = 0.0
    continue_rejecting = True
    decisions: list[HolmDecision] = []
    for index, (key, raw_p_value) in enumerate(ordered):
        remaining = family_size - index
        critical_alpha = family_alpha / remaining
        running_adjusted = max(running_adjusted, remaining * raw_p_value)
        rejected = continue_rejecting and raw_p_value <= critical_alpha
        if not rejected:
            continue_rejecting = False
        decisions.append(
            HolmDecision(
                key=key,
                rank=index + 1,
                raw_p_value=raw_p_value,
                adjusted_p_value=min(1.0, running_adjusted),
                critical_alpha=critical_alpha,
                rejected=rejected,
            )
        )
    return tuple(sorted(decisions, key=lambda decision: decision.key))


def leave_one_cluster_out_means(
    observations: tuple[ClusterObservation, ...],
    cluster_keys: tuple[str, ...],
) -> tuple[tuple[str, float], ...]:
    if not observations:
        raise ValueError("sensitivity analysis requires observations")
    results: list[tuple[str, float]] = []
    for cluster_key in cluster_keys:
        retained = [
            observation.value
            for observation in observations
            if observation.cluster_key != cluster_key
        ]
        if not retained:
            raise ValueError("cluster exclusion removed every observation")
        results.append((cluster_key, fmean(retained)))
    return tuple(results)
