"""Point-in-time decision selection for the pre-registered entry-floor family."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .replay import ReplayDecision, ReplayEpisode

ENTRY_THRESHOLD_FAMILY_VERSION = "entry_threshold_family_v1"
ENTRY_THRESHOLD_SELECTION_VERSION = "first_recorded_gate_eligible_crossing_v1"
ENTRY_THRESHOLD_COHORT_START = datetime(2026, 7, 27, 7, tzinfo=UTC)
BASELINE_ENTRY_FLOOR_PCT = 30.0


@dataclass(frozen=True)
class EntryThresholdVariant:
    key: str
    version: str
    min_pump_pct: float

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.version.strip():
            raise ValueError("threshold variant key and version must not be empty")
        if not math.isfinite(self.min_pump_pct) or self.min_pump_pct <= 0:
            raise ValueError("entry threshold must be finite and positive")


ENTRY_THRESHOLD_VARIANTS = (
    EntryThresholdVariant("floor_20", "entry_floor_20_v1", 20.0),
    EntryThresholdVariant("floor_25", "entry_floor_25_v1", 25.0),
    EntryThresholdVariant("floor_35", "entry_floor_35_v1", 35.0),
    EntryThresholdVariant("floor_40", "entry_floor_40_v1", 40.0),
    EntryThresholdVariant("floor_50", "entry_floor_50_v1", 50.0),
)

SelectionStatus = Literal["selected", "not_triggered", "unresolved"]


@dataclass(frozen=True)
class ThresholdSelection:
    min_pump_pct: float
    status: SelectionStatus
    decision: ReplayDecision | None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status == "selected" and self.decision is None:
            raise ValueError("selected threshold requires a decision")
        if self.status != "selected" and self.decision is not None:
            raise ValueError("non-selected threshold must not carry a decision")
        if self.status == "unresolved" and not self.error:
            raise ValueError("unresolved threshold requires an error")


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _recorded_gate_result(decision: ReplayDecision) -> tuple[bool | None, str | None]:
    """Reconstruct only the gates persisted for every measurement-only decision."""
    if decision.score is None or isinstance(decision.score, bool):
        return None, "missing_score"
    features = decision.features
    if not isinstance(features, dict):
        return None, "missing_features"
    config = features.get("config")
    if not isinstance(config, dict):
        return None, "missing_config"
    score_threshold = _finite_number(config.get("score_threshold"))
    if score_threshold is None:
        return None, "invalid_score_threshold"
    if decision.score < score_threshold:
        return False, None

    require_market_quality = config.get("require_market_quality")
    if not isinstance(require_market_quality, bool):
        return None, "invalid_require_market_quality"
    if not require_market_quality:
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


def select_threshold_decision(
    episode: ReplayEpisode,
    min_pump_pct: float,
) -> ThresholdSelection:
    """Select the first recorded crossing that passes reconstructable live gates."""
    if not math.isfinite(min_pump_pct) or min_pump_pct <= 0:
        raise ValueError("entry threshold must be finite and positive")
    for decision in episode.decisions:
        pump_pct = _finite_number(decision.pump_pct)
        if pump_pct is None:
            return ThresholdSelection(
                min_pump_pct,
                "unresolved",
                None,
                "invalid_pump_pct",
            )
        if pump_pct < min_pump_pct:
            continue
        allowed, error = _recorded_gate_result(decision)
        if error is not None:
            return ThresholdSelection(min_pump_pct, "unresolved", None, error)
        if allowed:
            return ThresholdSelection(min_pump_pct, "selected", decision)
    return ThresholdSelection(min_pump_pct, "not_triggered", None)


def registered_thresholds() -> tuple[float, ...]:
    return (
        BASELINE_ENTRY_FLOOR_PCT,
        *(variant.min_pump_pct for variant in ENTRY_THRESHOLD_VARIANTS),
    )


def selected_threshold_decisions(
    episodes: tuple[ReplayEpisode, ...],
) -> tuple[ReplayDecision, ...]:
    """Return the deterministic unique union needed by all registered floors."""
    by_id: dict[str, ReplayDecision] = {}
    for episode in episodes:
        for threshold in registered_thresholds():
            selection = select_threshold_decision(episode, threshold)
            decision = selection.decision
            if decision is None:
                continue
            if not decision.decision_id:
                raise ValueError("selected threshold decision is missing its id")
            by_id.setdefault(decision.decision_id, decision)
    return tuple(sorted(by_id.values(), key=lambda item: (item.ts, item.row_id)))
