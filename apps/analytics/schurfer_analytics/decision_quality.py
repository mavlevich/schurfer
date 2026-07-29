"""Point-in-time policy selection for descriptive decision-quality research."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .replay import ReplayDecision, ReplayEpisode

DECISION_QUALITY_POLICY_VERSION = "decision_quality_policy_v1"
SCORE_THRESHOLD_FAMILY_VERSION = "score_threshold_downward_family_v1"
SCORE_COMPONENT_SCHEMA_VERSION = "pump_short_score_components_v1"
BASELINE_POLICY_KEY = "score_6"
RECORDED_OPEN_ACTIONS = frozenset({"opened", "opened_dry_run"})

SCORE_COMPONENTS = (
    "pump_age",
    "price_extent",
    "oi_trend",
    "funding_rate",
    "retrace_from_peak",
)


@dataclass(frozen=True)
class ScorePolicy:
    key: str
    min_score: int
    omitted_component: str | None = None

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("score policy key must not be empty")
        if not 0 <= self.min_score <= 10:
            raise ValueError("score policy threshold must be between zero and ten")
        if self.omitted_component is not None and self.omitted_component not in SCORE_COMPONENTS:
            raise ValueError("unknown omitted score component")


MARKET_QUALITY_CONTROL_POLICY = ScorePolicy("score_any", 0)
SCORE_POLICIES = (
    MARKET_QUALITY_CONTROL_POLICY,
    *(ScorePolicy(f"score_{score}", score) for score in range(4, 10)),
    *(ScorePolicy(f"score_6_without_{component}", 6, component) for component in SCORE_COMPONENTS),
)
SCORE_THRESHOLD_BASELINE_POLICY = ScorePolicy(BASELINE_POLICY_KEY, 6)
SCORE_THRESHOLD_CHALLENGER_POLICIES = (
    ScorePolicy("score_4", 4),
    ScorePolicy("score_5", 5),
)
SCORE_THRESHOLD_POLICIES = (
    SCORE_THRESHOLD_BASELINE_POLICY,
    *SCORE_THRESHOLD_CHALLENGER_POLICIES,
)

SelectionStatus = Literal["selected", "not_triggered", "unresolved"]


@dataclass(frozen=True)
class ComponentSnapshot:
    name: str
    points: int
    maximum: int
    value: float
    data_available: bool | None


@dataclass(frozen=True)
class ScoreSelection:
    policy_key: str
    status: SelectionStatus
    decision: ReplayDecision | None
    effective_score: int | None
    components: tuple[ComponentSnapshot, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status == "selected" and self.decision is None:
            raise ValueError("selected policy requires a decision")
        if self.status != "selected" and self.decision is not None:
            raise ValueError("non-selected policy must not carry a decision")
        if self.status == "unresolved" and not self.error:
            raise ValueError("unresolved policy requires an error")


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def component_snapshot(
    decision: ReplayDecision,
) -> tuple[tuple[ComponentSnapshot, ...] | None, str | None]:
    features = decision.features
    if not isinstance(features, dict):
        return None, "missing_features"
    signal = features.get("signal")
    if not isinstance(signal, dict):
        return None, "missing_signal"
    raw_components = signal.get("components")
    if not isinstance(raw_components, dict):
        return None, "missing_score_components"
    raw_data_quality = signal.get("data_quality")

    snapshots: list[ComponentSnapshot] = []
    for name in SCORE_COMPONENTS:
        raw = raw_components.get(name)
        if not isinstance(raw, dict):
            return None, f"missing_component:{name}"
        points = raw.get("points")
        maximum = raw.get("max")
        value = _finite_number(raw.get("value"))
        if (
            isinstance(points, bool)
            or not isinstance(points, int)
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or not 0 <= points <= maximum
            or maximum <= 0
            or value is None
        ):
            return None, f"invalid_component:{name}"
        data_available: bool | None = True
        quality_key = {
            "oi_trend": "oi",
            "funding_rate": "funding",
        }.get(name)
        if quality_key is not None:
            if not isinstance(raw_data_quality, dict):
                data_available = None
            else:
                raw_available = raw_data_quality.get(quality_key)
                data_available = raw_available if isinstance(raw_available, bool) else None
        snapshots.append(ComponentSnapshot(name, points, maximum, value, data_available))

    if decision.score is None or isinstance(decision.score, bool):
        return None, "missing_score"
    if sum(item.points for item in snapshots) != decision.score:
        return None, "score_component_sum_mismatch"
    return tuple(snapshots), None


def _market_quality_result(decision: ReplayDecision) -> tuple[bool | None, str | None]:
    features = decision.features
    if not isinstance(features, dict):
        return None, "missing_features"
    config = features.get("config")
    if not isinstance(config, dict):
        return None, "missing_config"
    required = config.get("require_market_quality")
    if not isinstance(required, bool):
        return None, "invalid_require_market_quality"
    if not required:
        return True, None

    liquidity = decision.liquidity
    if not isinstance(liquidity, dict):
        return None, "missing_liquidity"
    quality = liquidity.get("quality")
    if not isinstance(quality, dict):
        return None, "missing_market_quality"
    allowed = quality.get("allowed")
    if not isinstance(allowed, bool):
        return None, "invalid_market_quality_allowed"
    return allowed, None


def select_score_policy(
    episode: ReplayEpisode,
    policy: ScorePolicy,
) -> ScoreSelection:
    """Select the first recorded decision that passes one descriptive score policy."""
    for decision in episode.decisions:
        if (
            decision.score is None
            or isinstance(decision.score, bool)
            or not 0 <= decision.score <= 10
        ):
            return ScoreSelection(policy.key, "unresolved", None, None, error="invalid_score")

        components: tuple[ComponentSnapshot, ...] = ()
        effective_score = decision.score
        if policy.omitted_component is not None:
            parsed, error = component_snapshot(decision)
            if error is not None or parsed is None:
                return ScoreSelection(
                    policy.key,
                    "unresolved",
                    None,
                    None,
                    error=error or "invalid_score_components",
                )
            components = parsed
            omitted = next(item for item in components if item.name == policy.omitted_component)
            effective_score -= omitted.points

        if effective_score < policy.min_score:
            if decision.action in RECORDED_OPEN_ACTIONS:
                return ScoreSelection(
                    policy.key,
                    "unresolved",
                    None,
                    None,
                    error="right_censored_after_recorded_open",
                )
            continue
        allowed, error = _market_quality_result(decision)
        if error is not None:
            return ScoreSelection(policy.key, "unresolved", None, None, error=error)
        if not allowed:
            if decision.action in RECORDED_OPEN_ACTIONS:
                return ScoreSelection(
                    policy.key,
                    "unresolved",
                    None,
                    None,
                    error="recorded_open_failed_reconstructed_market_quality",
                )
            continue
        if not components:
            parsed, _ = component_snapshot(decision)
            components = parsed or ()
        return ScoreSelection(
            policy.key,
            "selected",
            decision,
            effective_score,
            components,
        )
    return ScoreSelection(policy.key, "not_triggered", None, None)


def selected_policy_decisions(
    episodes: tuple[ReplayEpisode, ...],
    policies: tuple[ScorePolicy, ...] = SCORE_POLICIES,
) -> tuple[ReplayDecision, ...]:
    """Return the deterministic unique decision union required by the policies."""
    by_id: dict[str, ReplayDecision] = {}
    for episode in episodes:
        for policy in policies:
            decision = select_score_policy(episode, policy).decision
            if decision is None:
                continue
            if not decision.decision_id:
                raise ValueError("selected score-policy decision is missing its id")
            by_id.setdefault(decision.decision_id, decision)
    return tuple(sorted(by_id.values(), key=lambda item: (item.ts, item.row_id)))
