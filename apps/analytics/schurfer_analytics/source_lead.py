"""Pure, point-in-time source-lead event-study mechanics."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from statistics import fmean, median

from .clustered_inference import (
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_BOOTSTRAP_SEED,
    ClusterObservation,
    cluster_bootstrap_mean,
    cluster_bootstrap_mean_null_p_value,
    derived_seed,
    holm_step_down,
)
from .ohlcv import ONE_MINUTE_MS, Candle, next_timeframe_after
from .reporting import profit_factor

SOURCE_LEAD_VERSION = "source_lead_paired_confirmation_v1"
SOURCE_LEAD_COHORT_START = datetime(2026, 7, 24, tzinfo=UTC)
SOURCE_EXCHANGES = ("mexc", "gate")
EXECUTION_EXCHANGES = ("binance", "bybit")
ENTRY_DELAYS_MINUTES = (0, 1, 5)
LONG_HORIZONS_MINUTES = (1, 5, 15, 30, 60, 240)
PRIMARY_DELAY_MINUTES = 0
PRIMARY_HORIZON_MINUTES = 30
MAX_CONFIRMATION_LAG_MINUTES = 60
MAX_ROUND_TRIP_IMPACT_BPS = 20.0
DEFAULT_TAKER_FEE_BPS_PER_SIDE = 10.0
DEFAULT_FUNDING_COST_BPS_PER_8H = 5.0
MIN_INFERENCE_EPISODES = 5
MIN_INFERENCE_CLUSTERS = 2


@dataclass(frozen=True)
class SourceLeadObservation:
    exchange: str
    symbol: str
    identity_key: str | None
    unified_symbol: str | None
    market_type: str | None
    base_asset: str | None
    quote_asset: str | None
    settle_asset: str | None
    onboarded_at: datetime | None
    identity_conflict: bool
    first_seen_at: datetime
    first_change_pct: float
    first_price: float | None
    first_volume_24h_usd: float | None


@dataclass(frozen=True)
class SourceLeadEvent:
    event_id: int
    base: str
    episode: int
    first_seen_at: datetime
    closed_at: datetime | None
    observations: tuple[SourceLeadObservation, ...]

    @property
    def cluster_key(self) -> str:
        return f"base:{self.base.strip().upper()}"


@dataclass(frozen=True)
class SourceLeadCandidate:
    candidate_id: str
    event_id: int
    cluster_key: str
    base: str
    source_exchange: str
    execution_exchange: str
    exact_symbol: str
    source_at: datetime
    confirmation_at: datetime
    confirmation_lag_seconds: float
    source_change_pct: float
    source_volume_24h_usd: float | None


@dataclass(frozen=True)
class SourceLeadPath:
    candidate_id: str
    event_id: int
    exchange: str
    symbol: str
    status: str
    candles: tuple[Candle, ...]
    error: str | None = None


@dataclass(frozen=True)
class CandidateBuildResult:
    candidates: tuple[SourceLeadCandidate, ...]
    event_statuses: tuple[tuple[str, int], ...]
    route_statuses: tuple[tuple[str, str, str, int], ...]


@dataclass(frozen=True)
class SourceLeadOutcome:
    candidate_id: str
    event_id: int
    cluster_key: str
    base: str
    event_week: str
    source_exchange: str
    execution_exchange: str
    delay_minutes: int
    horizon_minutes: int
    status: str
    early_traded: bool
    confirmation_lag_seconds: float
    early_holding_minutes: float | None
    control_holding_minutes: float | None
    early_long_gross_pct: float | None
    early_long_net_pct: float | None
    confirmation_long_gross_pct: float | None
    confirmation_long_net_pct: float | None
    paired_long_delta_pct: float | None
    lead_capture_gross_pct: float | None
    confirmation_short_gross_pct: float | None
    confirmation_short_net_pct: float | None
    error: str | None = None


@dataclass(frozen=True)
class LaneMetrics:
    source_exchange: str
    execution_exchange: str
    lane: str
    delay_minutes: int
    horizon_minutes: int
    candidates: int
    resolved: int
    traded: int
    cash: int
    unresolved: int
    clusters: int
    mean_net_pct: float | None
    median_net_pct: float | None
    win_rate_pct: float | None
    profit_factor: float | None
    control_mean_net_pct: float | None
    paired_mean_delta_pct: float | None
    mean_lead_capture_gross_pct: float | None


@dataclass(frozen=True)
class PrimaryRouteInference:
    source_exchange: str
    execution_exchange: str
    episodes: int
    resolved: int
    clusters: int
    early_mean_net_pct: float | None
    control_mean_net_pct: float | None
    mean_lead_capture_gross_pct: float | None
    paired_mean_delta_pct: float | None
    paired_lower_95_pct: float | None
    paired_upper_95_pct: float | None
    weakest_leave_one_cluster_out_pct: float | None
    excluding_busiest_week_pct: float | None
    raw_p_value: float | None
    holm_adjusted_p_value: float | None
    holm_rejected: bool | None


def _count_rows(counter: Counter[str]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def _week(value: datetime) -> str:
    year, week, _ = value.astimezone(UTC).isocalendar()
    return f"{year}-W{week:02d}"


def _finite_positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def _identity_reason(observation: SourceLeadObservation, base: str) -> str | None:
    if observation.identity_conflict:
        return "identity_conflict"
    if not observation.identity_key or not observation.unified_symbol:
        return "missing_identity"
    if observation.market_type != "swap":
        return "not_swap"
    if (observation.base_asset or "").casefold() != base.casefold():
        return "base_mismatch"
    if (observation.quote_asset or "").upper() != "USDT":
        return "quote_not_usdt"
    if (observation.settle_asset or "").upper() != "USDT":
        return "settle_not_usdt"
    if not math.isfinite(observation.first_change_pct):
        return "invalid_source_change"
    if observation.first_seen_at.utcoffset() is None:
        return "naive_timestamp"
    return None


def source_lead_input_fingerprint(events: tuple[SourceLeadEvent, ...]) -> str:
    payload = [
        {
            "event_id": event.event_id,
            "base": event.base,
            "episode": event.episode,
            "first_seen_at": event.first_seen_at.astimezone(UTC).isoformat(),
            "closed_at": event.closed_at.astimezone(UTC).isoformat() if event.closed_at else None,
            "observations": [asdict(row) for row in event.observations],
        }
        for event in sorted(events, key=lambda row: row.event_id)
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def source_lead_path_fingerprint(paths: tuple[SourceLeadPath, ...]) -> str:
    payload = [asdict(path) for path in sorted(paths, key=lambda row: row.candidate_id)]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def build_source_lead_candidates(
    events: tuple[SourceLeadEvent, ...],
    *,
    until: datetime,
) -> CandidateBuildResult:
    if until.utcoffset() is None:
        raise ValueError("until must be timezone-aware")
    event_statuses: Counter[str] = Counter()
    route_statuses: Counter[tuple[str, str, str]] = Counter()
    candidates: list[SourceLeadCandidate] = []
    seen_ids: set[str] = set()

    for event in sorted(events, key=lambda row: (row.first_seen_at, row.event_id)):
        if event.event_id <= 0 or not event.base.strip():
            raise ValueError("source-lead events require positive ids and non-empty bases")
        if not event.observations:
            event_statuses["missing_source_attribution"] += 1
            continue
        normalized = tuple(
            sorted(
                event.observations,
                key=lambda row: (row.first_seen_at, row.exchange.strip().lower()),
            )
        )
        earliest = normalized[0].first_seen_at
        first = tuple(row for row in normalized if row.first_seen_at == earliest)
        if len(first) != 1:
            event_statuses["tied_first_source"] += 1
            continue
        source = first[0]
        source_exchange = source.exchange.strip().lower()
        if source_exchange not in SOURCE_EXCHANGES:
            event_statuses["other_first_source"] += 1
            continue
        source_reason = _identity_reason(source, event.base)
        if source_reason:
            event_statuses[f"invalid_source:{source_reason}"] += 1
            continue
        event_statuses[f"eligible_source:{source_exchange}"] += 1

        by_exchange = {row.exchange.strip().lower(): row for row in normalized}
        if len(by_exchange) != len(normalized):
            raise ValueError(f"duplicate exchange source rows for event {event.event_id}")
        for execution_exchange in EXECUTION_EXCHANGES:
            route = (source_exchange, execution_exchange)
            target = by_exchange.get(execution_exchange)
            if target is None:
                route_statuses[(*route, "no_confirmation")] += 1
                continue
            if target.first_seen_at <= source.first_seen_at:
                route_statuses[(*route, "not_later_confirmation")] += 1
                continue
            target_reason = _identity_reason(target, event.base)
            if target_reason:
                route_statuses[(*route, f"invalid_target:{target_reason}")] += 1
                continue
            if target.onboarded_at is not None and target.onboarded_at > source.first_seen_at:
                route_statuses[(*route, "target_not_onboarded_at_source")] += 1
                continue
            lag = (target.first_seen_at - source.first_seen_at).total_seconds()
            if lag > MAX_CONFIRMATION_LAG_MINUTES * 60:
                route_statuses[(*route, "confirmation_after_60m")] += 1
                continue
            confirmation_entry_ms = next_timeframe_after(
                int(target.first_seen_at.timestamp() * 1000), ONE_MINUTE_MS
            )
            mature_at = datetime.fromtimestamp(
                (confirmation_entry_ms + max(LONG_HORIZONS_MINUTES) * ONE_MINUTE_MS) / 1000,
                UTC,
            )
            if mature_at > until:
                route_statuses[(*route, "right_censored")] += 1
                continue
            candidate_id = f"{event.event_id}:{source_exchange}:{execution_exchange}"
            if candidate_id in seen_ids:
                raise ValueError(f"duplicate source-lead candidate: {candidate_id}")
            seen_ids.add(candidate_id)
            candidates.append(
                SourceLeadCandidate(
                    candidate_id=candidate_id,
                    event_id=event.event_id,
                    cluster_key=event.cluster_key,
                    base=event.base,
                    source_exchange=source_exchange,
                    execution_exchange=execution_exchange,
                    exact_symbol=target.unified_symbol or "",
                    source_at=source.first_seen_at,
                    confirmation_at=target.first_seen_at,
                    confirmation_lag_seconds=lag,
                    source_change_pct=source.first_change_pct,
                    source_volume_24h_usd=(
                        source.first_volume_24h_usd
                        if _finite_positive(source.first_volume_24h_usd)
                        else None
                    ),
                )
            )
            route_statuses[(*route, "candidate")] += 1

    return CandidateBuildResult(
        candidates=tuple(candidates),
        event_statuses=_count_rows(event_statuses),
        route_statuses=tuple(
            (*key, count)
            for key, count in sorted(
                route_statuses.items(),
                key=lambda item: (item[0][0], item[0][1], item[0][2]),
            )
        ),
    )


def source_lead_path_bounds(candidate: SourceLeadCandidate) -> tuple[int, int]:
    start_ms = next_timeframe_after(int(candidate.source_at.timestamp() * 1000), ONE_MINUTE_MS)
    confirmation_entry_ms = next_timeframe_after(
        int(candidate.confirmation_at.timestamp() * 1000), ONE_MINUTE_MS
    )
    return (
        start_ms,
        confirmation_entry_ms + max(LONG_HORIZONS_MINUTES) * ONE_MINUTE_MS,
    )


def _round_trip_cost_pct(
    holding_minutes: float,
    *,
    taker_fee_bps_per_side: float,
    funding_cost_bps_per_8h: float,
) -> float:
    cost_bps = (
        MAX_ROUND_TRIP_IMPACT_BPS
        + 2 * taker_fee_bps_per_side
        + funding_cost_bps_per_8h * holding_minutes / 480
    )
    return cost_bps / 100


def _unresolved_outcomes(
    candidate: SourceLeadCandidate,
    status: str,
    error: str,
) -> tuple[SourceLeadOutcome, ...]:
    return tuple(
        SourceLeadOutcome(
            candidate_id=candidate.candidate_id,
            event_id=candidate.event_id,
            cluster_key=candidate.cluster_key,
            base=candidate.base,
            event_week=_week(candidate.source_at),
            source_exchange=candidate.source_exchange,
            execution_exchange=candidate.execution_exchange,
            delay_minutes=delay,
            horizon_minutes=horizon,
            status=status,
            early_traded=False,
            confirmation_lag_seconds=candidate.confirmation_lag_seconds,
            early_holding_minutes=None,
            control_holding_minutes=None,
            early_long_gross_pct=None,
            early_long_net_pct=None,
            confirmation_long_gross_pct=None,
            confirmation_long_net_pct=None,
            paired_long_delta_pct=None,
            lead_capture_gross_pct=None,
            confirmation_short_gross_pct=None,
            confirmation_short_net_pct=None,
            error=error,
        )
        for delay in ENTRY_DELAYS_MINUTES
        for horizon in LONG_HORIZONS_MINUTES
    )


def evaluate_source_lead_candidate(
    candidate: SourceLeadCandidate,
    path: SourceLeadPath | None,
    *,
    taker_fee_bps_per_side: float = DEFAULT_TAKER_FEE_BPS_PER_SIDE,
    funding_cost_bps_per_8h: float = DEFAULT_FUNDING_COST_BPS_PER_8H,
) -> tuple[SourceLeadOutcome, ...]:
    if not math.isfinite(taker_fee_bps_per_side) or taker_fee_bps_per_side < 0:
        raise ValueError("taker fee must be finite and non-negative")
    if not math.isfinite(funding_cost_bps_per_8h):
        raise ValueError("funding cost must be finite")
    if path is None:
        return _unresolved_outcomes(candidate, "missing_path", "market path was not loaded")
    if path.candidate_id != candidate.candidate_id:
        raise ValueError("source-lead path identity mismatch")
    if path.exchange != candidate.execution_exchange or path.symbol != candidate.exact_symbol:
        raise ValueError("source-lead path instrument mismatch")
    if path.status != "complete":
        return _unresolved_outcomes(
            candidate,
            path.status,
            path.error or "market path is incomplete",
        )

    start_ms, end_ms = source_lead_path_bounds(candidate)
    expected = tuple(range(start_ms, end_ms, ONE_MINUTE_MS))
    by_ts = {candle.ts_ms: candle for candle in path.candles}
    if len(by_ts) != len(path.candles):
        return _unresolved_outcomes(candidate, "duplicate_candles", "duplicate candle timestamps")
    missing = [timestamp for timestamp in expected if timestamp not in by_ts]
    if missing:
        return _unresolved_outcomes(
            candidate,
            "path_gap",
            f"missing {len(missing)} required one-minute candles",
        )

    confirmation_entry_ms = next_timeframe_after(
        int(candidate.confirmation_at.timestamp() * 1000), ONE_MINUTE_MS
    )
    control_entry = by_ts[confirmation_entry_ms].open
    outcomes: list[SourceLeadOutcome] = []
    for delay in ENTRY_DELAYS_MINUTES:
        early_entry_ms = start_ms + delay * ONE_MINUTE_MS
        early_traded = early_entry_ms < int(candidate.confirmation_at.timestamp() * 1000)
        early_entry = by_ts[early_entry_ms].open if early_traded else None
        lead_capture = (
            (control_entry - early_entry) / early_entry * 100 if early_entry is not None else None
        )
        for horizon in LONG_HORIZONS_MINUTES:
            exit_candle_ms = confirmation_entry_ms + (horizon - 1) * ONE_MINUTE_MS
            exit_price = by_ts[exit_candle_ms].close
            control_gross = (exit_price - control_entry) / control_entry * 100
            control_holding = float(horizon)
            control_net = control_gross - _round_trip_cost_pct(
                control_holding,
                taker_fee_bps_per_side=taker_fee_bps_per_side,
                funding_cost_bps_per_8h=funding_cost_bps_per_8h,
            )
            short_gross = (control_entry - exit_price) / control_entry * 100
            short_net = short_gross - _round_trip_cost_pct(
                control_holding,
                taker_fee_bps_per_side=taker_fee_bps_per_side,
                funding_cost_bps_per_8h=funding_cost_bps_per_8h,
            )
            early_gross: float | None = None
            early_net = 0.0
            early_holding: float | None = None
            status = "missed_lead_cash"
            if early_entry is not None:
                early_holding = (exit_candle_ms + ONE_MINUTE_MS - early_entry_ms) / ONE_MINUTE_MS
                early_gross = (exit_price - early_entry) / early_entry * 100
                early_net = early_gross - _round_trip_cost_pct(
                    early_holding,
                    taker_fee_bps_per_side=taker_fee_bps_per_side,
                    funding_cost_bps_per_8h=funding_cost_bps_per_8h,
                )
                status = "complete"
            outcomes.append(
                SourceLeadOutcome(
                    candidate_id=candidate.candidate_id,
                    event_id=candidate.event_id,
                    cluster_key=candidate.cluster_key,
                    base=candidate.base,
                    event_week=_week(candidate.source_at),
                    source_exchange=candidate.source_exchange,
                    execution_exchange=candidate.execution_exchange,
                    delay_minutes=delay,
                    horizon_minutes=horizon,
                    status=status,
                    early_traded=early_traded,
                    confirmation_lag_seconds=candidate.confirmation_lag_seconds,
                    early_holding_minutes=early_holding,
                    control_holding_minutes=control_holding,
                    early_long_gross_pct=early_gross,
                    early_long_net_pct=early_net,
                    confirmation_long_gross_pct=control_gross,
                    confirmation_long_net_pct=control_net,
                    paired_long_delta_pct=(early_net - control_net),
                    lead_capture_gross_pct=lead_capture,
                    confirmation_short_gross_pct=short_gross,
                    confirmation_short_net_pct=short_net,
                )
            )
    return tuple(outcomes)


def _metrics(
    outcomes: tuple[SourceLeadOutcome, ...],
    *,
    lane: str,
) -> LaneMetrics:
    if not outcomes:
        raise ValueError("lane metrics require outcomes")
    first = outcomes[0]
    resolved = tuple(row for row in outcomes if row.early_long_net_pct is not None)
    if lane == "early_long":
        values = [row.early_long_net_pct for row in resolved if row.early_long_net_pct is not None]
        control = [
            row.confirmation_long_net_pct
            for row in resolved
            if row.confirmation_long_net_pct is not None
        ]
        deltas = [
            row.paired_long_delta_pct for row in resolved if row.paired_long_delta_pct is not None
        ]
        lead_capture = [
            row.lead_capture_gross_pct for row in resolved if row.lead_capture_gross_pct is not None
        ]
        traded = sum(row.early_traded for row in resolved)
        cash = sum(not row.early_traded for row in resolved)
    elif lane == "confirmation_short":
        values = [
            row.confirmation_short_net_pct
            for row in resolved
            if row.confirmation_short_net_pct is not None
        ]
        control = []
        deltas = []
        lead_capture = []
        traded = len(values)
        cash = 0
    else:
        raise ValueError(f"unsupported source-lead lane: {lane}")
    return LaneMetrics(
        source_exchange=first.source_exchange,
        execution_exchange=first.execution_exchange,
        lane=lane,
        delay_minutes=first.delay_minutes if lane == "early_long" else 0,
        horizon_minutes=first.horizon_minutes,
        candidates=len(outcomes),
        resolved=len(resolved),
        traded=traded,
        cash=cash,
        unresolved=len(outcomes) - len(resolved),
        clusters=len({row.cluster_key for row in outcomes}),
        mean_net_pct=fmean(values) if values else None,
        median_net_pct=median(values) if values else None,
        win_rate_pct=(sum(value > 0 for value in values) / len(values) * 100 if values else None),
        profit_factor=profit_factor(values),
        control_mean_net_pct=fmean(control) if control else None,
        paired_mean_delta_pct=fmean(deltas) if deltas else None,
        mean_lead_capture_gross_pct=fmean(lead_capture) if lead_capture else None,
    )


def build_lane_metrics(outcomes: tuple[SourceLeadOutcome, ...]) -> tuple[LaneMetrics, ...]:
    grouped: dict[tuple[str, str, int, int], list[SourceLeadOutcome]] = defaultdict(list)
    for outcome in outcomes:
        grouped[
            (
                outcome.source_exchange,
                outcome.execution_exchange,
                outcome.delay_minutes,
                outcome.horizon_minutes,
            )
        ].append(outcome)
    rows: list[LaneMetrics] = []
    for key, group in sorted(grouped.items()):
        rows.append(_metrics(tuple(group), lane="early_long"))
        if key[2] == PRIMARY_DELAY_MINUTES:
            rows.append(_metrics(tuple(group), lane="confirmation_short"))
    return tuple(rows)


def build_primary_inference(
    outcomes: tuple[SourceLeadOutcome, ...],
    *,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> tuple[PrimaryRouteInference, ...]:
    if bootstrap_iterations < 100:
        raise ValueError("bootstrap iterations must be at least 100")
    grouped: dict[tuple[str, str], list[SourceLeadOutcome]] = defaultdict(list)
    for outcome in outcomes:
        if (
            outcome.delay_minutes == PRIMARY_DELAY_MINUTES
            and outcome.horizon_minutes == PRIMARY_HORIZON_MINUTES
        ):
            grouped[(outcome.source_exchange, outcome.execution_exchange)].append(outcome)

    rows: list[PrimaryRouteInference] = []
    raw_p_values: dict[str, float] = {}
    for (source, execution), raw_group in sorted(grouped.items()):
        group = tuple(raw_group)
        resolved = tuple(row for row in group if row.paired_long_delta_pct is not None)
        early_values = [
            row.early_long_net_pct for row in resolved if row.early_long_net_pct is not None
        ]
        control_values = [
            row.confirmation_long_net_pct
            for row in resolved
            if row.confirmation_long_net_pct is not None
        ]
        delta_values = [
            row.paired_long_delta_pct for row in resolved if row.paired_long_delta_pct is not None
        ]
        lead_capture_values = [
            row.lead_capture_gross_pct for row in resolved if row.lead_capture_gross_pct is not None
        ]
        observations = tuple(
            ClusterObservation(row.cluster_key, row.paired_long_delta_pct)
            for row in resolved
            if row.paired_long_delta_pct is not None
        )
        lower: float | None = None
        upper: float | None = None
        weakest: float | None = None
        without_week: float | None = None
        raw_p: float | None = None
        if len(resolved) == len(group) and observations:
            label = f"{SOURCE_LEAD_VERSION}:{source}:{execution}:primary"
            estimate = cluster_bootstrap_mean(
                observations,
                iterations=bootstrap_iterations,
                seed=derived_seed(bootstrap_seed, label),
            ).estimate
            lower, upper = estimate.lower_bound, estimate.upper_bound
            cluster_keys = sorted({row.cluster_key for row in resolved})
            exclusions = [
                fmean(
                    row.paired_long_delta_pct
                    for row in resolved
                    if row.cluster_key != cluster and row.paired_long_delta_pct is not None
                )
                for cluster in cluster_keys
                if any(row.cluster_key != cluster for row in resolved)
            ]
            weakest = min(exclusions) if exclusions else None
            week_counts = Counter(row.event_week for row in resolved)
            busiest = sorted(week_counts, key=lambda key: (-week_counts[key], key))[0]
            remaining = [
                row.paired_long_delta_pct
                for row in resolved
                if row.event_week != busiest and row.paired_long_delta_pct is not None
            ]
            without_week = fmean(remaining) if remaining else None
            if (
                len(resolved) >= MIN_INFERENCE_EPISODES
                and len({row.cluster_key for row in resolved}) >= MIN_INFERENCE_CLUSTERS
            ):
                raw_p = cluster_bootstrap_mean_null_p_value(
                    observations,
                    iterations=bootstrap_iterations,
                    seed=derived_seed(bootstrap_seed, f"{label}:null"),
                )
                raw_p_values[f"{source}->{execution}"] = raw_p
        rows.append(
            PrimaryRouteInference(
                source_exchange=source,
                execution_exchange=execution,
                episodes=len(group),
                resolved=len(resolved),
                clusters=len({row.cluster_key for row in group}),
                early_mean_net_pct=fmean(early_values) if early_values else None,
                control_mean_net_pct=fmean(control_values) if control_values else None,
                mean_lead_capture_gross_pct=(
                    fmean(lead_capture_values) if lead_capture_values else None
                ),
                paired_mean_delta_pct=fmean(delta_values) if delta_values else None,
                paired_lower_95_pct=lower,
                paired_upper_95_pct=upper,
                weakest_leave_one_cluster_out_pct=weakest,
                excluding_busiest_week_pct=without_week,
                raw_p_value=raw_p,
                holm_adjusted_p_value=None,
                holm_rejected=None,
            )
        )
    if raw_p_values:
        holm = {row.key: row for row in holm_step_down(raw_p_values)}
        adjusted_rows: list[PrimaryRouteInference] = []
        for row in rows:
            key = f"{row.source_exchange}->{row.execution_exchange}"
            adjustment = holm.get(key)
            adjusted_rows.append(
                replace(
                    row,
                    holm_adjusted_p_value=adjustment.adjusted_p_value,
                    holm_rejected=adjustment.rejected,
                )
                if adjustment
                else row
            )
        rows = adjusted_rows
    return tuple(rows)
