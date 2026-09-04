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
from datetime import UTC, datetime, timedelta
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
from .momentum_flow_bidirectional_burst_study import DIRECTIONS as REGISTERED_DIRECTIONS
from .momentum_flow_bidirectional_burst_study import utc_week_key as _utc_week

# --- HYP-016's own frozen discovery window ------------------------------
#
# Registered in docs/research/discovery-ledger.md's own HYP-016 row --
# copied here verbatim (not re-typed from memory) so the formal freeze path
# (research/cex-activity-discovery-completion-v1) has exactly one place
# that can accept --since/--until, rejecting anything else, matching this
# codebase's established discipline for a viewed, already-registered
# window (mirrors SOURCE_LEAD_FORWARD_COHORT_START's own single-frozen-
# value pattern in source_lead_forward_cohort.py). The broader
# 2026-08-14 through 2026-08-28 context was already viewed per that same
# ledger row; DISCOVERY_SINCE/DISCOVERY_UNTIL below are the narrower
# half-open window the formal read actually commits to, not the wider
# context.
HYPOTHESIS_ID = "HYP-016"
DISCOVERY_SINCE = datetime(2026, 8, 18, tzinfo=UTC)
DISCOVERY_UNTIL = datetime(2026, 8, 27, tzinfo=UTC)

PRIMARY_MOVE_PCT = 25.0
OUTCOME_HORIZON_MINUTES = 24 * 60
CONTROL_QUIET_HOURS = 24
CONTROL_SEARCH_DAYS = 7
DISCOVERY_MIN_PAIRS = 100
DISCOVERY_MIN_CLUSTERS = 20
DISCOVERY_MIN_WEEKS = 2

# Colleague review, 2026-09-03 (research/cex-activity-discovery-completion-v1
# planning): build_control_requests already restricts every control
# candidate to strictly inside [since, until) (`if candidate_at < since or
# candidate_at >= until: continue`, unchanged below) -- an earlier proposal
# to widen that search past the discovery window's own edges was rejected:
# the window is already viewed and its boundaries already registered
# (DISCOVERY_SINCE/DISCOVERY_UNTIL above), so widening the control search
# now would add previously-unregistered data and change the estimand after
# the window was already chosen, exactly the kind of post-hoc adjustment
# this codebase's other frozen contracts (Holm correction, pre-registered
# thresholds) exist to prevent. This version pins that decision explicitly,
# so a future change to the boundary behavior is a deliberate, versioned
# one, not a silent edit to already-working code.
CONTROL_BOUNDARY_POLICY_VERSION = "within_discovery_window_v1"

# The five reasons ExactPricePath.unresolved_reason can return, plus the
# one reason it structurally cannot (a request_id the repository returned
# no row for at all -- classified by the caller, since there is no
# ExactPricePath instance to classify it on). Every unresolved path in a
# funnel must fall into exactly one of these -- a caller finding a reason
# outside this set has a real bug to investigate, not a new category to
# silently accept.
MISSING_PATH_RESULT_REASON = "missing_path_result"
UNRESOLVED_REASONS = frozenset(
    {
        "missing_entry_bar",
        "invalid_entry_price",
        "incomplete_24h_path",
        "missing_extrema",
        "invalid_extrema",
        MISSING_PATH_RESULT_REASON,
    }
)

