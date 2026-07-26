"""Pure episode-replay input validation and grouping.

This module deliberately contains no strategy logic. It turns persisted point-in-time
decisions into deterministic episode paths and fails closed when a path is incomplete.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from .outcomes import RESOLVER_VERSION

if TYPE_CHECKING:
    from datetime import datetime

DEFAULT_REPLAY_HORIZONS = (480,)
FOUNDATION_VERSION = "episode_replay_foundation_v2"
QUERY_VERSION = "replay_inputs_v1"
FORMAL_EPISODES = 100
MIN_FORMAL_CLUSTERS = 30
DIRECTIONAL_EPISODES = 50


@dataclass(frozen=True)
class ReplayFilters:
    until: datetime
    since: datetime | None = None
    strategy_versions: tuple[str, ...] = ("pump_short_v1_market_quality",)
    resolver_version: str = RESOLVER_VERSION
    required_horizons: tuple[int, ...] = DEFAULT_REPLAY_HORIZONS
    allow_fallback: bool = False

    def __post_init__(self) -> None:
        if self.until.utcoffset() is None:
            raise ValueError("until must be timezone-aware")
        if self.since is not None:
            if self.since.utcoffset() is None:
                raise ValueError("since must be timezone-aware")
            if self.since >= self.until:
                raise ValueError("since must be earlier than until")
        strategies = tuple(
            dict.fromkeys(
                normalized for item in self.strategy_versions if (normalized := item.strip())
            )
        )
        if not strategies:
            raise ValueError("at least one strategy version is required")
        horizons = tuple(sorted(set(self.required_horizons)))
        if not horizons or any(horizon <= 0 for horizon in horizons):
            raise ValueError("required horizons must be positive")
        if not self.resolver_version.strip():
            raise ValueError("resolver version must not be empty")
        object.__setattr__(self, "strategy_versions", strategies)
        object.__setattr__(self, "required_horizons", horizons)

    @property
    def accepted_outcome_statuses(self) -> tuple[str, ...]:
        if self.allow_fallback:
            return ("complete", "complete_fallback")
        return ("complete",)


@dataclass(frozen=True)
class ReplayOutcome:
    horizon_minutes: int
    status: str
    anchor_exchange: str | None
    source_exchange: str | None
    entry_price: float | None
    forward_price: float | None
    mfe_pct: float | None
    mae_pct: float | None
    short_return_pct: float | None
    coverage_ratio: float | None


@dataclass(frozen=True)
class ReplayDecision:
    row_id: int
    decision_id: str | None
    pump_event_id: int | None
    event_base: str | None
    event_first_seen_at: datetime | None
    event_closed_at: datetime | None
    ts: datetime
    base: str
    exchange: str
    action: str
    reason: str
    score: int | None
    pump_pct: float | None
    price: float | None
    strategy_version: str | None
    features: dict[str, Any] | None
    liquidity: dict[str, Any] | None
    outcomes: tuple[ReplayOutcome, ...]


@dataclass(frozen=True)
class ReplayEpisode:
    pump_event_id: int
    base: str
    cluster_key: str
    decisions: tuple[ReplayDecision, ...]
    exclusion_reasons: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return not self.exclusion_reasons

    @property
    def first_decision_at(self) -> datetime:
        return self.decisions[0].ts


@dataclass(frozen=True)
class ReplayDataset:
    decisions: tuple[ReplayDecision, ...]
    episodes: tuple[ReplayEpisode, ...]
    unassigned_decisions: tuple[ReplayDecision, ...]
    unassigned_reasons: tuple[tuple[int, tuple[str, ...]], ...]
    input_fingerprint: str

    @property
    def eligible_episodes(self) -> tuple[ReplayEpisode, ...]:
        return tuple(episode for episode in self.episodes if episode.eligible)

    @property
    def excluded_episodes(self) -> tuple[ReplayEpisode, ...]:
        return tuple(episode for episode in self.episodes if not episode.eligible)


def normalize_cluster_key(base: str) -> str:
    """Use a conservative ticker cluster until reviewed canonical links exist."""
    return f"base:{base.strip().upper()}"


def _usable_price(price: float | None) -> bool:
    return price is not None and math.isfinite(price) and price > 0


def _signal_timestamp_reason(
    features: dict[str, Any],
    decision_at: datetime,
) -> str | None:
    signal = features.get("signal")
    if not isinstance(signal, dict):
        return "missing_signal"
    computed_at = signal.get("computed_at")
    if (
        isinstance(computed_at, bool)
        or not isinstance(computed_at, int | float)
        or not math.isfinite(computed_at)
        or computed_at <= 0
    ):
        return "invalid_signal_timestamp"
    if computed_at > decision_at.timestamp() + 5:
        return "signal_after_decision"
    return None


def decision_exclusion_reasons(
    decision: ReplayDecision,
    filters: ReplayFilters,
    *,
    duplicate_decision_ids: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    reasons: set[str] = set()
    if not decision.decision_id:
        reasons.add("missing_decision_id")
    elif decision.decision_id in duplicate_decision_ids:
        reasons.add("duplicate_decision_id")
    if decision.pump_event_id is None or decision.pump_event_id <= 0:
        reasons.add("missing_pump_event_id")
    if decision.event_base is None:
        reasons.add("missing_pump_event")
    else:
        if decision.event_base.casefold() != decision.base.casefold():
            reasons.add("episode_base_mismatch")
        if decision.event_first_seen_at is None:
            reasons.add("missing_episode_start")
        elif filters.since is not None and decision.event_first_seen_at < filters.since:
            reasons.add("left_censored_episode")
        if decision.event_closed_at is None or decision.event_closed_at >= filters.until:
            reasons.add("right_censored_episode")
    if not decision.strategy_version:
        reasons.add("missing_strategy_version")
    elif decision.strategy_version not in filters.strategy_versions:
        reasons.add("mixed_strategy_episode")
    if not decision.exchange.strip():
        reasons.add("missing_exchange")
    if not _usable_price(decision.price):
        reasons.add("missing_price")
    if not isinstance(decision.features, dict):
        reasons.add("missing_features")
    else:
        if not isinstance(decision.features.get("config"), dict):
            reasons.add("missing_config")
        signal_reason = _signal_timestamp_reason(decision.features, decision.ts)
        if signal_reason:
            reasons.add(signal_reason)
    if not isinstance(decision.liquidity, dict):
        reasons.add("missing_liquidity")

    outcomes_by_horizon: dict[int, ReplayOutcome] = {}
    duplicate_horizons: set[int] = set()
    for outcome in decision.outcomes:
        if outcome.horizon_minutes in outcomes_by_horizon:
            duplicate_horizons.add(outcome.horizon_minutes)
        outcomes_by_horizon[outcome.horizon_minutes] = outcome
    for horizon in duplicate_horizons:
        reasons.add(f"duplicate_outcome:{horizon}")
    for horizon in filters.required_horizons:
        required_outcome = outcomes_by_horizon.get(horizon)
        if required_outcome is None:
            reasons.add(f"missing_outcome:{horizon}")
        elif required_outcome.status not in filters.accepted_outcome_statuses:
            reasons.add(f"outcome_status:{horizon}:{required_outcome.status}")
    return tuple(sorted(reasons))


def _fingerprint(decisions: tuple[ReplayDecision, ...]) -> str:
    payload = []
    for decision in decisions:
        row = asdict(decision)
        row["ts"] = decision.ts.isoformat()
        payload.append(row)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_replay_dataset(
    decisions: list[ReplayDecision],
    filters: ReplayFilters,
) -> ReplayDataset:
    candidate_event_ids = {
        row.pump_event_id
        for row in decisions
        if row.pump_event_id is not None
        and row.pump_event_id > 0
        and row.strategy_version in filters.strategy_versions
    }
    scoped = (
        row
        for row in decisions
        if row.pump_event_id in candidate_event_ids
        or (
            (row.pump_event_id is None or row.pump_event_id <= 0)
            and row.strategy_version in filters.strategy_versions
        )
    )
    ordered = tuple(sorted(scoped, key=lambda row: (row.ts, row.row_id)))
    id_counts = Counter(row.decision_id for row in ordered if row.decision_id)
    duplicate_ids = frozenset(key for key, count in id_counts.items() if count > 1)

    grouped: dict[int, list[ReplayDecision]] = {}
    unassigned: list[ReplayDecision] = []
    unassigned_reasons: list[tuple[int, tuple[str, ...]]] = []
    for decision in ordered:
        if decision.pump_event_id is None or decision.pump_event_id <= 0:
            unassigned.append(decision)
            unassigned_reasons.append(
                (
                    decision.row_id,
                    decision_exclusion_reasons(
                        decision,
                        filters,
                        duplicate_decision_ids=duplicate_ids,
                    ),
                )
            )
            continue
        grouped.setdefault(decision.pump_event_id, []).append(decision)

    episodes: list[ReplayEpisode] = []
    for event_id, event_decisions in grouped.items():
        chronological = tuple(sorted(event_decisions, key=lambda row: (row.ts, row.row_id)))
        reasons: set[str] = set()
        event_bases = {row.event_base.casefold() for row in chronological if row.event_base}
        if len(event_bases) > 1:
            reasons.add("inconsistent_event_base")
        for decision in chronological:
            reasons.update(
                decision_exclusion_reasons(
                    decision,
                    filters,
                    duplicate_decision_ids=duplicate_ids,
                )
            )
        base = next(
            (row.event_base for row in chronological if row.event_base),
            chronological[0].base,
        )
        episodes.append(
            ReplayEpisode(
                pump_event_id=event_id,
                base=base,
                cluster_key=normalize_cluster_key(base),
                decisions=chronological,
                exclusion_reasons=tuple(sorted(reasons)),
            )
        )
    episodes.sort(key=lambda episode: (episode.first_decision_at, episode.pump_event_id))
    return ReplayDataset(
        decisions=ordered,
        episodes=tuple(episodes),
        unassigned_decisions=tuple(unassigned),
        unassigned_reasons=tuple(unassigned_reasons),
        input_fingerprint=_fingerprint(ordered),
    )
