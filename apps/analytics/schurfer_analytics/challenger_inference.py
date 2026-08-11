"""Reusable formal inference for a pre-registered paired challenger family."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from .clustered_inference import (
    BOOTSTRAP_SEED_DERIVATION,
    CLUSTER_BOOTSTRAP_VERSION,
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_BOOTSTRAP_SEED,
    DEFAULT_CONFIDENCE_LEVEL,
    DEFAULT_FAMILY_ALPHA,
    HOLM_CORRECTION_VERSION,
    BootstrapComputation,
    BootstrapEstimate,
    ClusterObservation,
    cluster_bootstrap_mean,
    cluster_bootstrap_mean_null_p_value,
    confidence_interval,
    derived_seed,
    holm_step_down,
    leave_one_cluster_out_means,
)
from .replay import DIRECTIONAL_EPISODES, FORMAL_EPISODES, MIN_FORMAL_CLUSTERS

DEFAULT_INFERENCE_VERSION = "paired_challenger_formal_inference_v1"
TOP_CLUSTER_SENSITIVITY_COUNT = 5


@dataclass(frozen=True)
class InferenceSettings:
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS
    seed: int = DEFAULT_BOOTSTRAP_SEED
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL
    family_alpha: float = DEFAULT_FAMILY_ALPHA

    def __post_init__(self) -> None:
        if self.iterations < 100:
            raise ValueError("inference requires at least 100 bootstrap iterations")
        if self.seed < 0:
            raise ValueError("inference seed must be non-negative")
        if not 0 < self.confidence_level < 1:
            raise ValueError("confidence level must be between zero and one")
        if not 0 < self.family_alpha < 1:
            raise ValueError("family alpha must be between zero and one")


DEFAULT_INFERENCE_SETTINGS = InferenceSettings()


@dataclass(frozen=True)
class ChallengerEpisode:
    pump_event_id: int
    cluster_key: str
    baseline_return_pct: float | None
    challenger_returns_pct: tuple[tuple[str, float | None], ...]
    # Whether this episode was a real trade versus a zero_return_cash_episode
    # (e.g. a threshold that was never crossed). Optional: only required when
    # build_challenger_inference's minimum_triggered_episodes gate is used —
    # families where every resolved episode is inherently a real trade (most
    # exit/score challengers) can leave these at their defaults.
    baseline_triggered: bool = True
    challenger_triggered: tuple[tuple[str, bool], ...] = ()

    def __post_init__(self) -> None:
        if self.pump_event_id <= 0:
            raise ValueError("pump event id must be positive")
        if not self.cluster_key.strip():
            raise ValueError("cluster key must not be empty")
        keys = [key for key, _ in self.challenger_returns_pct]
        if any(not key.strip() for key in keys) or len(keys) != len(set(keys)):
            raise ValueError("challenger keys must be unique and non-empty")
        if self.challenger_triggered and {key for key, _ in self.challenger_triggered} != set(keys):
            raise ValueError("challenger_triggered keys must match challenger_returns_pct keys")
        values = [
            value
            for value in (
                self.baseline_return_pct,
                *(value for _, value in self.challenger_returns_pct),
            )
            if value is not None
        ]
        if any(not math.isfinite(value) for value in values):
            raise ValueError("inference returns must be finite")

    def challenger_return(self, key: str) -> float | None:
        for candidate_key, value in self.challenger_returns_pct:
            if candidate_key == key:
                return value
        return None


@dataclass(frozen=True)
class ClusterConcentration:
    cluster_key: str
    episodes: int
    share_pct: float


@dataclass(frozen=True)
class InferenceReadiness:
    status: str
    eligible_episodes: int
    formal_sample_episodes: int
    formal_sample_clusters: int
    baseline_resolved: int
    completely_paired_episodes: int
    # Populated only when build_challenger_inference was called with
    # minimum_triggered_episodes — see the module docstring on ChallengerEpisode.
    minimum_triggered_episodes: int | None = None
    least_triggered_variant: str | None = None
    least_triggered_count: int | None = None
    # Always populated (defaults to 0, meaning no tolerance was configured). See
    # build_challenger_inference's max_unresolved_tolerance.
    max_unresolved_tolerance: int = 0


@dataclass(frozen=True)
class StrategyInference:
    strategy_key: str
    estimate: BootstrapEstimate
    verdict: str
    minimum_leave_one_cluster_out_pct: float
    leave_one_cluster_out: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class PairedInference:
    variant_key: str
    estimate: BootstrapEstimate
    holm_rank: int
    raw_p_value: float
    holm_adjusted_p_value: float
    holm_critical_alpha: float
    holm_rejected: bool
    familywise_confidence_level: float
    familywise_lower_bound: float
    familywise_upper_bound: float


@dataclass(frozen=True)
class ChallengerFormalResult:
    variant_key: str
    strategy: StrategyInference
    paired: PairedInference
    verdict: str


@dataclass(frozen=True)
class ChallengerInference:
    inference_version: str
    bootstrap_version: str
    holm_version: str
    seed_derivation: str
    settings: InferenceSettings
    readiness: InferenceReadiness
    formal_sample_event_ids: tuple[int, ...]
    cluster_concentration: tuple[ClusterConcentration, ...]
    baseline: StrategyInference | None
    challengers: tuple[ChallengerFormalResult, ...]


def _readiness(
    *,
    eligible_episodes: int,
    formal_sample_clusters: int,
    baseline_resolved: int,
    completely_paired: int,
    formal_sample_size: int,
    minimum_triggered_episodes: int | None,
    least_triggered_count: int | None,
    max_unresolved_tolerance: int,
    directional_episodes: int,
    formal_episodes: int,
    min_formal_clusters: int,
) -> str:
    if eligible_episodes < directional_episodes:
        return "collecting"
    if eligible_episodes < formal_episodes:
        return "directional_only"
    if formal_sample_clusters < min_formal_clusters:
        return "insufficient_diversity"
    # completely_paired always requires baseline_return_pct to be resolved (a
    # necessary condition of "completely paired"), so completely_paired <=
    # baseline_resolved always — gating on completely_paired's shortfall alone
    # already implies baseline_resolved clears the same bar.
    if formal_sample_size - completely_paired > max_unresolved_tolerance:
        # max_unresolved_tolerance defaults to 0 (unchanged strict behavior). A
        # caller opts into a small nonzero tolerance only after manually confirming
        # the remaining gap is a genuine, isolated data-capture limitation — not a
        # fetch bug — since this check has no way to tell the two apart on its own.
        # See the 2026-08-05 exit-policy finding: a real, permanent
        # cost_inputs_unavailable gap on one episode versus the four Bitget
        # episodes fixed the same day, which were a genuine fetch bug and were not
        # tolerated here — they were fixed at the source instead.
        return "insufficient_resolution"
    if (
        minimum_triggered_episodes is not None
        and least_triggered_count is not None
        and least_triggered_count < minimum_triggered_episodes
    ):
        # A "resolved" episode can be a zero_return_cash_episode (threshold never
        # crossed, no trade made). A family with a low trigger rate can reach every
        # gate above almost entirely on cash episodes while the informative,
        # actually-traded sample is still tiny — see the 2026-08-04 entry-floor
        # finding, where +35/+40/+50% each had exactly one triggered trade in the
        # locked 100-episode formal sample despite the sample itself being "ready".
        return "insufficient_triggers"
    return "formal_sample_ready"


def _observations(
    episodes: tuple[ChallengerEpisode, ...],
    getter: Callable[[ChallengerEpisode], float | None],
) -> tuple[ClusterObservation, ...]:
    values: list[ClusterObservation] = []
    for episode in episodes:
        value = getter(episode)
        if value is not None:
            values.append(ClusterObservation(episode.cluster_key, value))
    return tuple(values)


def _strategy_verdict(estimate: BootstrapEstimate) -> str:
    if estimate.upper_bound <= 0:
        return "no_go"
    if estimate.lower_bound > 0:
        return "evidence_of_edge"
    return "inconclusive"


def _challenger_return(
    episode: ChallengerEpisode,
    *,
    variant_key: str,
) -> float | None:
    return episode.challenger_return(variant_key)


def _paired_delta(
    episode: ChallengerEpisode,
    *,
    variant_key: str,
) -> float | None:
    challenger = episode.challenger_return(variant_key)
    if challenger is None or episode.baseline_return_pct is None:
        return None
    return challenger - episode.baseline_return_pct


def build_challenger_inference(
    episodes: tuple[ChallengerEpisode, ...],
    variant_keys: tuple[str, ...],
    *,
    settings: InferenceSettings = DEFAULT_INFERENCE_SETTINGS,
    inference_version: str = DEFAULT_INFERENCE_VERSION,
    minimum_triggered_episodes: int | None = None,
    max_unresolved_tolerance: int = 0,
    directional_episodes: int = DIRECTIONAL_EPISODES,
    formal_episodes: int = FORMAL_EPISODES,
    min_formal_clusters: int = MIN_FORMAL_CLUSTERS,
) -> ChallengerInference:
    """Evaluate episodes supplied in deterministic chronological cohort order.

    minimum_triggered_episodes requires every formal-sample episode to carry
    baseline_triggered/challenger_triggered (see ChallengerEpisode) and gates
    readiness on the least-triggered strategy in the family, not just on
    resolved-episode counts — a low-trigger-rate family (e.g. a rare entry
    threshold) can otherwise report "ready" on a sample that is almost entirely
    zero_return_cash_episode rows.

    max_unresolved_tolerance (default 0, unchanged strict behavior) allows the
    formal sample to proceed with up to this many unresolved episodes out of the
    locked window. Only raise it after manually confirming the gap is a genuine,
    isolated data-capture limitation, not a fetch bug that should be fixed at the
    source instead — this check cannot tell the two apart on its own.

    directional_episodes/formal_episodes/min_formal_clusters default to the
    shared replay.py constants every existing challenger family (oi_growth,
    the virtual_* entry/score/threshold challengers) already relies on —
    passing these is opt-in and changes nothing for a caller that omits
    them. A family whose own frozen pre-registration states a different,
    deliberately looser (or stricter) discovery-vs-confirmation bar — e.g.
    token_behavior_discovery_report.py's own >=60/>=20 discovery floor,
    distinct from oi_growth's >=100/>=30 confirmation floor — passes its own
    numbers explicitly instead of silently inheriting a floor designed for a
    different family's sample-size economics (added 2026-08-11).
    """
    normalized_version = inference_version.strip()
    if not normalized_version:
        raise ValueError("inference version must not be empty")
    if directional_episodes <= 0 or formal_episodes <= 0 or min_formal_clusters <= 0:
        raise ValueError("directional/formal episode and cluster floors must be positive")
    if directional_episodes > formal_episodes:
        raise ValueError("directional_episodes must not exceed formal_episodes")
    if not variant_keys or len(variant_keys) != len(set(variant_keys)):
        raise ValueError("registered variant keys must be unique and non-empty")
    if any(not key.strip() for key in variant_keys):
        raise ValueError("registered variant keys must be unique and non-empty")
    event_ids = [episode.pump_event_id for episode in episodes]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("inference episodes must have unique event ids")
    for episode in episodes:
        if tuple(key for key, _ in episode.challenger_returns_pct) != variant_keys:
            raise ValueError("episode challenger family does not match the manifest")

    formal_sample = episodes[:formal_episodes]
    cluster_counts = Counter(episode.cluster_key for episode in formal_sample)
    concentration = tuple(
        ClusterConcentration(
            cluster_key=cluster_key,
            episodes=count,
            share_pct=count / len(formal_sample) * 100 if formal_sample else 0.0,
        )
        for cluster_key, count in sorted(
            cluster_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    )
    baseline_resolved = sum(episode.baseline_return_pct is not None for episode in formal_sample)
    completely_paired = sum(
        episode.baseline_return_pct is not None
        and all(value is not None for _, value in episode.challenger_returns_pct)
        for episode in formal_sample
    )
    least_triggered_variant: str | None = None
    least_triggered_count: int | None = None
    if minimum_triggered_episodes is not None:
        missing = [
            episode.pump_event_id
            for episode in formal_sample
            if len(episode.challenger_triggered) != len(variant_keys)
        ]
        if missing:
            raise ValueError(
                "minimum_triggered_episodes requires challenger_triggered on every "
                f"formal-sample episode; missing on event ids {missing[:5]}"
            )
        trigger_counts = {"baseline": sum(ep.baseline_triggered for ep in formal_sample)}
        for variant_key in variant_keys:
            trigger_counts[variant_key] = sum(
                dict(ep.challenger_triggered)[variant_key] for ep in formal_sample
            )
        least_triggered_variant, least_triggered_count = min(
            trigger_counts.items(), key=lambda item: item[1]
        )
    readiness = InferenceReadiness(
        status=_readiness(
            eligible_episodes=len(episodes),
            formal_sample_clusters=len(cluster_counts),
            baseline_resolved=baseline_resolved,
            completely_paired=completely_paired,
            formal_sample_size=len(formal_sample),
            minimum_triggered_episodes=minimum_triggered_episodes,
            least_triggered_count=least_triggered_count,
            max_unresolved_tolerance=max_unresolved_tolerance,
            directional_episodes=directional_episodes,
            formal_episodes=formal_episodes,
            min_formal_clusters=min_formal_clusters,
        ),
        eligible_episodes=len(episodes),
        formal_sample_episodes=len(formal_sample),
        formal_sample_clusters=len(cluster_counts),
        baseline_resolved=baseline_resolved,
        completely_paired_episodes=completely_paired,
        minimum_triggered_episodes=minimum_triggered_episodes,
        least_triggered_variant=least_triggered_variant,
        least_triggered_count=least_triggered_count,
        max_unresolved_tolerance=max_unresolved_tolerance,
    )
    base = ChallengerInference(
        inference_version=normalized_version,
        bootstrap_version=CLUSTER_BOOTSTRAP_VERSION,
        holm_version=HOLM_CORRECTION_VERSION,
        seed_derivation=BOOTSTRAP_SEED_DERIVATION,
        settings=settings,
        readiness=readiness,
        formal_sample_event_ids=tuple(episode.pump_event_id for episode in formal_sample),
        cluster_concentration=concentration,
        baseline=None,
        challengers=(),
    )
    if readiness.status != "formal_sample_ready":
        return base

    top_clusters = tuple(row.cluster_key for row in concentration[:TOP_CLUSTER_SENSITIVITY_COUNT])
    baseline_observations = _observations(
        formal_sample,
        lambda episode: episode.baseline_return_pct,
    )
    baseline_computation = cluster_bootstrap_mean(
        baseline_observations,
        iterations=settings.iterations,
        seed=derived_seed(settings.seed, "baseline_expectancy"),
        confidence_level=settings.confidence_level,
    )
    baseline_sensitivity = leave_one_cluster_out_means(
        baseline_observations,
        top_clusters,
    )
    baseline = StrategyInference(
        strategy_key="baseline",
        estimate=baseline_computation.estimate,
        verdict=_strategy_verdict(baseline_computation.estimate),
        minimum_leave_one_cluster_out_pct=min(value for _, value in baseline_sensitivity),
        leave_one_cluster_out=baseline_sensitivity,
    )

    own_computations: dict[str, BootstrapComputation] = {}
    paired_computations: dict[str, BootstrapComputation] = {}
    paired_null_p_values: dict[str, float] = {}
    own_sensitivities: dict[str, tuple[tuple[str, float], ...]] = {}
    for variant_key in variant_keys:
        own = _observations(
            formal_sample,
            partial(_challenger_return, variant_key=variant_key),
        )
        paired_observations = _observations(
            formal_sample,
            partial(_paired_delta, variant_key=variant_key),
        )
        own_computations[variant_key] = cluster_bootstrap_mean(
            own,
            iterations=settings.iterations,
            seed=derived_seed(settings.seed, f"{variant_key}:expectancy"),
            confidence_level=settings.confidence_level,
        )
        paired_computations[variant_key] = cluster_bootstrap_mean(
            paired_observations,
            iterations=settings.iterations,
            seed=derived_seed(settings.seed, f"{variant_key}:paired_delta"),
            confidence_level=settings.confidence_level,
        )
        paired_null_p_values[variant_key] = cluster_bootstrap_mean_null_p_value(
            paired_observations,
            iterations=settings.iterations,
            seed=derived_seed(
                settings.seed,
                f"{variant_key}:paired_delta_null",
            ),
        )
        own_sensitivities[variant_key] = leave_one_cluster_out_means(
            own,
            top_clusters,
        )

    holm = {
        decision.key: decision
        for decision in holm_step_down(
            paired_null_p_values,
            family_alpha=settings.family_alpha,
        )
    }
    challengers: list[ChallengerFormalResult] = []
    for variant_key in variant_keys:
        own_computation = own_computations[variant_key]
        paired_computation = paired_computations[variant_key]
        sensitivity = own_sensitivities[variant_key]
        decision = holm[variant_key]
        # Holm controls the paired tests; Bonferroni supplies an intentionally
        # conservative simultaneous interval because Holm has no direct simple CI.
        familywise_confidence = 1 - settings.family_alpha / len(variant_keys)
        familywise_lower, familywise_upper = confidence_interval(
            paired_computation.samples,
            confidence_level=familywise_confidence,
        )
        strategy = StrategyInference(
            strategy_key=variant_key,
            estimate=own_computation.estimate,
            verdict=_strategy_verdict(own_computation.estimate),
            minimum_leave_one_cluster_out_pct=min(value for _, value in sensitivity),
            leave_one_cluster_out=sensitivity,
        )
        paired_result = PairedInference(
            variant_key=variant_key,
            estimate=paired_computation.estimate,
            holm_rank=decision.rank,
            raw_p_value=decision.raw_p_value,
            holm_adjusted_p_value=decision.adjusted_p_value,
            holm_critical_alpha=decision.critical_alpha,
            holm_rejected=decision.rejected,
            familywise_confidence_level=familywise_confidence,
            familywise_lower_bound=familywise_lower,
            familywise_upper_bound=familywise_upper,
        )
        if strategy.estimate.upper_bound <= 0:
            verdict = "no_go"
        elif (
            strategy.estimate.lower_bound > 0
            and paired_result.holm_rejected
            and paired_result.familywise_lower_bound > 0
            and strategy.minimum_leave_one_cluster_out_pct > 0
        ):
            verdict = "shadow_candidate"
        else:
            verdict = "inconclusive"
        challengers.append(
            ChallengerFormalResult(
                variant_key=variant_key,
                strategy=strategy,
                paired=paired_result,
                verdict=verdict,
            )
        )
    return ChallengerInference(
        inference_version=base.inference_version,
        bootstrap_version=base.bootstrap_version,
        holm_version=base.holm_version,
        seed_derivation=base.seed_derivation,
        settings=base.settings,
        readiness=base.readiness,
        formal_sample_event_ids=base.formal_sample_event_ids,
        cluster_concentration=base.cluster_concentration,
        baseline=baseline,
        challengers=tuple(challengers),
    )