# Bumped whenever select_matched_pairs' own assignment algorithm changes --
# colleague review, 2026-09-02: HYP-017's own already-recorded discovery
# read (docs/research/discovery-ledger.md) was produced by the PRIOR
# greedy "first available control" version of select_matched_pairs, now
# proven (test_pair_selection_maximizes_pairs_instead_of_greedy_first_
# available) capable of systematically losing pairs an earlier episode
# could have released to a later one. Every report that calls
# select_matched_pairs carries this in its own manifest so a reader can
# tell which assignment policy actually produced a given result --
# without it, an old ledger row (or a re-run's own output) is silently
# ambiguous about which algorithm built its own pair count. Read together
# with docs/research/discovery-ledger.md's own HYP-017 note before
# treating that row's 189 pairs as reliable evidence either way.
MATCHING_POLICY_VERSION = "max_cardinality_bipartite_matching_v1"


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
    """`unresolved_reason` classifies exactly WHY a path failed to resolve
    -- colleague review, 2026-09-03 (research/cex-activity-discovery-
    completion-v1 planning): a single collapsed `resolved` boolean cannot
    reproduce the funnel a real formal report must show (how many failed
    for a missing entry bar versus a genuinely gappy 24h outcome window
    are very different findings). `resolved` is now DERIVED from
    `unresolved_reason is None`, not a second, independently-maintained
    boolean check -- the two can never silently drift apart. See
    UNRESOLVED_REASONS below for the full frozen set of reason strings
    this can return; MISSING_PATH_RESULT (a request_id the repository
    returned no row for at all, a shape this dataclass itself cannot
    represent) is classified by the caller, not here."""

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
    def unresolved_reason(self) -> str | None:
        if self.entry_price is None:
            return "missing_entry_bar"
        if not math.isfinite(self.entry_price) or not (self.entry_price > 0):
            return "invalid_entry_price"
        if self.observed_minutes != OUTCOME_HORIZON_MINUTES:
            return "incomplete_24h_path"
        if self.max_high is None or self.min_low is None:
            return "missing_extrema"
        if not (
            math.isfinite(self.max_high)
            and math.isfinite(self.min_low)
            and self.max_high > 0
            and self.min_low > 0
        ):
            return "invalid_extrema"
        return None

    @property
    def resolved(self) -> bool:
        return self.unresolved_reason is None


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
    """Deterministic MAXIMUM-CARDINALITY bipartite matching between
    episodes and their own resolved quiet controls (Kuhn's augmenting-path
    algorithm), not a greedy first-available pick.

    Colleague review, 2026-09-01: the earlier greedy version processed
    episodes in trigger_at order and let each one take the FIRST resolved,
    unused control among its own candidates -- an early episode could
    monopolize a control another, later episode could ONLY use, losing a
    pair the correct global assignment would have kept. Example: E1 can
    use C1 or C2, E2 can only use C1. Greedy (E1 first) takes C1 for E1,
    leaving E2 with nothing -- 1 pair total. The correct maximum matching
    is E1->C2, E2->C1 -- 2 pairs. Verified directly:
    test_pair_selection_maximizes_pairs_instead_of_greedy_first_available
    reproduces this exact scenario.

    A control candidate's own symbol always equals its episode's own
    symbol (build_control_requests only ever proposes same-instrument
    controls), so this bipartite graph never has an edge crossing symbols
    -- an implicit per-exact-instrument matching, with no need to
    partition by symbol by hand.

    Ordering is fixed for reproducibility: episodes are processed by
    (trigger_at, episode_id), and each episode's own candidate controls in
    control_requests' own existing (nearest-day-first) order -- the same
    order the earlier greedy version used, so a tie between two otherwise-
    equal maximum matchings resolves the same way every run."""
    ordered_episodes = sorted(episodes, key=lambda item: (item.trigger_at, item.episode_id))

    eligible: list[OutcomeSignalEpisode] = []
    signal_by_episode: dict[int, ExactPricePath] = {}
    candidates_by_episode: dict[int, tuple[tuple[str, datetime], ...]] = {}
    path_by_key: dict[tuple[str, datetime], ExactPricePath] = {}

    for episode in ordered_episodes:
        signal = signal_paths.get(signal_request(episode).request_id)
        if signal is None or not signal.resolved:
            continue
        keys: list[tuple[str, datetime]] = []
        for request in control_requests.get(episode.episode_id, ()):
            path = control_paths.get(request.request_id)
            if path is None or not path.resolved:
                continue
            key = (request.symbol, request.trigger_at)
            keys.append(key)
            path_by_key.setdefault(key, path)
        eligible.append(episode)
        signal_by_episode[episode.episode_id] = signal
        candidates_by_episode[episode.episode_id] = tuple(keys)

    # match_for_control[key] = the episode_id currently assigned that
    # control -- Kuhn's algorithm: try to give each episode, in turn, one
    # of its own candidate controls; if every candidate is already taken,
    # try to bump the current occupant of one candidate onto a DIFFERENT
    # one of ITS OWN candidates first (an augmenting path), only failing
    # if no such reshuffle exists anywhere down the chain.
    match_for_control: dict[tuple[str, datetime], int] = {}

    def _try_augment(episode_id: int, visited: set[tuple[str, datetime]]) -> bool:
        for key in candidates_by_episode[episode_id]:
            if key in visited:
                continue
            visited.add(key)
            occupant = match_for_control.get(key)
            if occupant is None or _try_augment(occupant, visited):
                match_for_control[key] = episode_id
                return True
        return False

    for episode in eligible:
        _try_augment(episode.episode_id, set())

    key_for_episode = {episode_id: key for key, episode_id in match_for_control.items()}
    pairs: list[MatchedMovePair] = []
    for episode in eligible:
        control_key = key_for_episode.get(episode.episode_id)
        if control_key is not None:
            pairs.append(
                MatchedMovePair(
                    episode, signal_by_episode[episode.episode_id], path_by_key[control_key]
                )
            )
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


