"""Discovery pass for the token-behavior candidate filter family.

The full pre-registration lives in `token_behavior_descriptors.py`'s module
docstring (research question, data window + fingerprint, baseline, the 4
candidates with their directions, `historical_spike_v1`/
`historical_volatility_30d` contracts, primary metric, discovery readiness
gates, confirmation requirement). This module is the bridge from that frozen
contract to real decisions/outcomes: it reads the already-frozen token-
history Parquet dataset, loads the corresponding baseline (`score_6`)
decisions from the live replay store, computes the 4 descriptors
point-in-time, and feeds paired baseline/challenger returns into
`challenger_inference.build_challenger_inference` -- the same cluster-
bootstrap-plus-Holm machinery `oi_growth_filter_report.py` already uses, not
reimplemented here.

Threshold freezing (two passes, no I/O twice): candidates 2-4 gate on this
run's own cohort median (volatility, recovery days, listing age). That
median is computed from descriptors ALONE, in a first pass that touches only
bars already sitting on disk (no market-path fetch, no simulate_decision) --
so freezing the threshold never depends on, or is contaminated by, any
return/outcome data. The real (candidate-gating) evaluation is a second pass
using the frozen thresholds from the first.

Candidate semantics: a candidate GATES the baseline's own decision to cash
or not -- it never recomputes a different decision, never selects a
different entry, matching `oi_growth_filter_report.py`'s established shape.
`candidate_gates[key] is True` means "filtered out, cash"; `False` means
"kept, same entry as baseline"; `None` means "this candidate's descriptor is
unresolved for this episode, excluded from that candidate's own population,
never folded into cash or a kept trade" (per each descriptor's own
right-censoring/coverage rules in `token_behavior_descriptors.py`).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, Any

import duckdb

from .challenger_inference import (
    DEFAULT_INFERENCE_SETTINGS,
    ChallengerEpisode,
    ChallengerInference,
    InferenceReadiness,
    build_challenger_inference,
)
from .clustered_inference import (
    BOOTSTRAP_SEED_DERIVATION,
    CLUSTER_BOOTSTRAP_VERSION,
    HOLM_CORRECTION_VERSION,
    ClusterObservation,
    leave_one_cluster_out_means,
)
from .decision_quality import SCORE_THRESHOLD_BASELINE_POLICY, select_score_policy
from .episode_replay import PROTOCOL_VERSION
from .outcomes import RESOLVER_VERSION
from .replay import (
    DEFAULT_REPLAY_HORIZONS,
    FOUNDATION_VERSION,
    QUERY_VERSION,
    ReplayDataset,
    ReplayFilters,
    build_replay_dataset,
)
from .reporting import (
    ReportWindowNotStartedError,
    format_number,
    format_percentage,
    json_ready,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
    profit_factor,
)
from .token_behavior_descriptors import (
    DailyBar,
    RecoveryResult,
    days_since_last_spike_recovery,
    detect_historical_spikes,
    historical_volatility,
    listing_age_days,
    prior_spike_count,
)
from .virtual_market import DECISION_MARKET_PATH_VERSION, decision_market_path_fingerprint
from .virtual_strategy import (
    COST_MODEL_VERSION,
    DEFAULT_COSTS,
    ENTRY_MODEL_VERSION,
    EXIT_MODEL_VERSION,
    VIRTUAL_STRATEGY_VERSION,
    CostParameters,
    MarketPath,
    VirtualTrade,
    simulate_decision,
)

if TYPE_CHECKING:
    from .replay import ReplayDecision, ReplayEpisode
    from .virtual_market import DecisionMarketPath

TOKEN_BEHAVIOR_REPORT_VERSION = "token_behavior_discovery_report_v1"  # noqa: S105
TOKEN_BEHAVIOR_CANDIDATE_VERSION = "token_behavior_discovery_v1"  # noqa: S105
TOKEN_BEHAVIOR_INFERENCE_VERSION = "token_behavior_discovery_formal_inference_v1"  # noqa: S105
TOKEN_BEHAVIOR_STRATEGY_VERSIONS = ("pump_short_v1_market_quality",)

# The already-frozen dataset this report reads, and the exact cohort window
# it was built against. A manifest whose own fingerprint/since/until drift
# from these must never be silently trusted -- see _verify_manifest.
TOKEN_BEHAVIOR_DATASET_RUN_ID = "20260810T081729Z-6f781fae"  # noqa: S105
TOKEN_BEHAVIOR_DATASET_RELATIVE_PATH = Path(
    "backups/token-history/token_history_ohlcv_v1/20260810T081729Z-6f781fae"
)
TOKEN_BEHAVIOR_DATASET_CONTENT_FINGERPRINT = (
    "22d23eba6997b509802cd3fe7a50b7dd90958a525ade5275ffbd7444b5cd0651"  # noqa: S105
)
TOKEN_BEHAVIOR_DATASET_SINCE = datetime(2026, 7, 26, tzinfo=UTC)
TOKEN_BEHAVIOR_DATASET_UNTIL_EXCLUSIVE = datetime(2026, 8, 9, 23, 21, 14, 187586, tzinfo=UTC)

# Candidate filter family, frozen 2026-08-11 (see token_behavior_descriptors.py
# module docstring for the full contract and the reasoning behind each
# direction). Holm-corrected jointly, all 4 at once, never re-run one at a
# time to fish for significance.
PRIOR_SPIKE_LOOKBACK_DAYS = 90.0
HISTORICAL_SPIKE_THRESHOLD_PCT = 30.0
VOLATILITY_LOOKBACK_DAYS = 30
VOLATILITY_MIN_RETURNS = 29
RECOVERY_BAND_PCT = 10.0

CANDIDATE_NO_PRIOR_SPIKE = "no_prior_spike_90d"
CANDIDATE_HIGH_VOLATILITY = "above_median_volatility_30d"
CANDIDATE_SLOW_RECOVERY = "above_median_recovery_days"
CANDIDATE_YOUNG_LISTING = "below_median_listing_age"
CANDIDATE_VARIANT_KEYS = (
    CANDIDATE_NO_PRIOR_SPIKE,
    CANDIDATE_HIGH_VOLATILITY,
    CANDIDATE_SLOW_RECOVERY,
    CANDIDATE_YOUNG_LISTING,
)

# Discovery readiness gates, frozen 2026-08-11. This is discovery, not
# confirmation -- deliberately looser than oi_growth_filter_report.py's
# >=100/>=30-cluster/>=4-week confirmation bar (a forward cohort must still
# clear that stricter bar separately before any promotion). This looser
# floor is only actually enforceable because build_challenger_inference
# accepts a per-family formal_episodes/min_formal_clusters/
# directional_episodes override (added 2026-08-11 specifically for this):
# the shared module's own DEFAULT floor (replay.FORMAL_EPISODES=100/
# replay.MIN_FORMAL_CLUSTERS=30) is designed around confirmation-scale
# families like oi_growth and is NOT achievable on the frozen 47-instrument,
# ~2-week token-history dataset (an empirical count against production on
# 2026-08-11 found 69 baseline-triggered episodes across 46 of the 47
# instruments in the full frozen window -- comfortably clears 60/20, never
# 100/30). TOKEN_BEHAVIOR_DIRECTIONAL_EPISODES below is passed as this
# report's own directional_episodes override.
MIN_FORMAL_EPISODES = 60
MIN_FORMAL_CLUSTERS = 20
TOKEN_BEHAVIOR_DIRECTIONAL_EPISODES = 30
MIN_FORMAL_WEEKS = 2
MAX_WEEK_CONCENTRATION_PCT = 70.0
MIN_DESCRIPTOR_RESOLVED_PCT = 80.0
MIN_CHANGED_TRADES = 10
MIN_CHANGED_ASSETS = 8


def _week_key(value: datetime) -> str:
    year, week, _ = value.astimezone(UTC).isocalendar()
    return f"{year}-W{week:02d}"


# --- manifest + bars loading ---


@dataclass(frozen=True)
class TokenHistoryContext:
    bars: tuple[DailyBar, ...]
    onboarded_at: datetime


def _verify_parquet_checksum(parquet_path: Path, *, expected_sha256: str | None) -> None:
    # The manifest's own dataset_content_fingerprint (_verify_manifest) only
    # covers the manifest.json bytes themselves, never the bars.parquet
    # payloads it points at -- a manifest can drift-check clean while a
    # parquet file underneath it is stale, corrupted, or swapped. Each
    # instrument carries its own recorded sha256; verify it before trusting
    # the bars for anything.
    if not expected_sha256:
        raise ValueError(f"manifest is missing parquet_sha256 for {parquet_path}")
    actual = hashlib.sha256(parquet_path.read_bytes()).hexdigest()
    if actual != expected_sha256:
        raise ValueError(
            f"parquet checksum mismatch for {parquet_path}: "
            f"manifest expected {expected_sha256}, file hashes to {actual}"
        )


def _read_bars(parquet_path: Path) -> tuple[DailyBar, ...]:
    connection = duckdb.connect(":memory:")
    try:
        rows = connection.execute(
            "SELECT ts_ms, close, high FROM read_parquet(?) ORDER BY ts_ms",
            [parquet_path.as_posix()],
        ).fetchall()
    finally:
        connection.close()
    return tuple(DailyBar(ts_ms=row[0], close=row[1], high=row[2]) for row in rows)


def _verify_manifest(manifest: dict[str, Any]) -> None:
    inner = manifest["manifest"]
    if inner.get("run_id") != TOKEN_BEHAVIOR_DATASET_RUN_ID:
        raise ValueError(
            f"token-history dataset run_id drifted from the frozen pre-registration: "
            f"expected {TOKEN_BEHAVIOR_DATASET_RUN_ID}, got {inner.get('run_id')}"
        )
    if inner.get("dataset_content_fingerprint") != TOKEN_BEHAVIOR_DATASET_CONTENT_FINGERPRINT:
        raise ValueError(
            "token-history dataset content fingerprint drifted from the frozen "
            f"pre-registration: expected {TOKEN_BEHAVIOR_DATASET_CONTENT_FINGERPRINT}, "
            f"got {inner.get('dataset_content_fingerprint')}"
        )
    if not inner.get("dataset_ready"):
        raise ValueError("token-history dataset manifest reports dataset_ready=false")


def load_token_history_index(dataset_root: Path) -> dict[int, TokenHistoryContext]:
    """Reads the frozen manifest, verifies it against the pre-registered
    fingerprint/run_id, and returns pump_event_id -> (bars, onboarded_at) for
    every publishable instrument's decisions. Each instrument's bars are read
    from disk exactly once and shared across all of that instrument's
    decisions, not re-read per decision."""
    manifest = json.loads((dataset_root / "manifest.json").read_text())
    _verify_manifest(manifest)
    index: dict[int, TokenHistoryContext] = {}
    for result in manifest["results"]:
        if not result.get("publishable") or not result.get("parquet_relative_path"):
            continue
        parquet_path = dataset_root / result["parquet_relative_path"]
        _verify_parquet_checksum(parquet_path, expected_sha256=result.get("parquet_sha256"))
        bars = _read_bars(parquet_path)
        for decision in result["decisions"]:
            index[decision["pump_event_id"]] = TokenHistoryContext(
                bars=bars, onboarded_at=parse_utc_datetime(decision["onboarded_at"])
            )
    return index


# --- descriptors + frozen thresholds ---


@dataclass(frozen=True)
class DescriptorSet:
    prior_spike_count_90d: int | None
    historical_volatility_30d: float | None
    recovery: RecoveryResult
    listing_age_days: float


def _compute_descriptors(
    *, bars: tuple[DailyBar, ...], decision_ts: datetime, onboarded_at: datetime
) -> DescriptorSet:
    spike_history = detect_historical_spikes(
        bars=bars,
        decision_ts=decision_ts,
        lookback_days=int(PRIOR_SPIKE_LOOKBACK_DAYS),
        threshold_pct=HISTORICAL_SPIKE_THRESHOLD_PCT,
    )
    return DescriptorSet(
        prior_spike_count_90d=prior_spike_count(spike_history=spike_history),
        historical_volatility_30d=historical_volatility(
            bars=bars,
            decision_ts=decision_ts,
            lookback_days=VOLATILITY_LOOKBACK_DAYS,
            min_returns=VOLATILITY_MIN_RETURNS,
        ),
        recovery=days_since_last_spike_recovery(
            bars=bars,
            decision_ts=decision_ts,
            spike_history=spike_history,
            recovery_band_pct=RECOVERY_BAND_PCT,
        ),
        listing_age_days=listing_age_days(decision_ts=decision_ts, onboarded_at=onboarded_at),
    )


@dataclass(frozen=True)
class FrozenThresholds:
    volatility_median: float
    recovery_median_days: float
    listing_age_median_days: float
    # How many baseline-triggered, token-history-covered episodes fed each
    # median -- reported for transparency, not itself a readiness gate (the
    # MIN_DESCRIPTOR_RESOLVED_PCT gate below covers that).
    volatility_sample_size: int
    recovery_sample_size: int
    listing_age_sample_size: int


def in_scope_episodes(
    dataset: ReplayDataset, token_history: dict[int, TokenHistoryContext]
) -> tuple[ReplayEpisode, ...]:
    """The baseline itself is scoped to the frozen dataset's own instruments
    (see token_behavior_descriptors.py's module docstring: "restricted to
    instruments covered by the frozen dataset (47 instruments)") -- an
    episode whose pump_event_id has no token-history entry is not a
    descriptor-unresolved member of this report's population, it is not a
    member of the population at all. Every downstream pass (threshold
    freezing, evaluation, funnel counts) must use this same filtered set,
    never `dataset.eligible_episodes` directly."""
    return tuple(
        episode for episode in dataset.eligible_episodes if episode.pump_event_id in token_history
    )


def _first_pass_descriptors(
    episodes: tuple[ReplayEpisode, ...], token_history: dict[int, TokenHistoryContext]
) -> list[DescriptorSet]:
    """Descriptor values for every baseline-triggered episode in `episodes`
    (already scope-filtered by `in_scope_episodes`) -- used ONLY to freeze
    candidates 2-4's median thresholds before the real, threshold-applying
    pass. No market-path fetch or simulate_decision here; descriptors need
    neither, and freezing a threshold must never depend on outcome data."""
    collected: list[DescriptorSet] = []
    for episode in episodes:
        selection = select_score_policy(episode, SCORE_THRESHOLD_BASELINE_POLICY)
        if selection.status != "selected" or selection.decision is None:
            continue
        context = token_history.get(episode.pump_event_id)
        if context is None:
            continue
        collected.append(
            _compute_descriptors(
                bars=context.bars,
                decision_ts=selection.decision.ts,
                onboarded_at=context.onboarded_at,
            )
        )
    return collected


class InsufficientDescriptorDataError(ValueError):
    """Raised by freeze_thresholds when the baseline cohort does not yet
    carry enough resolved descriptor values to freeze a median. This is an
    expected, early "not enough data yet" state, not a report bug --
    build_token_behavior_discovery_report catches it and produces an
    insufficient_data verdict, never an unhandled crash."""


def freeze_thresholds(descriptor_sample: list[DescriptorSet]) -> FrozenThresholds:
    volatilities = [
        d.historical_volatility_30d
        for d in descriptor_sample
        if d.historical_volatility_30d is not None
    ]
    recovered_days: list[float] = []
    for d in descriptor_sample:
        if d.recovery.status != "recovered":
            continue
        if d.recovery.recovered_in_days is None:
            raise ValueError("a 'recovered' status must always carry recovered_in_days")
        recovered_days.append(d.recovery.recovered_in_days)
    listing_ages = [d.listing_age_days for d in descriptor_sample]
    if not volatilities or not recovered_days or not listing_ages:
        raise InsufficientDescriptorDataError(
            "insufficient resolved descriptor values in the baseline cohort to freeze "
            "candidate thresholds -- collecting, not a report failure"
        )
    return FrozenThresholds(
        volatility_median=median(volatilities),
        recovery_median_days=median(recovered_days),
        listing_age_median_days=median(listing_ages),
        volatility_sample_size=len(volatilities),
        recovery_sample_size=len(recovered_days),
        listing_age_sample_size=len(listing_ages),
    )


# --- candidate gating (True=cash, False=kept, None=unresolved/excluded) ---


def _candidate_no_prior_spike(descriptors: DescriptorSet) -> bool | None:
    if descriptors.prior_spike_count_90d is None:
        return None
    return descriptors.prior_spike_count_90d == 0


def _candidate_high_volatility(
    descriptors: DescriptorSet, thresholds: FrozenThresholds
) -> bool | None:
    if descriptors.historical_volatility_30d is None:
        return None
    return descriptors.historical_volatility_30d > thresholds.volatility_median


def _candidate_slow_recovery(
    descriptors: DescriptorSet, thresholds: FrozenThresholds
) -> bool | None:
    recovery = descriptors.recovery
    if recovery.status in ("no_prior_spike", "missing_reference_price"):
        # No prior-spike history to judge recovery from at all -- excluded
        # from this candidate's population entirely, not cash, not kept.
        return None
    if recovery.status == "recovered":
        assert recovery.recovered_in_days is not None
        return recovery.recovered_in_days > thresholds.recovery_median_days
    # not_yet_recovered_by_decision: right-censored. Only a DEFINITIVE slow-
    # recovery signal once observed_for_days already exceeds the threshold;
    # shorter than that means the decision came too soon to know, which is
    # unresolved, not a confirmed signal either way.
    assert recovery.observed_for_days is not None
    if recovery.observed_for_days >= thresholds.recovery_median_days:
        return True
    return None


def _candidate_young_listing(descriptors: DescriptorSet, thresholds: FrozenThresholds) -> bool:
    # listing_age_days is always computable given decision_ts/onboarded_at
    # are always known -- never unresolved.
    return descriptors.listing_age_days < thresholds.listing_age_median_days


def _evaluate_candidates(
    descriptors: DescriptorSet, thresholds: FrozenThresholds
) -> dict[str, bool | None]:
    return {
        CANDIDATE_NO_PRIOR_SPIKE: _candidate_no_prior_spike(descriptors),
        CANDIDATE_HIGH_VOLATILITY: _candidate_high_volatility(descriptors, thresholds),
        CANDIDATE_SLOW_RECOVERY: _candidate_slow_recovery(descriptors, thresholds),
        CANDIDATE_YOUNG_LISTING: _candidate_young_listing(descriptors, thresholds),
    }


# --- per-episode evaluation ---


@dataclass(frozen=True)
class TokenBehaviorEpisodeResult:
    pump_event_id: int
    cluster_key: str
    base: str
    week_key: str
    decision_id: str | None
    decision_ts: datetime | None
    baseline_triggered: bool
    baseline_net_return_pct: float | None
    descriptors: DescriptorSet | None
    candidate_gates: dict[str, bool | None]
    candidate_returns: dict[str, float | None]
    trade: VirtualTrade | None
    error: str | None = None


def _missing_path(episode: ReplayEpisode, decision: ReplayDecision) -> MarketPath:
    return MarketPath(
        pump_event_id=episode.pump_event_id,
        exchange=decision.exchange,
        base=decision.base,
        status="missing_path",
        candles=(),
        error="market path was not loaded",
    )


def evaluate_token_behavior_episode(
    episode: ReplayEpisode,
    path_by_decision: dict[str, MarketPath],
    token_history: dict[int, TokenHistoryContext],
    thresholds: FrozenThresholds,
    costs: CostParameters,
) -> TokenBehaviorEpisodeResult:
    """Pure per-episode evaluation (no I/O beyond the already-fetched market
    path map and already-loaded bars), testable directly against hand-built
    episodes/decisions/bars."""
    selection = select_score_policy(episode, SCORE_THRESHOLD_BASELINE_POLICY)
    week = _week_key(episode.first_decision_at)

    if selection.status == "not_triggered":
        return TokenBehaviorEpisodeResult(
            episode.pump_event_id,
            episode.cluster_key,
            episode.base,
            week,
            None,
            None,
            False,
            None,
            None,
            {},
            dict.fromkeys(CANDIDATE_VARIANT_KEYS, 0.0),
            None,
        )
    if selection.status == "unresolved" or selection.decision is None:
        return TokenBehaviorEpisodeResult(
            episode.pump_event_id,
            episode.cluster_key,
            episode.base,
            week,
            None,
            None,
            False,
            None,
            None,
            {},
            dict.fromkeys(CANDIDATE_VARIANT_KEYS, None),
            None,
            error=selection.error or "baseline selection unresolved",
        )

    decision = selection.decision
    week = _week_key(decision.ts)

    # Baseline resolution never depends on token-history coverage -- it is
    # the unmodified production strategy and would trade regardless. Missing
    # token-history bars only ever leave the CANDIDATES unresolved for this
    # episode; they must never shrink the baseline's own resolved sample.
    path = path_by_decision.get(decision.decision_id or "") or _missing_path(episode, decision)
    trade = simulate_decision(
        episode,
        path,
        decision,
        selection_reason=f"token_behavior_discovery:{TOKEN_BEHAVIOR_CANDIDATE_VERSION}",
        costs=costs,
    )
    baseline_resolved = trade.status == "complete"
    baseline_return = trade.net_return_pct if baseline_resolved else None

    context = token_history.get(episode.pump_event_id)
    if context is None:
        return TokenBehaviorEpisodeResult(
            episode.pump_event_id,
            episode.cluster_key,
            episode.base,
            week,
            decision.decision_id,
            decision.ts,
            True,
            baseline_return,
            None,
            {},
            dict.fromkeys(CANDIDATE_VARIANT_KEYS, None),
            trade,
            error="no token-history bars for this pump_event_id in the frozen dataset",
        )

    descriptors = _compute_descriptors(
        bars=context.bars, decision_ts=decision.ts, onboarded_at=context.onboarded_at
    )
    gates = _evaluate_candidates(descriptors, thresholds)
    returns: dict[str, float | None] = {}
    for key, gate in gates.items():
        if gate is None:
            returns[key] = None
        elif gate:
            returns[key] = 0.0  # filtered out -> cash
        else:
            returns[key] = baseline_return  # kept -> same as baseline

    return TokenBehaviorEpisodeResult(
        episode.pump_event_id,
        episode.cluster_key,
        episode.base,
        week,
        decision.decision_id,
        decision.ts,
        True,
        baseline_return,
        descriptors,
        gates,
        returns,
        trade,
        error=trade.error,
    )


# --- readiness + verdict ---


@dataclass(frozen=True)
class WeekConcentration:
    distinct_weeks: int
    largest_week_pct: float | None


def _week_concentration(results: tuple[TokenBehaviorEpisodeResult, ...]) -> WeekConcentration:
    counts = Counter(result.week_key for result in results if result.baseline_triggered)
    if not counts:
        return WeekConcentration(distinct_weeks=0, largest_week_pct=None)
    total = sum(counts.values())
    return WeekConcentration(
        distinct_weeks=len(counts), largest_week_pct=max(counts.values()) / total * 100
    )


@dataclass(frozen=True)
class CandidateReadiness:
    variant_key: str
    resolved_count: int
    resolved_pct: float
    changed_trades: int
    changed_assets: int
    materiality_ok: bool
    # Cash-inclusive profit factor over every descriptor-resolved episode in
    # the formal sample (gross positive / abs(gross negative) of the
    # candidate's OWN return series, cash 0.0 included) -- the frozen
    # contract's "profit factor > 1 on the exact same frozen sample" gate.
    # None means no losing episodes at all (profit_factor's own convention),
    # which trivially satisfies ">1" and is treated as ok.
    profit_factor: float | None
    profit_factor_ok: bool


def _candidate_readiness(
    variant_key: str, formal_sample_population: tuple[TokenBehaviorEpisodeResult, ...]
) -> CandidateReadiness:
    """`formal_sample_population` must be restricted to
    `inference.formal_sample_event_ids` -- the exact same episodes the
    Holm-corrected statistical test ran over. Computing readiness over a
    larger population than the one actually tested would let episodes the
    statistics never saw decide materiality/profit-factor gating."""
    resolved = [
        result
        for result in formal_sample_population
        if result.candidate_gates.get(variant_key) is not None
    ]
    resolved_pct = (
        len(resolved) / len(formal_sample_population) * 100 if formal_sample_population else 0.0
    )
    changed = [
        result
        for result in resolved
        if result.candidate_gates[variant_key] is True
        and result.baseline_net_return_pct is not None
    ]
    changed_assets = len({result.base for result in changed})
    # "Descriptor resolved" (gate is not None, the resolved_count/resolved_pct
    # above) is not the same thing as "return resolved" -- a kept (gate=False)
    # episode whose baseline trade itself never resolved still has gate=False
    # but candidate_returns[variant_key]=None. Profit factor needs an
    # explicit second, narrower filter for that.
    resolved_returns = [
        value
        for result in resolved
        if (value := result.candidate_returns.get(variant_key)) is not None
    ]
    pf = profit_factor(resolved_returns) if resolved_returns else None
    return CandidateReadiness(
        variant_key=variant_key,
        resolved_count=len(resolved),
        resolved_pct=resolved_pct,
        changed_trades=len(changed),
        changed_assets=changed_assets,
        materiality_ok=len(changed) >= MIN_CHANGED_TRADES and changed_assets >= MIN_CHANGED_ASSETS,
        profit_factor=pf,
        profit_factor_ok=pf is None or pf > 1,
    )


def _last_week_robustness(
    variant_key: str, formal_population: tuple[TokenBehaviorEpisodeResult, ...]
) -> str:
    """Non-statistical sign-consistency check on the single most recent UTC
    week alone: does the candidate's own direction (paired delta sign) and
    its own cash-inclusive expectancy both stay positive there? A slice this
    small cannot support a significance test -- only sign-consistency is
    asked of it."""
    weeks = sorted({result.week_key for result in formal_population if result.baseline_triggered})
    if not weeks:
        return "no_data"
    last_week = weeks[-1]
    slice_results = [result for result in formal_population if result.week_key == last_week]
    deltas: list[float] = []
    challenger_returns: list[float] = []
    for result in slice_results:
        challenger_return = result.candidate_returns.get(variant_key)
        if challenger_return is None:
            continue
        challenger_returns.append(challenger_return)
        if result.baseline_net_return_pct is not None:
            deltas.append(challenger_return - result.baseline_net_return_pct)
    if not deltas or not challenger_returns:
        return "insufficient_data"
    mean_delta = sum(deltas) / len(deltas)
    mean_expectancy = sum(challenger_returns) / len(challenger_returns)
    if mean_delta > 0 and mean_expectancy > 0:
        return "positive"
    return "not_positive"


@dataclass(frozen=True)
class TokenBehaviorManifest:
    protocol_version: str
    replay_engine_version: str
    replay_query_version: str
    report_version: str
    candidate_version: str
    inference_version: str
    virtual_strategy_version: str
    entry_model_version: str
    exit_model_version: str
    cost_model_version: str
    market_path_version: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    dataset_since: datetime
    dataset_until_exclusive: datetime
    dataset_run_id: str
    dataset_content_fingerprint: str
    decision_input_fingerprint: str
    market_path_fingerprint: str
    strategy_versions: tuple[str, ...]
    resolver_version: str
    required_horizons: tuple[int, ...]
    fallback_allowed: bool
    # None only when the baseline cohort did not yet carry enough resolved
    # descriptor values to freeze a median at all -- see
    # InsufficientDescriptorDataError. final_verdict is "insufficient_data"
    # in that case and every downstream candidate field is empty.
    thresholds: FrozenThresholds | None
    bootstrap_iterations: int
    bootstrap_seed: int
    bootstrap_confidence_level: float
    holm_family_alpha: float
    min_formal_episodes: int
    min_formal_clusters: int
    min_formal_weeks: int
    report_scope: str = "discovery_only_no_production_score_change"


@dataclass(frozen=True)
class TokenBehaviorReport:
    manifest: TokenBehaviorManifest
    dataset_episodes: int
    eligible_episodes: int
    # eligible_episodes minus the ones scoped out because their
    # pump_event_id has no entry in the frozen 47-instrument token-history
    # dataset -- see in_scope_episodes.
    out_of_scope_episodes: int
    formal_population_size: int
    # How many of formal_population_size actually fed the Holm-corrected
    # statistics (inference.readiness.formal_sample_episodes,
    # capped at MIN_FORMAL_EPISODES). candidate_readiness/week_sensitivity/
    # last_week_robustness are all computed over exactly this many.
    formal_sample_size: int
    week_concentration: WeekConcentration
    candidate_readiness: tuple[CandidateReadiness, ...]
    inference: ChallengerInference
    week_sensitivity: dict[str, tuple[tuple[str, float], ...]]
    last_week_robustness: dict[str, str]
    final_verdict: str
    nominated_candidate: str | None
    episode_results: tuple[TokenBehaviorEpisodeResult, ...]


def _is_canonical_run(filters: ReplayFilters, costs: CostParameters) -> bool:
    return (
        filters.strategy_versions == TOKEN_BEHAVIOR_STRATEGY_VERSIONS
        and filters.resolver_version == RESOLVER_VERSION
        and filters.required_horizons == DEFAULT_REPLAY_HORIZONS
        and filters.allow_fallback is False
        and costs.taker_fee_bps_per_side == DEFAULT_COSTS.taker_fee_bps_per_side
        and costs.funding_cost_bps_per_8h == DEFAULT_COSTS.funding_cost_bps_per_8h
    )


def _final_verdict(
    *,
    canonical_run: bool,
    formal_population_size: int,
    formal_cluster_count: int,
    week_concentration: WeekConcentration,
    inference: ChallengerInference,
    candidate_readiness: dict[str, CandidateReadiness],
    last_week_robustness: dict[str, str],
) -> tuple[str, str | None]:
    """Returns (verdict, nominated_candidate). At most one candidate may be
    nominated per this discovery pass -- see module/descriptor docstrings."""
    if not canonical_run:
        return "sensitivity_only_no_promotion", None
    if (
        formal_population_size < MIN_FORMAL_EPISODES
        or formal_cluster_count < MIN_FORMAL_CLUSTERS
        or week_concentration.distinct_weeks < MIN_FORMAL_WEEKS
        or week_concentration.largest_week_pct is None
        or week_concentration.largest_week_pct > MAX_WEEK_CONCENTRATION_PCT
    ):
        return "insufficient_data", None
    if inference.readiness.status != "formal_sample_ready":
        # This report's own gates above passed, but build_challenger_inference
        # (called with this report's own MIN_FORMAL_EPISODES/MIN_FORMAL_CLUSTERS/
        # TOKEN_BEHAVIOR_DIRECTIONAL_EPISODES overrides) still reports something
        # other than ready -- e.g. insufficient_resolution or
        # insufficient_triggers from a genuinely unexpected gap, not merely
        # a smaller family-specific sample size (that's already accounted
        # for by passing our own floor). No statistical result exists yet --
        # this is "not enough data to test", never "tested, no separation
        # found". inference.challengers is empty in this case.
        return "insufficient_data", None

    nominees = []
    for challenger in inference.challengers:
        readiness = candidate_readiness.get(challenger.variant_key)
        if readiness is None:
            continue
        if (
            challenger.verdict == "shadow_candidate"
            and readiness.resolved_pct >= MIN_DESCRIPTOR_RESOLVED_PCT
            and readiness.materiality_ok
            and readiness.profit_factor_ok
            and last_week_robustness.get(challenger.variant_key) == "positive"
        ):
            nominees.append(challenger)
    if not nominees:
        return "no_separation", None
    if len(nominees) == 1:
        return "candidate", nominees[0].variant_key
    # Holm-Bonferroni controls the family-wise error rate; it does NOT
    # guarantee at most one rejection. Two or more candidates clearing every
    # gate in the same pass is a legitimate, foreseeable outcome (strong
    # data, not a bug) -- the frozen contract caps nomination at exactly
    # one, so break the tie deterministically by strongest evidence (lowest
    # Holm-adjusted p-value), then by variant_key so the choice is fully
    # reproducible even if that also ties.
    strongest = min(nominees, key=lambda c: (c.paired.holm_adjusted_p_value, c.variant_key))
    return "candidate", strongest.variant_key


def _structural_candidate_gap_count(
    formal_sample_preview: tuple[TokenBehaviorEpisodeResult, ...],
) -> int:
    """How many episodes in the formal sample are NOT "completely paired"
    (per challenger_inference's own definition) purely because at least one
    candidate's own descriptor is structurally/expectedly unresolved for
    that episode (its `candidate_gates[key] is None`) -- e.g. candidate 3
    excluded whenever `no_prior_spike`. This is the value to pass as
    `max_unresolved_tolerance`.

    An episode whose BASELINE itself failed to resolve is deliberately
    EXCLUDED from this count: that is a real simulate_decision/market-path
    problem (the same zero-tolerance signal every other challenger family
    relies on), never a documented candidate-side exclusion, and must keep
    counting against the tolerance so a genuine data problem still trips
    `insufficient_resolution`.
    """
    return sum(
        result.baseline_net_return_pct is not None
        and any(result.candidate_gates.get(key) is None for key in CANDIDATE_VARIANT_KEYS)
        for result in formal_sample_preview
    )


def _insufficient_threshold_data_report(
    *,
    dataset: ReplayDataset,
    filters: ReplayFilters,
    paths: tuple[DecisionMarketPath, ...],
    revision: str,
    generated_at: datetime,
    working_tree_dirty: bool,
    out_of_scope_count: int,
) -> TokenBehaviorReport:
    # Callers only reach this helper via build_token_behavior_discovery_report,
    # which already validated filters.since == TOKEN_BEHAVIOR_DATASET_SINCE
    # (a datetime, never None) before this point.
    assert filters.since is not None
    empty_readiness = InferenceReadiness(
        status="collecting",
        eligible_episodes=0,
        formal_sample_episodes=0,
        formal_sample_clusters=0,
        baseline_resolved=0,
        completely_paired_episodes=0,
    )
    empty_inference = ChallengerInference(
        inference_version=TOKEN_BEHAVIOR_INFERENCE_VERSION,
        bootstrap_version=CLUSTER_BOOTSTRAP_VERSION,
        holm_version=HOLM_CORRECTION_VERSION,
        seed_derivation=BOOTSTRAP_SEED_DERIVATION,
        settings=DEFAULT_INFERENCE_SETTINGS,
        readiness=empty_readiness,
        formal_sample_event_ids=(),
        cluster_concentration=(),
        baseline=None,
        challengers=(),
    )
    return TokenBehaviorReport(
        manifest=TokenBehaviorManifest(
            protocol_version=PROTOCOL_VERSION,
            replay_engine_version=FOUNDATION_VERSION,
            replay_query_version=QUERY_VERSION,
            report_version=TOKEN_BEHAVIOR_REPORT_VERSION,
            candidate_version=TOKEN_BEHAVIOR_CANDIDATE_VERSION,
            inference_version=TOKEN_BEHAVIOR_INFERENCE_VERSION,
            virtual_strategy_version=VIRTUAL_STRATEGY_VERSION,
            entry_model_version=ENTRY_MODEL_VERSION,
            exit_model_version=EXIT_MODEL_VERSION,
            cost_model_version=COST_MODEL_VERSION,
            market_path_version=DECISION_MARKET_PATH_VERSION,
            code_revision=revision,
            working_tree_dirty=working_tree_dirty,
            generated_at=generated_at,
            dataset_since=filters.since,
            dataset_until_exclusive=filters.until,
            dataset_run_id=TOKEN_BEHAVIOR_DATASET_RUN_ID,
            dataset_content_fingerprint=TOKEN_BEHAVIOR_DATASET_CONTENT_FINGERPRINT,
            decision_input_fingerprint=dataset.input_fingerprint,
            market_path_fingerprint=decision_market_path_fingerprint(paths),
            strategy_versions=filters.strategy_versions,
            resolver_version=filters.resolver_version,
            required_horizons=filters.required_horizons,
            fallback_allowed=filters.allow_fallback,
            thresholds=None,
            bootstrap_iterations=DEFAULT_INFERENCE_SETTINGS.iterations,
            bootstrap_seed=DEFAULT_INFERENCE_SETTINGS.seed,
            bootstrap_confidence_level=DEFAULT_INFERENCE_SETTINGS.confidence_level,
            holm_family_alpha=DEFAULT_INFERENCE_SETTINGS.family_alpha,
            min_formal_episodes=MIN_FORMAL_EPISODES,
            min_formal_clusters=MIN_FORMAL_CLUSTERS,
            min_formal_weeks=MIN_FORMAL_WEEKS,
        ),
        dataset_episodes=len(dataset.episodes),
        eligible_episodes=len(dataset.eligible_episodes),
        out_of_scope_episodes=out_of_scope_count,
        formal_population_size=0,
        formal_sample_size=0,
        week_concentration=WeekConcentration(distinct_weeks=0, largest_week_pct=None),
        candidate_readiness=(),
        inference=empty_inference,
        week_sensitivity={},
        last_week_robustness={},
        final_verdict="insufficient_data",
        nominated_candidate=None,
        episode_results=(),
    )


def build_token_behavior_discovery_report(
    dataset: ReplayDataset,
    filters: ReplayFilters,
    token_history: dict[int, TokenHistoryContext],
    paths: tuple[DecisionMarketPath, ...],
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    costs: CostParameters = DEFAULT_COSTS,
) -> TokenBehaviorReport:
    revision = normalize_code_revision(code_revision)
    if (
        filters.since != TOKEN_BEHAVIOR_DATASET_SINCE
        or filters.until != TOKEN_BEHAVIOR_DATASET_UNTIL_EXCLUSIVE
    ):
        raise ValueError("formal token-behavior report requires the frozen dataset cohort window")
    if filters.strategy_versions != TOKEN_BEHAVIOR_STRATEGY_VERSIONS:
        raise ValueError("formal token-behavior report requires the registered strategy cohort")

    path_counts = Counter(path.decision_id for path in paths)
    duplicates = sorted(decision_id for decision_id, count in path_counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate market paths for decisions: {duplicates}")
    path_by_decision = {item.decision_id: item.path for item in paths}

    # The baseline is scoped to the frozen dataset's own 47 instruments (see
    # token_behavior_descriptors.py's module docstring) -- an episode
    # outside that instrument set is out of scope for this report entirely,
    # not a member of the population left with unresolved candidates.
    scoped_episodes = in_scope_episodes(dataset, token_history)
    out_of_scope_count = len(dataset.eligible_episodes) - len(scoped_episodes)

    descriptor_sample = _first_pass_descriptors(scoped_episodes, token_history)
    try:
        thresholds = freeze_thresholds(descriptor_sample)
    except InsufficientDescriptorDataError:
        # Not enough resolved descriptor values yet to freeze even one
        # candidate's median -- an expected early state (the frozen 47-
        # instrument cohort can be this small), never a crash. Every
        # candidate-dependent field stays empty/None; the funnel counts
        # collected so far are still reported honestly.
        return _insufficient_threshold_data_report(
            dataset=dataset,
            filters=filters,
            paths=paths,
            revision=revision,
            generated_at=generated_at,
            working_tree_dirty=working_tree_dirty,
            out_of_scope_count=out_of_scope_count,
        )

    results = tuple(
        evaluate_token_behavior_episode(episode, path_by_decision, token_history, thresholds, costs)
        for episode in scoped_episodes
    )
    formal_population = tuple(result for result in results if result.baseline_triggered)

    # challenger_inference.build_challenger_inference requires every episode
    # in the formal sample to be "completely paired" (baseline resolved AND
    # every one of the 4 candidates resolved) before it will report
    # formal_sample_ready, with zero tolerance by default. Candidate 3
    # (slow recovery) is deliberately, structurally unresolved for any
    # episode with no_prior_spike/missing_reference_price -- an expected,
    # pre-registered exclusion from ITS OWN population, not a data gap --
    # and candidates 1/2 can be legitimately unresolved too (insufficient
    # OHLCV coverage). Zero tolerance would make "insufficient_resolution"
    # essentially guaranteed regardless of real data quality. See
    # _structural_candidate_gap_count for exactly what is, and is not,
    # covered by the resulting tolerance.
    formal_sample_preview = formal_population[:MIN_FORMAL_EPISODES]
    structural_candidate_gaps = _structural_candidate_gap_count(formal_sample_preview)

    inference = build_challenger_inference(
        tuple(
            ChallengerEpisode(
                pump_event_id=result.pump_event_id,
                cluster_key=result.cluster_key,
                baseline_return_pct=result.baseline_net_return_pct,
                challenger_returns_pct=tuple(
                    (key, result.candidate_returns.get(key)) for key in CANDIDATE_VARIANT_KEYS
                ),
                baseline_triggered=result.baseline_triggered,
                challenger_triggered=tuple(
                    (key, result.candidate_gates.get(key) is False)
                    for key in CANDIDATE_VARIANT_KEYS
                ),
            )
            for result in formal_population
        ),
        CANDIDATE_VARIANT_KEYS,
        inference_version=TOKEN_BEHAVIOR_INFERENCE_VERSION,
        minimum_triggered_episodes=MIN_CHANGED_TRADES,
        max_unresolved_tolerance=structural_candidate_gaps,
        # This report's own, deliberately looser, pre-registered discovery
        # floor -- see the module-level comment on MIN_FORMAL_EPISODES for
        # why the shared default (replay.FORMAL_EPISODES=100/
        # MIN_FORMAL_CLUSTERS=30) is not achievable on this frozen dataset.
        directional_episodes=TOKEN_BEHAVIOR_DIRECTIONAL_EPISODES,
        formal_episodes=MIN_FORMAL_EPISODES,
        min_formal_clusters=MIN_FORMAL_CLUSTERS,
    )

    # Every gate feeding candidate nomination below -- including the "no
    # week over 70%" concentration cap, which the frozen contract states
    # explicitly "of the formal sample" -- must be computed over EXACTLY
    # the episodes the Holm-corrected statistics ran over
    # (inference.formal_sample_event_ids, capped at MIN_FORMAL_EPISODES/
    # first-chronological), never the full, possibly larger, formal_population.
    formal_sample_ids = set(inference.formal_sample_event_ids)
    formal_sample_population = tuple(
        result for result in formal_population if result.pump_event_id in formal_sample_ids
    )
    week_concentration = _week_concentration(formal_sample_population)

    week_sensitivity: dict[str, tuple[tuple[str, float], ...]] = {}
    for key in CANDIDATE_VARIANT_KEYS:
        observations: list[ClusterObservation] = []
        for result in formal_sample_population:
            challenger_return = result.candidate_returns.get(key)
            if challenger_return is None or result.baseline_net_return_pct is None:
                continue
            observations.append(
                ClusterObservation(
                    result.week_key, challenger_return - result.baseline_net_return_pct
                )
            )
        weeks = sorted({observation.cluster_key for observation in observations})
        week_sensitivity[key] = (
            leave_one_cluster_out_means(tuple(observations), tuple(weeks))
            if len(weeks) >= 2
            else ()
        )

    candidate_readiness = {
        key: _candidate_readiness(key, formal_sample_population) for key in CANDIDATE_VARIANT_KEYS
    }
    last_week_robustness = {
        key: _last_week_robustness(key, formal_sample_population) for key in CANDIDATE_VARIANT_KEYS
    }
    canonical_run = _is_canonical_run(filters, costs)
    formal_cluster_count = len({result.cluster_key for result in formal_population})
    final_verdict, nominated = _final_verdict(
        canonical_run=canonical_run,
        formal_population_size=len(formal_population),
        formal_cluster_count=formal_cluster_count,
        week_concentration=week_concentration,
        inference=inference,
        candidate_readiness=candidate_readiness,
        last_week_robustness=last_week_robustness,
    )

    return TokenBehaviorReport(
        manifest=TokenBehaviorManifest(
            protocol_version=PROTOCOL_VERSION,
            replay_engine_version=FOUNDATION_VERSION,
            replay_query_version=QUERY_VERSION,
            report_version=TOKEN_BEHAVIOR_REPORT_VERSION,
            candidate_version=TOKEN_BEHAVIOR_CANDIDATE_VERSION,
            inference_version=TOKEN_BEHAVIOR_INFERENCE_VERSION,
            virtual_strategy_version=VIRTUAL_STRATEGY_VERSION,
            entry_model_version=ENTRY_MODEL_VERSION,
            exit_model_version=EXIT_MODEL_VERSION,
            cost_model_version=COST_MODEL_VERSION,
            market_path_version=DECISION_MARKET_PATH_VERSION,
            code_revision=revision,
            working_tree_dirty=working_tree_dirty,
            generated_at=generated_at,
            dataset_since=filters.since,
            dataset_until_exclusive=filters.until,
            dataset_run_id=TOKEN_BEHAVIOR_DATASET_RUN_ID,
            dataset_content_fingerprint=TOKEN_BEHAVIOR_DATASET_CONTENT_FINGERPRINT,
            decision_input_fingerprint=dataset.input_fingerprint,
            market_path_fingerprint=decision_market_path_fingerprint(paths),
            strategy_versions=filters.strategy_versions,
            resolver_version=filters.resolver_version,
            required_horizons=filters.required_horizons,
            fallback_allowed=filters.allow_fallback,
            thresholds=thresholds,
            bootstrap_iterations=DEFAULT_INFERENCE_SETTINGS.iterations,
            bootstrap_seed=DEFAULT_INFERENCE_SETTINGS.seed,
            bootstrap_confidence_level=DEFAULT_INFERENCE_SETTINGS.confidence_level,
            holm_family_alpha=DEFAULT_INFERENCE_SETTINGS.family_alpha,
            min_formal_episodes=MIN_FORMAL_EPISODES,
            min_formal_clusters=MIN_FORMAL_CLUSTERS,
            min_formal_weeks=MIN_FORMAL_WEEKS,
        ),
        dataset_episodes=len(dataset.episodes),
        eligible_episodes=len(dataset.eligible_episodes),
        out_of_scope_episodes=out_of_scope_count,
        formal_population_size=len(formal_population),
        formal_sample_size=len(formal_sample_population),
        week_concentration=week_concentration,
        candidate_readiness=tuple(candidate_readiness[key] for key in CANDIDATE_VARIANT_KEYS),
        inference=inference,
        week_sensitivity=week_sensitivity,
        last_week_robustness=last_week_robustness,
        final_verdict=final_verdict,
        nominated_candidate=nominated,
        episode_results=results,
    )


# --- rendering ---


def render_json(report: TokenBehaviorReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True, allow_nan=False)


def render_markdown(report: TokenBehaviorReport) -> str:
    manifest = report.manifest
    lines = [
        "# Token-Behavior Discovery (Historical Pass, No Production Change)",
        "",
        f"Generated: {manifest.generated_at.isoformat()}",
        f"Code revision: `{manifest.code_revision}`",
        (
            f"Dataset: `{manifest.dataset_run_id}` "
            f"(fingerprint `{manifest.dataset_content_fingerprint}`)"
        ),
        (
            f"Scope: {manifest.dataset_since.isoformat()} <= decision "
            f"< {manifest.dataset_until_exclusive.isoformat()}"
        ),
        "",
        (
            f"> Formal inference status: `{report.inference.readiness.status}`. "
            f"Final verdict: `{report.final_verdict}`"
            + (f" (`{report.nominated_candidate}`)" if report.nominated_candidate else "")
            + ". This report never changes production score settings or authorizes real trading."
        ),
        "",
        "## Funnel",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Dataset episodes", report.dataset_episodes),
                ("Eligible episodes", report.eligible_episodes),
                (
                    "Out of scope (not one of the 47 frozen-dataset instruments)",
                    report.out_of_scope_episodes,
                ),
                ("Formal population (baseline-triggered, in scope)", report.formal_population_size),
                (
                    "Formal sample actually tested (Holm/bootstrap)",
                    report.formal_sample_size,
                ),
                ("Distinct UTC weeks", report.week_concentration.distinct_weeks),
                (
                    "Largest week share (%)",
                    format_number(report.week_concentration.largest_week_pct, 1, missing="n/a"),
                ),
            ],
        )
    )
    lines.extend(["", "## Frozen thresholds", ""])
    if manifest.thresholds is None:
        lines.append(
            "_Not enough resolved descriptor values in the baseline cohort to freeze "
            "even one candidate's median yet -- collecting, not a report failure._"
        )
    else:
        lines.extend(
            markdown_table(
                ("Threshold", "Value", "Sample size"),
                [
                    (
                        "Volatility median",
                        format_number(manifest.thresholds.volatility_median, 6),
                        manifest.thresholds.volatility_sample_size,
                    ),
                    (
                        "Recovery median (days)",
                        format_number(manifest.thresholds.recovery_median_days, 2),
                        manifest.thresholds.recovery_sample_size,
                    ),
                    (
                        "Listing age median (days)",
                        format_number(manifest.thresholds.listing_age_median_days, 1),
                        manifest.thresholds.listing_age_sample_size,
                    ),
                ],
            )
        )
    lines.extend(["", "## Candidate readiness (computed over the formal sample only)", ""])
    lines.extend(
        markdown_table(
            (
                "Candidate",
                "Resolved %",
                "Changed trades",
                "Changed assets",
                "Materiality OK",
                "Profit factor",
                "PF OK (>1)",
            ),
            [
                (
                    row.variant_key,
                    format_percentage(row.resolved_pct, 1),
                    row.changed_trades,
                    row.changed_assets,
                    "yes" if row.materiality_ok else "no",
                    format_number(row.profit_factor, 2, missing="no losses"),
                    "yes" if row.profit_factor_ok else "no",
                )
                for row in report.candidate_readiness
            ],
        )
    )
    lines.extend(["", "## Statistical result per candidate (Holm-corrected jointly)", ""])
    for challenger in report.inference.challengers:
        lines.extend([f"### `{challenger.variant_key}`", ""])
        lines.extend(
            markdown_table(
                ("Metric", "Value"),
                [
                    ("Verdict (statistical only)", challenger.verdict),
                    (
                        "Own expectancy CI",
                        f"[{challenger.strategy.estimate.lower_bound:.4f}, "
                        f"{challenger.strategy.estimate.upper_bound:.4f}]",
                    ),
                    ("Holm-adjusted p-value", f"{challenger.paired.holm_adjusted_p_value:.4f}"),
                    ("Holm rejected null", "yes" if challenger.paired.holm_rejected else "no"),
                    (
                        "Last-UTC-week robustness (sign only)",
                        report.last_week_robustness.get(challenger.variant_key, "n/a"),
                    ),
                ],
            )
        )
        lines.append("")
    lines.extend(
        [
            f"## Final verdict: `{report.final_verdict}`"
            + (
                f" -- nominated `{report.nominated_candidate}`"
                if report.nominated_candidate
                else ""
            ),
            "",
            (
                "At most one candidate may be nominated per this discovery pass. A "
                "`candidate` verdict registers this exact rule and a new forward cohort "
                "in a separate PR, starting strictly after this dataset's own cutoff, "
                "using a point-in-time snapshot of its input data -- never a re-fetched, "
                "recomputed history."
            ),
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the pre-registered token-behavior discovery pass"
    )
    parser.add_argument(
        "--dataset-root",
        type=Path,
        default=Path("backups/token-history/token_history_ohlcv_v1")
        / TOKEN_BEHAVIOR_DATASET_RUN_ID,
        help="root of the frozen token-history Parquet dataset run",
    )
    parser.add_argument("--resolver-version", default=RESOLVER_VERSION)
    parser.add_argument("--allow-fallback", action="store_true")
    parser.add_argument(
        "--taker-fee-bps-per-side",
        type=float,
        default=DEFAULT_COSTS.taker_fee_bps_per_side,
    )
    parser.add_argument(
        "--funding-cost-bps-per-8h",
        type=float,
        default=DEFAULT_COSTS.funding_cost_bps_per_8h,
    )
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty",
        action=argparse.BooleanOptionalAction,
        required=True,
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


async def _run(args: argparse.Namespace) -> str:
    from .exchange_registry import EXCHANGE_FACTORIES
    from .replay_repository import ReplayRepository
    from .virtual_market import fetch_decision_market_paths

    generated_at = datetime.now(UTC)
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for token-behavior-discovery-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")

    filters = ReplayFilters(
        since=TOKEN_BEHAVIOR_DATASET_SINCE,
        until=TOKEN_BEHAVIOR_DATASET_UNTIL_EXCLUSIVE,
        strategy_versions=TOKEN_BEHAVIOR_STRATEGY_VERSIONS,
        resolver_version=args.resolver_version,
        required_horizons=DEFAULT_REPLAY_HORIZONS,
        allow_fallback=args.allow_fallback,
    )
    costs = CostParameters(
        taker_fee_bps_per_side=args.taker_fee_bps_per_side,
        funding_cost_bps_per_8h=args.funding_cost_bps_per_8h,
    )
    token_history = load_token_history_index(args.dataset_root)

    repository = ReplayRepository.from_url(db_url)
    try:
        decisions = await repository.load(filters)
    finally:
        await repository.close()
    dataset = build_replay_dataset(decisions, filters)

    # Fetch market paths only for episodes in scope for this report (the
    # frozen dataset's own 47 instruments) -- fetching for every eligible
    # episode would waste network calls and risk operational failures
    # (unsupported exchange, fetch errors) on pump events this report
    # excludes from its population anyway.
    scoped_episodes = in_scope_episodes(dataset, token_history)
    selected = tuple(
        selection.decision
        for episode in scoped_episodes
        for selection in (select_score_policy(episode, SCORE_THRESHOLD_BASELINE_POLICY),)
        if selection.status == "selected" and selection.decision is not None
    )
    paths = await fetch_decision_market_paths(selected, EXCHANGE_FACTORIES)
    report = build_token_behavior_discovery_report(
        dataset,
        filters,
        token_history,
        paths,
        generated_at=generated_at,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
        costs=costs,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        output = asyncio.run(_run(args))
    except ReportWindowNotStartedError as exc:
        parser.error(str(exc))
        return
    sys.stdout.write(output)


if __name__ == "__main__":
    main()
