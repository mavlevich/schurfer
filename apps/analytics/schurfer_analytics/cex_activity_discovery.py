"""Pure discovery logic for CEX activity episodes and +25% price moves.

This module deliberately separates three things that the historical
``research/pump-analytics`` scripts mixed together:

* a point-in-time signal (a directional five-minute notional burst);
* a market outcome (the exact native market moves 25% after the signal); and
* a matched comparison (the same instrument and UTC time on a quiet day).

The result is discovery evidence only.  OHLCV bar opens/highs/lows are exact-
venue market data, but they are not executable bid/ask quotes and therefore do
not justify paper or live trading by themselves.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from statistics import fmean, median

from .clustered_inference import (
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_BOOTSTRAP_SEED,
    DEFAULT_CONFIDENCE_LEVEL,
    BootstrapEstimate,
    ClusterObservation,
    cluster_bootstrap_mean,
    cluster_bootstrap_mean_null_p_value,
    derived_seed,
    holm_step_down,
)

PRIMARY_MOVE_PCT = 25.0
OUTCOME_HORIZON_MINUTES = 24 * 60
CONTROL_QUIET_HOURS = 24
CONTROL_SEARCH_DAYS = 7
DISCOVERY_MIN_PAIRS = 60
DISCOVERY_MIN_CLUSTERS = 20
DISCOVERY_MIN_WEEKS = 2
REGISTERED_DIRECTIONS = ("buy", "sell")


@dataclass(frozen=True)
class PathRequest:
    request_id: str
    symbol: str
    trigger_at: datetime
    entry_at: datetime

    def __post_init__(self) -> None:
        if not self.request_id.strip() or not self.symbol.strip():
            raise ValueError("path request id and symbol must not be empty")
        if self.entry_at <= self.trigger_at:
            raise ValueError("path entry must be strictly after the point-in-time signal")


@dataclass(frozen=True)
class OutcomeSignalEpisode:
    episode_id: int
    signal_id: str
    source: str
    exchange: str
    symbol: str
    direction: str
    trigger_at: datetime
    entry_at: datetime
    signal_value: float | None

    def __post_init__(self) -> None:
        if self.episode_id <= 0:
            raise ValueError("episode id must be positive")
        if (
            not self.signal_id.strip()
            or not self.source.strip()
            or not self.exchange.strip()
            or not self.symbol.strip()
        ):
            raise ValueError("episode identity fields must not be empty")
        if self.direction not in REGISTERED_DIRECTIONS:
            raise ValueError(f"unsupported episode direction: {self.direction}")
        if self.entry_at <= self.trigger_at:
            raise ValueError("episode entry must be strictly after its point-in-time signal")

    @property
    def cluster_key(self) -> str:
        return self.symbol


@dataclass(frozen=True)
class ExactPricePath:
    request_id: str
    symbol: str
    trigger_at: datetime
    entry_at: datetime
    entry_price: float | None
    observed_minutes: int
    max_high: float | None
    min_low: float | None
    first_up_25_at: datetime | None
    first_down_25_at: datetime | None

    @property
    def resolved(self) -> bool:
        return (
            self.entry_price is not None
            and math.isfinite(self.entry_price)
            and self.entry_price > 0
            and self.observed_minutes == OUTCOME_HORIZON_MINUTES
            and self.max_high is not None
            and self.min_low is not None
            and math.isfinite(self.max_high)
            and math.isfinite(self.min_low)
            and self.max_high > 0
            and self.min_low > 0
        )


@dataclass(frozen=True)
class MatchedMovePair:
    episode: OutcomeSignalEpisode
    signal_path: ExactPricePath
    control_path: ExactPricePath


@dataclass(frozen=True)
class DirectionMoveResult:
    direction: str
    eligible_episodes: int
    resolved_signal_paths: int
    paired_episodes: int
    clusters: int
    utc_weeks: int
    signal_hit_rate_pct: float | None
    control_hit_rate_pct: float | None
    paired_hit_rate_delta_pct: float | None
    signal_median_favorable_move_pct: float | None
    control_median_favorable_move_pct: float | None
    signal_median_adverse_move_pct: float | None
    median_time_to_hit_minutes: float | None
    readiness: str
    estimate: BootstrapEstimate | None
    raw_p_value: float | None
    holm_adjusted_p_value: float | None
    holm_rejected: bool | None
    verdict: str


@dataclass(frozen=True)
class _DirectionWorking:
    eligible: int
    resolved_signals: int
    pairs: tuple[MatchedMovePair, ...]
    clusters: int
    weeks: int
    readiness: str
    estimate: BootstrapEstimate | None
    raw_p_value: float | None
    signal_hit_rate_pct: float | None
    control_hit_rate_pct: float | None


def signal_request(episode: OutcomeSignalEpisode) -> PathRequest:
    return PathRequest(
        request_id=f"signal:{episode.episode_id}",
        symbol=episode.symbol,
        trigger_at=episode.trigger_at,
        entry_at=episode.entry_at,
    )


def _control_request_id(episode_id: int, offset_days: int) -> str:
    sign = "p" if offset_days > 0 else "m"
    return f"control:{episode_id}:{sign}{abs(offset_days)}"


def _utc_week(value: datetime) -> str:
    iso = value.isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def build_control_requests(
    episodes: tuple[OutcomeSignalEpisode, ...],
    *,
    since: datetime,
    until: datetime,
    search_days: int = CONTROL_SEARCH_DAYS,
    quiet_hours: int = CONTROL_QUIET_HOURS,
) -> dict[int, tuple[PathRequest, ...]]:
    """Build deterministic same-symbol/same-UTC-time quiet controls.

    Candidate order is nearest past day, nearest future day, then expands
    outward.  A candidate within ``quiet_hours`` of *any* burst direction for
    the same exact instrument is excluded.  This is an event-study control,
    not a live feature: knowing that the control stayed away from another
    event is used only to keep the baseline uncontaminated.
    """
    if since >= until:
        raise ValueError("since must be earlier than until")
    if search_days <= 0 or quiet_hours <= 0:
        raise ValueError("control search days and quiet hours must be positive")

    episode_times: dict[tuple[str, str], tuple[datetime, ...]] = {}
    grouped: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for episode in episodes:
        grouped[(episode.exchange, episode.symbol)].append(episode.trigger_at)
    for key, values in grouped.items():
        episode_times[key] = tuple(sorted(values))

    quiet = timedelta(hours=quiet_hours)
    offsets = tuple(offset for day in range(1, search_days + 1) for offset in (-day, day))
    result: dict[int, tuple[PathRequest, ...]] = {}
    for episode in episodes:
        requests: list[PathRequest] = []
        for offset in offsets:
            candidate_at = episode.trigger_at + timedelta(days=offset)
            if candidate_at < since or candidate_at >= until:
                continue
            if any(
                abs(candidate_at - other_at) <= quiet
                for other_at in episode_times[(episode.exchange, episode.symbol)]
            ):
                continue
            requests.append(
                PathRequest(
                    request_id=_control_request_id(episode.episode_id, offset),
                    symbol=episode.symbol,
                    trigger_at=candidate_at,
                    entry_at=episode.entry_at + timedelta(days=offset),
                )
            )
        result[episode.episode_id] = tuple(requests)
    return result


def select_matched_pairs(
    episodes: tuple[OutcomeSignalEpisode, ...],
    *,
    signal_paths: dict[str, ExactPricePath],
    control_requests: dict[int, tuple[PathRequest, ...]],
    control_paths: dict[str, ExactPricePath],
) -> tuple[MatchedMovePair, ...]:
    """Select the first resolved quiet control without reusing a control."""
    used_controls: set[tuple[str, datetime]] = set()
    pairs: list[MatchedMovePair] = []
    for episode in sorted(episodes, key=lambda item: (item.trigger_at, item.episode_id)):
        signal = signal_paths.get(signal_request(episode).request_id)
        if signal is None or not signal.resolved:
            continue
        selected: ExactPricePath | None = None
        for request in control_requests.get(episode.episode_id, ()):
            path = control_paths.get(request.request_id)
            key = (request.symbol, request.trigger_at)
            if path is not None and path.resolved and key not in used_controls:
                selected = path
                used_controls.add(key)
                break
        if selected is not None:
            pairs.append(MatchedMovePair(episode, signal, selected))
    return tuple(pairs)


def favorable_move_pct(path: ExactPricePath, direction: str) -> float:
    if not path.resolved:
        raise ValueError("favorable move requires a resolved path")
    assert path.entry_price is not None and path.max_high is not None and path.min_low is not None
    if direction == "buy":
        return (path.max_high / path.entry_price - 1) * 100
    if direction == "sell":
        return (1 - path.min_low / path.entry_price) * 100
    raise ValueError(f"unsupported direction: {direction}")


def adverse_move_pct(path: ExactPricePath, direction: str) -> float:
    if not path.resolved:
        raise ValueError("adverse move requires a resolved path")
    assert path.entry_price is not None and path.max_high is not None and path.min_low is not None
    if direction == "buy":
        return (1 - path.min_low / path.entry_price) * 100
    if direction == "sell":
        return (path.max_high / path.entry_price - 1) * 100
    raise ValueError(f"unsupported direction: {direction}")


def hit_target(path: ExactPricePath, direction: str) -> bool:
    if direction == "buy":
        return path.first_up_25_at is not None
    if direction == "sell":
        return path.first_down_25_at is not None
    raise ValueError(f"unsupported direction: {direction}")


def time_to_hit_minutes(path: ExactPricePath, direction: str) -> float | None:
    hit_at = path.first_up_25_at if direction == "buy" else path.first_down_25_at
    return (hit_at - path.entry_at).total_seconds() / 60 if hit_at is not None else None


def _median(values: list[float]) -> float | None:
    return median(values) if values else None


def build_direction_results(
    episodes: tuple[OutcomeSignalEpisode, ...],
    pairs: tuple[MatchedMovePair, ...],
    signal_paths: dict[str, ExactPricePath],
    *,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    registered_directions: tuple[str, ...] = REGISTERED_DIRECTIONS,
) -> tuple[DirectionMoveResult, ...]:
    """Evaluate the two pre-registered directional candidates jointly.

    The primary statistic is the paired difference in +25%-within-24h hit
    indicators.  Whole-symbol bootstrap keeps repeated episodes of one token
    together, and Holm correction covers the registered buy/sell family.
    """
    if not registered_directions or len(set(registered_directions)) != len(registered_directions):
        raise ValueError("registered directions must be a non-empty unique family")
    if any(direction not in REGISTERED_DIRECTIONS for direction in registered_directions):
        raise ValueError("registered directions contain an unsupported direction")

    provisional: dict[str, _DirectionWorking] = {}
    p_values: dict[str, float] = {}
    for direction in registered_directions:
        direction_episodes = tuple(item for item in episodes if item.direction == direction)
        direction_pairs = tuple(item for item in pairs if item.episode.direction == direction)
        resolved_signals = sum(
            1
            for episode in direction_episodes
            if (
                (path := signal_paths.get(signal_request(episode).request_id)) is not None
                and path.resolved
            )
        )
        observations = tuple(
            ClusterObservation(
                cluster_key=pair.episode.cluster_key,
                value=float(hit_target(pair.signal_path, direction))
                - float(hit_target(pair.control_path, direction)),
            )
            for pair in direction_pairs
        )
        clusters = len({pair.episode.cluster_key for pair in direction_pairs})
        weeks = len({_utc_week(pair.episode.trigger_at) for pair in direction_pairs})
        if len(direction_pairs) < DISCOVERY_MIN_PAIRS:
            readiness = "collecting"
        elif clusters < DISCOVERY_MIN_CLUSTERS:
            readiness = "insufficient_diversity"
        elif weeks < DISCOVERY_MIN_WEEKS:
            readiness = "insufficient_weeks"
        else:
            readiness = "discovery_ready"

        estimate: BootstrapEstimate | None = None
        raw_p_value: float | None = None
        if readiness == "discovery_ready" and observations and clusters >= 2:
            estimate = cluster_bootstrap_mean(
                observations,
                iterations=bootstrap_iterations,
                seed=derived_seed(DEFAULT_BOOTSTRAP_SEED, f"cex_activity:{direction}"),
                confidence_level=DEFAULT_CONFIDENCE_LEVEL,
            ).estimate
            raw_p_value = cluster_bootstrap_mean_null_p_value(
                observations,
                iterations=bootstrap_iterations,
                seed=derived_seed(DEFAULT_BOOTSTRAP_SEED, f"cex_activity_null:{direction}"),
            )
            p_values[direction] = raw_p_value

        signal_hits = [hit_target(pair.signal_path, direction) for pair in direction_pairs]
        control_hits = [hit_target(pair.control_path, direction) for pair in direction_pairs]
        provisional[direction] = _DirectionWorking(
            eligible=len(direction_episodes),
            resolved_signals=resolved_signals,
            pairs=direction_pairs,
            clusters=clusters,
            weeks=weeks,
            readiness=readiness,
            estimate=estimate,
            raw_p_value=raw_p_value,
            signal_hit_rate_pct=fmean(signal_hits) * 100 if signal_hits else None,
            control_hit_rate_pct=fmean(control_hits) * 100 if control_hits else None,
        )

    # Keep the registered two-direction family intact even when one direction
    # is unresolved; silently correcting only the surviving direction would
    # reward missing data with a weaker multiplicity penalty.
    family_p_values = (
        {direction: p_values.get(direction, 1.0) for direction in registered_directions}
        if p_values
        else {}
    )
    holm = {row.key: row for row in holm_step_down(family_p_values)} if p_values else {}
    results: list[DirectionMoveResult] = []
    for direction in registered_directions:
        item = provisional[direction]
        direction_pairs = item.pairs
        estimate = item.estimate
        decision = holm.get(direction)
        readiness = item.readiness
        if readiness != "discovery_ready" or decision is None or estimate is None:
            verdict = "insufficient_data" if readiness != "discovery_ready" else "inconclusive"
        elif decision.rejected and estimate.lower_bound > 0:
            verdict = "forward_candidate"
        elif estimate.upper_bound <= 0:
            verdict = "no_evidence"
        else:
            verdict = "inconclusive"

        signal_moves = [favorable_move_pct(pair.signal_path, direction) for pair in direction_pairs]
        control_moves = [
            favorable_move_pct(pair.control_path, direction) for pair in direction_pairs
        ]
        adverse_moves = [adverse_move_pct(pair.signal_path, direction) for pair in direction_pairs]
        hit_times = [
            value
            for pair in direction_pairs
            if (value := time_to_hit_minutes(pair.signal_path, direction)) is not None
        ]
        signal_hit_rate = item.signal_hit_rate_pct
        control_hit_rate = item.control_hit_rate_pct
        results.append(
            DirectionMoveResult(
                direction=direction,
                eligible_episodes=item.eligible,
                resolved_signal_paths=item.resolved_signals,
                paired_episodes=len(direction_pairs),
                clusters=item.clusters,
                utc_weeks=item.weeks,
                signal_hit_rate_pct=signal_hit_rate,
                control_hit_rate_pct=control_hit_rate,
                paired_hit_rate_delta_pct=(
                    signal_hit_rate - control_hit_rate
                    if signal_hit_rate is not None and control_hit_rate is not None
                    else None
                ),
                signal_median_favorable_move_pct=_median(signal_moves),
                control_median_favorable_move_pct=_median(control_moves),
                signal_median_adverse_move_pct=_median(adverse_moves),
                median_time_to_hit_minutes=_median(hit_times),
                readiness=readiness,
                estimate=estimate,
                raw_p_value=item.raw_p_value,
                holm_adjusted_p_value=decision.adjusted_p_value if decision else None,
                holm_rejected=decision.rejected if decision else None,
                verdict=verdict,
            )
        )
    return tuple(results)


def select_forward_candidate(results: tuple[DirectionMoveResult, ...]) -> str | None:
    candidates = [
        row
        for row in results
        if row.verdict == "forward_candidate" and row.paired_hit_rate_delta_pct is not None
    ]
    if not candidates:
        return None
    winner = max(
        candidates,
        key=lambda row: (row.paired_hit_rate_delta_pct or 0.0, row.direction),
    )
    return winner.direction


def input_fingerprint(
    episodes: tuple[OutcomeSignalEpisode, ...],
    signal_paths: dict[str, ExactPricePath],
    control_paths: dict[str, ExactPricePath],
) -> str:
    payload = {
        "episodes": [asdict(item) for item in episodes],
        "signal_paths": [asdict(signal_paths[key]) for key in sorted(signal_paths)],
        "control_paths": [asdict(control_paths[key]) for key in sorted(control_paths)],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()