def contract_fingerprint(
    *,
    candidate_query_version: str,
    path_query_version: str,
    extreme_threshold_pct: float,
    refractory_minutes: int,
    min_volume_24h_usd: float,
) -> str:
    """Hashes every parameter this module's own estimand/decision-rule
    computation depends on -- colleague review, 2026-09-04
    (research/cex-activity-discovery-completion-v1). Two things this
    closes, found in two separate review rounds:

    1. A frozen artifact's own raw rows never change, but `build_report`
       (cex_activity_discovery_report.py) re-runs LIVE matching/bootstrap/
       Holm code against them on every `--from-artifact` call. If any
       CODE CONSTANT this hashes (`matching_policy_version`,
       `primary_move_pct`, the evidence floors, the bootstrap seed/
       iterations/confidence level, ...) ever changes in a later code
       revision -- deliberately or by accident -- re-evaluating an OLD
       artifact with NEW code would silently compute a different verdict
       while still labeling itself with whatever the CURRENT code's
       version strings happen to be. `freeze_dataset` stores this
       fingerprint at freeze time; `build_report` recomputes it fresh from
       the CURRENT code's own constants (passing back the dataset's own
       recorded `extreme_threshold_pct`/`refractory_minutes`/
       `min_volume_24h_usd`, since those are operational parameters this
       module has no independent "live" opinion about) and refuses to
       render if they disagree.
    2. `extreme_threshold_pct`/`refractory_minutes`/`min_volume_24h_usd`
       directly determine WHICH candidates/episodes a freeze selects --
       the sampling frame itself, not just how it is later evaluated.
       Colleague review, 2026-09-04 follow-up: hashing them here is not
       enough on its own if this fingerprint only ever lived in the
       artifact's `extra` field, since the generic artifact fingerprint
       (`research_dataset_artifact._fingerprint()`) deliberately excludes
       `extra` -- two freezes with the same resulting ROWS but different
       thresholds could otherwise collide on the same content fingerprint
       and silently resolve as `ALREADY_EXISTS`, discarding the second
       run's own different parameters with no error at all.
       `cex_activity_discovery_dataset_artifact.build_cohort` now takes
       this fingerprint (and the three raw parameters, for human
       auditability) and folds it into `cohort` itself, which DOES
       participate in both the generic artifact fingerprint and the
       cohort-lock key -- a different threshold is now genuinely a
       different cohort, never silently absorbed into an existing one.

    `candidate_query_version`/`path_query_version` are owned by the
    report/repository modules that actually run those queries, not by this
    module -- passed in rather than imported, since both of those modules
    already import from this one and importing back would be circular."""
    payload = {
        "candidate_query_version": candidate_query_version,
        "path_query_version": path_query_version,
        "extreme_threshold_pct": extreme_threshold_pct,
        "refractory_minutes": refractory_minutes,
        "min_volume_24h_usd": min_volume_24h_usd,
        "matching_policy_version": MATCHING_POLICY_VERSION,
        "primary_move_pct": PRIMARY_MOVE_PCT,
        "outcome_horizon_minutes": OUTCOME_HORIZON_MINUTES,
        "control_search_days": CONTROL_SEARCH_DAYS,
        "control_quiet_hours": CONTROL_QUIET_HOURS,
        "control_boundary_policy_version": CONTROL_BOUNDARY_POLICY_VERSION,
        "discovery_min_pairs": DISCOVERY_MIN_PAIRS,
        "discovery_min_clusters": DISCOVERY_MIN_CLUSTERS,
        "discovery_min_weeks": DISCOVERY_MIN_WEEKS,
        "bootstrap_seed": DEFAULT_BOOTSTRAP_SEED,
        "bootstrap_iterations": DEFAULT_BOOTSTRAP_ITERATIONS,
        "confidence_level": DEFAULT_CONFIDENCE_LEVEL,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


class IncompatibleResearchContractError(ValueError):
    """Raised by `build_report` when the CURRENT code's own
    `contract_fingerprint()` does not match the one an artifact was frozen
    with -- fail closed rather than silently applying a changed matching/
    bootstrap/floor/query contract to old frozen raw data and mislabeling
    the result with the new code's own version strings."""
