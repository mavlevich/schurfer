"""Pure discovery analysis for unused early_momentum flow features.

This module deliberately does not change the running v4 strategy.  It asks a
smaller question: among already-comparable v4 paper trades, do point-in-time
flow features reconstructed from the exact source series describe a useful
filter-to-cash challenger?

The only candidate frozen by this discovery is a moderate 15-minute taker
imbalance band.  The bounds were selected from the pre-prospective discovery
window and are hypotheses for a later registered cohort, never promotion
evidence from this module itself.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from statistics import fmean, median
from typing import Any

from .early_momentum_net_evidence import FORMAL_COHORT_START
from .reporting import profit_factor

REPORT_VERSION = "early_momentum_unused_flow_features_v1"
DATASET_VERSION = "early_momentum_unused_flow_features_dataset_v1"
DISCOVERY_START = FORMAL_COHORT_START
# The database-clock boundary registered before the prospective v4 workers
# started.  No episode armed at or after this timestamp belongs to discovery.
DISCOVERY_END = datetime(2026, 8, 25, 6, 25, 0, 970709, tzinfo=UTC)
EXPECTED_BARS = 121
EXPECTED_GAP_SECONDS = 60.0

# One candidate only.  It was chosen after the discovery-only quartile read:
# the middle-positive band was positive while the most aggressive buy-flow
# quartile was negative.  Never retune these values on the later validation
# cohort; a different band is a different candidate/version.
CANDIDATE_VERSION = "moderate_15m_taker_imbalance_filter_v1"
CANDIDATE_IMBALANCE_MIN = 0.20
CANDIDATE_IMBALANCE_MAX = 0.50

FEATURE_NAMES = (
    "imbalance_15m",
    "imbalance_acceleration",
    "burst_imbalance_15m",
    "turnover_to_oi_15m",
)


@dataclass(frozen=True)
class RawFeatureRow:
    trade_id: int
    episode_id: str
    cluster_key: str
    source_exchange: str
    source_native_id: str
    decision_bucket: datetime
    entry_at: datetime
    exit_at: datetime
    net_pnl_pct: float
    net_pnl_usd: float
    bars_observed: int
    distinct_buckets: int
    first_bucket: datetime | None
    last_bucket: datetime | None
    max_gap_seconds: float | None
    complete_bars: int
    buy_15m: float | None
    sell_15m: float | None
    buy_prior: float | None
    sell_prior: float | None
    buy_burst_15m: float | None
    sell_burst_15m: float | None
    oi_value_latest: float | None


@dataclass(frozen=True)
class FeatureObservation:
    trade_id: int
    episode_id: str
    cluster_key: str
    source_exchange: str
    decision_bucket: datetime
    exit_at: datetime
    net_pnl_pct: float
    net_pnl_usd: float
    imbalance_15m: float
    imbalance_acceleration: float
    burst_imbalance_15m: float
    turnover_to_oi_15m: float


@dataclass(frozen=True)
class QuartileSummary:
    feature: str
    quartile: int
    trades: int
    minimum: float
    maximum: float
    mean_net_return_pct: float
    median_net_return_pct: float
    total_net_pnl_usd: float
    profit_factor: float | None


@dataclass(frozen=True)
class CandidateSummary:
    candidate_version: str
    imbalance_min_inclusive: float
    imbalance_max_exclusive: float
    baseline_trades: int
    selected_trades: int
    rejected_to_cash: int
    selected_mean_net_return_pct: float | None
    selected_median_net_return_pct: float | None
    selected_total_net_pnl_usd: float
    selected_profit_factor: float | None
    selected_clusters: int
    selected_utc_weeks: int


@dataclass(frozen=True)
class FlowFeatureAnalysis:
    report_version: str
    dataset_version: str
    discovery_start: datetime
    discovery_end: datetime
    raw_rows: int
    comparable_feature_rows: int
    exclusions: dict[str, int]
    dataset_fingerprint: str
    correlations: dict[str, float | None]
    quartiles: tuple[QuartileSummary, ...]
    candidate: CandidateSummary
    verdict: str
    verdict_reasons: tuple[str, ...]


def _week_key(value: datetime) -> str:
    year, week, _ = value.astimezone(UTC).isocalendar()
    return f"{year}-W{week:02d}"


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    value = numerator / denominator
    return value if math.isfinite(value) else None


def _quality_reason(row: RawFeatureRow) -> str | None:
    if row.bars_observed != EXPECTED_BARS or row.distinct_buckets != EXPECTED_BARS:
        return "not_exactly_121_distinct_bars"
    if row.first_bucket != row.decision_bucket - timedelta(minutes=120):
        return "wrong_window_start"
    if row.last_bucket != row.decision_bucket:
        return "wrong_window_end"
    if row.max_gap_seconds is None or not math.isclose(
        row.max_gap_seconds, EXPECTED_GAP_SECONDS, abs_tol=1e-6
    ):
        return "non_contiguous_window"
    if row.complete_bars != EXPECTED_BARS:
        return "incomplete_source_bar"
    totals = (row.buy_15m, row.sell_15m, row.buy_prior, row.sell_prior)
    if any(value is None or value < 0 or not math.isfinite(value) for value in totals):
        return "invalid_taker_notional"
    bursts = (row.buy_burst_15m, row.sell_burst_15m)
    if any(value is None or value < 0 or not math.isfinite(value) for value in bursts):
        return "invalid_burst_notional"
    if row.oi_value_latest is None or row.oi_value_latest <= 0:
        return "missing_or_invalid_oi_value"
    return None


def build_observations(
    rows: tuple[RawFeatureRow, ...],
) -> tuple[tuple[FeatureObservation, ...], dict[str, int]]:
    observations: list[FeatureObservation] = []
    exclusions: Counter[str] = Counter()
    for row in rows:
        reason = _quality_reason(row)
        if reason is not None:
            exclusions[reason] += 1
            continue
        assert row.buy_15m is not None
        assert row.sell_15m is not None
        assert row.buy_prior is not None
        assert row.sell_prior is not None
        assert row.buy_burst_15m is not None
        assert row.sell_burst_15m is not None
        assert row.oi_value_latest is not None
        imbalance = _safe_ratio(
            row.buy_15m - row.sell_15m,
            row.buy_15m + row.sell_15m,
        )
        prior_imbalance = _safe_ratio(
            row.buy_prior - row.sell_prior,
            row.buy_prior + row.sell_prior,
        )
        burst = _safe_ratio(
            row.buy_burst_15m - row.sell_burst_15m,
            row.buy_burst_15m + row.sell_burst_15m,
        )
        turnover = _safe_ratio(row.buy_15m + row.sell_15m, row.oi_value_latest)
        if None in (imbalance, prior_imbalance, burst, turnover):
            exclusions["zero_or_invalid_feature_denominator"] += 1
            continue
        assert imbalance is not None
        assert prior_imbalance is not None
        assert burst is not None
        assert turnover is not None
        observations.append(
            FeatureObservation(
                trade_id=row.trade_id,
                episode_id=row.episode_id,
                cluster_key=row.cluster_key,
                source_exchange=row.source_exchange,
                decision_bucket=row.decision_bucket,
                exit_at=row.exit_at,
                net_pnl_pct=row.net_pnl_pct,
                net_pnl_usd=row.net_pnl_usd,
                imbalance_15m=imbalance,
                imbalance_acceleration=imbalance - prior_imbalance,
                burst_imbalance_15m=burst,
                turnover_to_oi_15m=turnover,
            )
        )
    return (
        tuple(sorted(observations, key=lambda item: item.trade_id)),
        dict(sorted(exclusions.items())),
    )


def _correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    x_mean = fmean(xs)
    y_mean = fmean(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True))
    x_var = sum((x - x_mean) ** 2 for x in xs)
    y_var = sum((y - y_mean) ** 2 for y in ys)
    if x_var <= 0 or y_var <= 0:
        return None
    return numerator / math.sqrt(x_var * y_var)


def _quartiles(observations: tuple[FeatureObservation, ...]) -> tuple[QuartileSummary, ...]:
    summaries: list[QuartileSummary] = []
    for feature in FEATURE_NAMES:
        ordered = sorted(observations, key=lambda row: (getattr(row, feature), row.trade_id))
        buckets: list[list[FeatureObservation]] = [[], [], [], []]
        for index, row in enumerate(ordered):
            quartile = min(3, index * 4 // max(1, len(ordered)))
            buckets[quartile].append(row)
        for index, bucket in enumerate(buckets, start=1):
            if not bucket:
                continue
            values = [float(getattr(row, feature)) for row in bucket]
            returns = [row.net_pnl_pct for row in bucket]
            pnls = [row.net_pnl_usd for row in bucket]
            summaries.append(
                QuartileSummary(
                    feature=feature,
                    quartile=index,
                    trades=len(bucket),
                    minimum=min(values),
                    maximum=max(values),
                    mean_net_return_pct=fmean(returns),
                    median_net_return_pct=median(returns),
                    total_net_pnl_usd=sum(pnls),
                    profit_factor=profit_factor(pnls),
                )
            )
    return tuple(summaries)


def _fingerprint(rows: tuple[RawFeatureRow, ...]) -> str:
    payload: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: item.trade_id):
        item = asdict(row)
        for key, value in tuple(item.items()):
            if isinstance(value, datetime):
                item[key] = value.isoformat()
        payload.append(item)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def analyze(rows: tuple[RawFeatureRow, ...]) -> FlowFeatureAnalysis:
    observations, exclusions = build_observations(rows)
    correlations = {
        feature: _correlation(
            [float(getattr(row, feature)) for row in observations],
            [row.net_pnl_pct for row in observations],
        )
        for feature in FEATURE_NAMES
    }
    selected = tuple(
        row
        for row in observations
        if CANDIDATE_IMBALANCE_MIN <= row.imbalance_15m < CANDIDATE_IMBALANCE_MAX
    )
    selected_pnls = [row.net_pnl_usd for row in selected]
    selected_returns = [row.net_pnl_pct for row in selected]
    candidate = CandidateSummary(
        candidate_version=CANDIDATE_VERSION,
        imbalance_min_inclusive=CANDIDATE_IMBALANCE_MIN,
        imbalance_max_exclusive=CANDIDATE_IMBALANCE_MAX,
        baseline_trades=len(observations),
        selected_trades=len(selected),
        rejected_to_cash=len(observations) - len(selected),
        selected_mean_net_return_pct=fmean(selected_returns) if selected_returns else None,
        selected_median_net_return_pct=median(selected_returns) if selected_returns else None,
        selected_total_net_pnl_usd=sum(selected_pnls),
        selected_profit_factor=profit_factor(selected_pnls),
        selected_clusters=len({row.cluster_key for row in selected}),
        selected_utc_weeks=len({_week_key(row.exit_at) for row in selected}),
    )
    reasons: list[str] = []
    if len(observations) < 30:
        reasons.append(f"comparable_feature_rows_{len(observations)}_below_30")
    if len(selected) < 10:
        reasons.append(f"candidate_selected_trades_{len(selected)}_below_10")
    if candidate.selected_utc_weeks < 2:
        reasons.append(f"candidate_utc_weeks_{candidate.selected_utc_weeks}_below_2")
    # This is a viewed discovery window by construction.  Even a large,
    # positive sample would still require a newly registered prospective
    # cohort; this report is never allowed to emit a promotion verdict.
    reasons.append("discovery_only_requires_new_prospective_registration")
    return FlowFeatureAnalysis(
        report_version=REPORT_VERSION,
        dataset_version=DATASET_VERSION,
        discovery_start=DISCOVERY_START,
        discovery_end=DISCOVERY_END,
        raw_rows=len(rows),
        comparable_feature_rows=len(observations),
        exclusions=exclusions,
        dataset_fingerprint=_fingerprint(rows),
        correlations=correlations,
        quartiles=_quartiles(observations),
        candidate=candidate,
        verdict="discovery_candidate_only" if selected else "insufficient_data",
        verdict_reasons=tuple(reasons),
    )


__all__ = [
    "CANDIDATE_IMBALANCE_MAX",
    "CANDIDATE_IMBALANCE_MIN",
    "CANDIDATE_VERSION",
    "DISCOVERY_END",
    "DISCOVERY_START",
    "FeatureObservation",
    "FlowFeatureAnalysis",
    "RawFeatureRow",
    "analyze",
    "build_observations",
]
