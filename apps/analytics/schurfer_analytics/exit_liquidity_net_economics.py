"""`analysis/exit-liquidity-adjusted-net-economics-v1`.

Retrospective diagnostic: after replacing the decision-time MODELED exit
cost with the OBSERVED close-time executable quote, does `pump_short` v1
still have positive net economics? Full contract, exclusion funnel, and
formula rationale: `docs/research/exit-liquidity-adjusted-net-economics-
v1.md` -- read that first; this docstring only summarizes.

The adjusted-PnL formula deliberately never touches `Trade.exit_price` or
`Trade.exit_slippage_bps` -- those mean different things depending on
which accounting era closed the trade (see `exit_liquidity_calibration_
repository.py`'s own docstring, and #286), and naively recomputing gross
PnL from them would double-count the exit cost on every fresh-VWAP-capture
row. Instead: `adjusted_gross_pnl_usd` is computed directly from
`entry_price` and the OBSERVED `ask_vwap` at close time
(`TradeExitLiquidityObservation.ask_vwap`, never `Trade.exit_price`).

`FORMULA_VERSION = "ask_vwap_primitives_v2"` -- v1 (2026-08-25, never
shipped past colleague review) computed gross PnL from `entry_price` and
`observation.mid`, then charged `observation.ask_impact_bps` separately
against `filled_notional_usd` as a flat notional-scaled cost. That is only
equivalent to using `ask_vwap` directly when `mid == entry_price`, because
`ask_impact_bps` is measured relative to `mid`, not `entry_price` --
`ask_vwap = mid * (1 + ask_impact_bps / 10_000)`. On a short where the
price moved against the position (mid far above entry -- exactly the
trades this report cares most about), v1 systematically UNDERSTATED exit
cost and overstated PnL, worse the larger the move (colleague review,
2026-08-25, reproduced a $0.25 overstatement on a $50 position with a
50%-adverse move and 100bps impact). v2 reads `ask_vwap` directly, so this
class of error cannot occur no matter how far mid drifts from entry.

A positive verdict here authorizes only a new, untouched, forward-
registered cohort -- never live capital directly from this retrospective
number.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from statistics import mean, median
from typing import TYPE_CHECKING, Any

from schurfer_performance import (
    COST_MODEL_VERSION,
    LEGACY_ACCOUNTING_VERSION,
    PAPER_ACCOUNTING_VERSION,
)

from .clustered_inference import (
    CLUSTER_BOOTSTRAP_VERSION,
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_BOOTSTRAP_SEED,
    DEFAULT_CONFIDENCE_LEVEL,
    ClusterObservation,
    cluster_bootstrap_mean,
    leave_one_cluster_out_means,
)
from .exit_liquidity_calibration_report import (
    EXIT_LIQUIDITY_COHORT_START,
    MAX_EXIT_QUOTE_SKEW_SECONDS,
    ExitLiquidityFilters,
)
from .exit_liquidity_calibration_report import (
    _base as _asset_base,
)
from .exit_liquidity_calibration_report import (
    _duration_bucket as _duration_bucket_of,
)
from .exit_liquidity_calibration_report import (
    _spread_bucket as _spread_bucket_of,
)
from .reporting import (
    format_number,
    format_percentage,
    json_ready,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
)
from .reporting import (
    profit_factor as _profit_factor,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from .research_dataset_artifact import DatasetArtifactManifest

REPORT_CONTRACT = "exit_liquidity_net_economics_v1"
FORMULA_VERSION = "ask_vwap_primitives_v2"
DECISION_SAMPLE_SIZE = 100
MINIMUM_CLUSTERS = 30
MINIMUM_UTC_WEEKS = 4

_SUPPORTED_ACCOUNTING_VERSIONS = (LEGACY_ACCOUNTING_VERSION, PAPER_ACCOUNTING_VERSION)
_KNOWN_EXIT_REASONS = ("initial_sl", "max_hold", "no_progress", "trailing_stop")

# Frozen contract scope (docs/research/exit-liquidity-adjusted-net-
# economics-v1.md): this report is `pump_short` v1 ONLY. A real, distinct
# registered variant, ("pump_short", "1_market_quality") (see trader.py's
# own comment on journal.strategy_identity parsing), is deliberately NOT
# included -- mixing it in without evidence the two are economically
# equivalent would be exactly the strategy-mixing bug this allowlist
# exists to prevent (colleague review, 2026-08-25). Broadening this needs
# its own explicit contract change, not a silent expansion.
ALLOWED_STRATEGY_IDENTITIES = (("pump_short", "1"),)

# Empirically anchored, not the exact deploy instant: the 2026-08-20 audit
# (exit_liquidity_calibration_report.py's own step-0 investigation) found
# the malformed-symbol identity bug confined to 15:49:52-17:08:44 UTC that
# day, PR #263 (commit 29ccd71) merged the same day, and zero occurrences
# on any other day (checked 2026-07-31 through 2026-08-24). End-of-day UTC
# on 2026-08-20 is a safe, simple boundary strictly after the last known
# occurrence -- exact deploy-completion time was not independently
# recorded, so this does not claim minute-level precision.
_POST_IDENTITY_FIX_CUTOFF = datetime(2026, 8, 21, tzinfo=UTC)


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def _finite_nonnegative(value: float | None) -> bool:
    return _finite(value) and value >= 0  # type: ignore[operator]


@dataclass(frozen=True)
class NetEconomicsRow:
    trade_id: int
    episode_id: str | None
    strategy_name: str
    strategy_version: str
    symbol: str
    exchange: str
    side: str
    entry_at: datetime
    exit_at: datetime
    exit_reason: str | None
    size_usd: float
    leverage: float
    entry_price: float
    exit_price: float | None
    recorded_gross_pnl_usd: float | None
    recorded_net_pnl_usd: float | None
    fees_usd: float
    funding_usd: float
    entry_slippage_bps: float | None
    modeled_exit_bps: float | None
    accounting_version: str
    accounting_status: str
    accounting_error: str | None
    observation_id: int | None
    observed_at: datetime | None
    observation_exchange: str | None
    observation_symbol: str | None
    observation_status: str | None
    requested_notional_usd: float | None
    filled_notional_usd: float | None
    observed_mid: float | None
    observed_spread_bps: float | None
    observed_exit_bps: float | None
    observed_ask_vwap: float | None
    latency_ms: int | None
    error: str | None


@dataclass(frozen=True)
class ExclusionFlags:
    missing_observation: bool
    not_sampled: bool
    observation_error: bool
    identity_mismatch: bool
    malformed_identity_post_263: bool
    missing_observed_at: bool
    stale_quote: bool
    missing_or_invalid_notional_fields: bool
    requested_notional_mismatch: bool
    insufficient_visible_depth: bool
    missing_or_invalid_quote_fields: bool
    unsupported_accounting_version: bool
    incomplete_accounting: bool

    @property
    def blocks_adjusted_pnl(self) -> bool:
        """Any of these makes `adjusted_net_pnl_usd` uncomputable at all --
        `incomplete_accounting` alone does not (it only blocks pairing
        against `recorded_net_pnl_usd`, see this row's own docstring in
        `docs/research/exit-liquidity-adjusted-net-economics-v1.md`)."""
        return (
            self.missing_observation
            or self.not_sampled
            or self.observation_error
            or self.identity_mismatch
            or self.missing_observed_at
            or self.stale_quote
            or self.missing_or_invalid_notional_fields
            or self.requested_notional_mismatch
            or self.insufficient_visible_depth
            or self.missing_or_invalid_quote_fields
            or self.unsupported_accounting_version
        )

    @property
    def primary_reason(self) -> str | None:
        if self.missing_observation:
            return "missing_observation"
        if self.not_sampled:
            return "not_sampled"
        if self.observation_error:
            return "observation_error"
        if self.malformed_identity_post_263:
            return "malformed_identity_post_263"
        if self.identity_mismatch:
            return "identity_mismatch"
        if self.missing_observed_at:
            return "missing_observed_at"
        if self.stale_quote:
            return "stale_quote"
        if self.missing_or_invalid_notional_fields:
            return "missing_or_invalid_notional_fields"
        if self.requested_notional_mismatch:
            return "requested_notional_mismatch"
        if self.insufficient_visible_depth:
            return "insufficient_visible_depth"
        if self.missing_or_invalid_quote_fields:
            return "missing_or_invalid_quote_fields"
        if self.unsupported_accounting_version:
            return "unsupported_accounting_version"
        if self.incomplete_accounting:
            return "incomplete_accounting"
        return None


def compute_exclusion_flags(row: NetEconomicsRow) -> ExclusionFlags:
    missing_observation = row.observation_id is None
    # Structural completeness of the observation itself -- checked BEFORE
    # anything that reads its fields, and each condition below is written
    # to fail closed (exclude) rather than silently pass when a field is
    # None, unlike the first version of this function (colleague review,
    # 2026-08-25: `filled_notional_usd=None` and `observed_at=None` both
    # reached `compute_adjusted_net_pnl_usd` uncaught, one crashing an
    # assert and the other being silently treated as a fresh quote).
    not_sampled = not missing_observation and row.observation_status != "sampled"
    observation_error = not missing_observation and row.error is not None
    identity_mismatch = (not missing_observation) and (
        row.observation_exchange != row.exchange or row.observation_symbol != row.symbol
    )
    malformed_identity_post_263 = identity_mismatch and row.entry_at >= _POST_IDENTITY_FIX_CUTOFF
    missing_observed_at = not missing_observation and row.observed_at is None
    stale_quote = (
        not missing_observation
        and row.observed_at is not None
        and abs((row.exit_at - row.observed_at).total_seconds()) > MAX_EXIT_QUOTE_SKEW_SECONDS
    )
    missing_or_invalid_notional_fields = not missing_observation and not (
        _finite(row.requested_notional_usd)
        and row.requested_notional_usd > 0  # type: ignore[operator]
        and _finite(row.filled_notional_usd)
        and row.filled_notional_usd >= 0  # type: ignore[operator]
    )
    requested_notional_mismatch = (
        not missing_observation
        and not missing_or_invalid_notional_fields
        and abs(row.requested_notional_usd - row.size_usd) > 0.01  # type: ignore[operator]
    )
    insufficient_visible_depth = (
        not missing_observation
        and not missing_or_invalid_notional_fields
        and row.filled_notional_usd + 0.01 < row.requested_notional_usd  # type: ignore[operator]
    )
    missing_or_invalid_quote_fields = not missing_observation and not (
        _finite(row.observed_mid)
        and row.observed_mid > 0  # type: ignore[operator]
        and _finite(row.observed_ask_vwap)
        and row.observed_ask_vwap > 0  # type: ignore[operator]
        and _finite_nonnegative(row.observed_exit_bps)
        and _finite_nonnegative(row.entry_slippage_bps)
    )
    unsupported_accounting_version = row.accounting_version not in _SUPPORTED_ACCOUNTING_VERSIONS
    incomplete_accounting = row.accounting_status != "complete"
    return ExclusionFlags(
        missing_observation=missing_observation,
        not_sampled=not_sampled,
        observation_error=observation_error,
        identity_mismatch=identity_mismatch,
        malformed_identity_post_263=malformed_identity_post_263,
        missing_observed_at=missing_observed_at,
        stale_quote=stale_quote,
        missing_or_invalid_notional_fields=missing_or_invalid_notional_fields,
        requested_notional_mismatch=requested_notional_mismatch,
        insufficient_visible_depth=insufficient_visible_depth,
        missing_or_invalid_quote_fields=missing_or_invalid_quote_fields,
        unsupported_accounting_version=unsupported_accounting_version,
        incomplete_accounting=incomplete_accounting,
    )


def compute_adjusted_net_pnl_usd(row: NetEconomicsRow) -> float:
    """Only ever called on a row whose flags.blocks_adjusted_pnl is False
    (all fields this asserts non-None are guaranteed present by that
    check). Reads `observed_ask_vwap` directly -- never `observed_mid` plus
    a separately-charged bps adjustment (see `FORMULA_VERSION`'s own
    changelog above for why that was wrong), and never `Trade.exit_price`/
    `exit_slippage_bps` (see this module's top-of-file docstring)."""
    assert row.observed_ask_vwap is not None
    assert row.entry_slippage_bps is not None
    if row.side != "short":
        raise ValueError(f"trade {row.trade_id}: only short is supported by this formula")

    adjusted_gross_pnl_usd = (
        row.size_usd * (row.entry_price - row.observed_ask_vwap) / row.entry_price
    )
    entry_cost_usd = row.size_usd * row.entry_slippage_bps / 10_000
    return adjusted_gross_pnl_usd - entry_cost_usd - row.fees_usd - row.funding_usd


def _utc_week(dt: datetime) -> str:
    year, week, _weekday = dt.astimezone(UTC).isocalendar()
    return f"{year}-W{week:02d}"


def normalize_exit_reason(raw: str | None) -> tuple[str, str | None]:
    """Returns `(normalized_reason, parameters)`. An unrecognized leading
    token stays `unknown` rather than inventing a new category (same
    convention `early_momentum_net_evidence.py` already uses)."""
    text = (raw or "").strip()
    if not text:
        return "unknown", None
    token, _, raw_rest = text.partition(" ")
    params: str | None = raw_rest.strip() or None
    if token in _KNOWN_EXIT_REASONS:
        return token, params
    return "unknown", text


@dataclass(frozen=True)
class ComparablePair:
    trade_id: int
    cluster: str
    utc_week: str
    exchange: str
    strategy_name: str
    strategy_version: str
    normalized_exit_reason: str
    exit_reason_params: str | None
    spread_bucket: str
    duration_bucket: str
    leverage: float
    adjusted_net_pnl_usd: float
    recorded_net_pnl_usd: float
    paired_delta_usd: float
    exit_at: datetime


@dataclass(frozen=True)
class CoverageRow:
    trade_id: int
    flags: ExclusionFlags
    primary_reason: str | None
    adjusted_net_pnl_usd: float | None
    comparable: ComparablePair | None


def build_coverage(rows: tuple[NetEconomicsRow, ...]) -> tuple[CoverageRow, ...]:
    if len({row.trade_id for row in rows}) != len(rows):
        raise ValueError("duplicate trade rows in exit-liquidity net-economics input")
    coverage: list[CoverageRow] = []
    for row in rows:
        flags = compute_exclusion_flags(row)
        if flags.blocks_adjusted_pnl:
            coverage.append(
                CoverageRow(
                    trade_id=row.trade_id,
                    flags=flags,
                    primary_reason=flags.primary_reason,
                    adjusted_net_pnl_usd=None,
                    comparable=None,
                )
            )
            continue
        adjusted = compute_adjusted_net_pnl_usd(row)
        comparable = None
        if not flags.incomplete_accounting:
            assert row.recorded_net_pnl_usd is not None
            normalized_reason, params = normalize_exit_reason(row.exit_reason)
            comparable = ComparablePair(
                trade_id=row.trade_id,
                cluster=_asset_base(row.symbol),
                utc_week=_utc_week(row.exit_at),
                exchange=row.exchange,
                strategy_name=row.strategy_name,
                strategy_version=row.strategy_version,
                normalized_exit_reason=normalized_reason,
                exit_reason_params=params,
                spread_bucket=(
                    _spread_bucket_of(row.observed_spread_bps)
                    if row.observed_spread_bps is not None
                    else "unknown"
                ),
                duration_bucket=_duration_bucket_of(
                    (row.exit_at - row.entry_at).total_seconds() / 60
                ),
                leverage=row.leverage,
                adjusted_net_pnl_usd=adjusted,
                recorded_net_pnl_usd=row.recorded_net_pnl_usd,
                paired_delta_usd=adjusted - row.recorded_net_pnl_usd,
                exit_at=row.exit_at,
            )
        coverage.append(
            CoverageRow(
                trade_id=row.trade_id,
                flags=flags,
                primary_reason=flags.primary_reason,
                adjusted_net_pnl_usd=adjusted,
                comparable=comparable,
            )
        )
    return tuple(coverage)


def _max_drawdown_and_losing_streak(pairs: tuple[ComparablePair, ...]) -> tuple[float, int]:
    ordered = sorted(pairs, key=lambda p: (p.exit_at, p.trade_id))
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    streak = 0
    worst_streak = 0
    for pair in ordered:
        equity += pair.adjusted_net_pnl_usd
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        if pair.adjusted_net_pnl_usd < 0:
            streak += 1
            worst_streak = max(worst_streak, streak)
        else:
            streak = 0
    return max_drawdown, worst_streak


@dataclass(frozen=True)
class NetEconomicsMetrics:
    comparable_count: int
    asset_clusters: int
    utc_weeks: int
    largest_cluster_share_pct: float
    busiest_week_share_pct: float
    total_recorded_net_pnl_usd: float
    mean_recorded_net_pnl_usd: float
    median_recorded_net_pnl_usd: float
    total_adjusted_net_pnl_usd: float
    mean_adjusted_net_pnl_usd: float
    median_adjusted_net_pnl_usd: float
    mean_paired_delta_usd: float
    median_paired_delta_usd: float
    win_rate_pct: float
    profit_factor: float | None
    max_drawdown_usd: float
    worst_losing_streak: int


def _metrics(pairs: tuple[ComparablePair, ...]) -> NetEconomicsMetrics | None:
    if not pairs:
        return None
    recorded = [p.recorded_net_pnl_usd for p in pairs]
    adjusted = [p.adjusted_net_pnl_usd for p in pairs]
    deltas = [p.paired_delta_usd for p in pairs]
    cluster_counts = Counter(p.cluster for p in pairs)
    week_counts = Counter(p.utc_week for p in pairs)
    max_drawdown, worst_streak = _max_drawdown_and_losing_streak(pairs)
    return NetEconomicsMetrics(
        comparable_count=len(pairs),
        asset_clusters=len(cluster_counts),
        utc_weeks=len(week_counts),
        largest_cluster_share_pct=max(cluster_counts.values()) / len(pairs) * 100,
        busiest_week_share_pct=max(week_counts.values()) / len(pairs) * 100,
        total_recorded_net_pnl_usd=sum(recorded),
        mean_recorded_net_pnl_usd=mean(recorded),
        median_recorded_net_pnl_usd=median(recorded),
        total_adjusted_net_pnl_usd=sum(adjusted),
        mean_adjusted_net_pnl_usd=mean(adjusted),
        median_adjusted_net_pnl_usd=median(adjusted),
        mean_paired_delta_usd=mean(deltas),
        median_paired_delta_usd=median(deltas),
        win_rate_pct=sum(value > 0 for value in adjusted) / len(adjusted) * 100,
        profit_factor=_profit_factor(adjusted),
        max_drawdown_usd=max_drawdown,
        worst_losing_streak=worst_streak,
    )


@dataclass(frozen=True)
class RobustnessResult:
    bootstrap_point_estimate: float
    bootstrap_lower_bound: float
    bootstrap_upper_bound: float
    bootstrap_clusters: int
    leave_best_asset_out_cluster: str
    leave_best_asset_out_mean_usd: float
    leave_one_week_out: tuple[tuple[str, float], ...]


def _robustness(pairs: tuple[ComparablePair, ...]) -> RobustnessResult | None:
    if not pairs:
        return None
    observations = tuple(
        ClusterObservation(cluster_key=p.cluster, value=p.adjusted_net_pnl_usd) for p in pairs
    )
    bootstrap = cluster_bootstrap_mean(observations)

    totals_by_cluster: dict[str, float] = defaultdict(float)
    for p in pairs:
        totals_by_cluster[p.cluster] += p.adjusted_net_pnl_usd
    best_cluster = max(totals_by_cluster, key=lambda key: totals_by_cluster[key])
    remaining = [p.adjusted_net_pnl_usd for p in pairs if p.cluster != best_cluster]
    leave_best_asset_out_mean = mean(remaining) if remaining else float("nan")

    week_observations = tuple(
        ClusterObservation(cluster_key=p.utc_week, value=p.adjusted_net_pnl_usd) for p in pairs
    )
    weeks = tuple(sorted({p.utc_week for p in pairs}))
    leave_one_week_out = (
        leave_one_cluster_out_means(week_observations, weeks) if len(weeks) > 1 else ()
    )

    return RobustnessResult(
        bootstrap_point_estimate=bootstrap.estimate.point_estimate,
        bootstrap_lower_bound=bootstrap.estimate.lower_bound,
        bootstrap_upper_bound=bootstrap.estimate.upper_bound,
        bootstrap_clusters=bootstrap.estimate.clusters,
        leave_best_asset_out_cluster=best_cluster,
        leave_best_asset_out_mean_usd=leave_best_asset_out_mean,
        leave_one_week_out=leave_one_week_out,
    )


@dataclass(frozen=True)
class SegmentMetrics:
    dimension: str
    bucket: str
    observations: int
    mean_adjusted_net_pnl_usd: float
    win_rate_pct: float


def _segment(dimension: str, bucket: str, pairs: list[ComparablePair]) -> SegmentMetrics:
    adjusted = [p.adjusted_net_pnl_usd for p in pairs]
    return SegmentMetrics(
        dimension=dimension,
        bucket=bucket,
        observations=len(pairs),
        mean_adjusted_net_pnl_usd=mean(adjusted),
        win_rate_pct=sum(value > 0 for value in adjusted) / len(adjusted) * 100,
    )


def _segments(pairs: tuple[ComparablePair, ...]) -> tuple[SegmentMetrics, ...]:
    dimensions: dict[str, Callable[[ComparablePair], str]] = {
        "strategy": lambda p: f"{p.strategy_name}@{p.strategy_version}",
        "exchange": lambda p: p.exchange,
        "exit_reason": lambda p: p.normalized_exit_reason,
        "close_spread": lambda p: p.spread_bucket,
        "duration": lambda p: p.duration_bucket,
        "leverage": lambda p: f"{p.leverage:g}x",
    }
    segments: list[SegmentMetrics] = []
    for dimension, key_fn in dimensions.items():
        grouped: dict[str, list[ComparablePair]] = defaultdict(list)
        for pair in pairs:
            grouped[key_fn(pair)].append(pair)
        segments.extend(
            _segment(dimension, bucket, group) for bucket, group in sorted(grouped.items())
        )
    return tuple(segments)


def _fingerprint(rows: tuple[NetEconomicsRow, ...]) -> str:
    payload = json.dumps(
        json_ready([asdict(row) for row in rows]),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class NetEconomicsReport:
    manifest: dict[str, Any]
    readiness: dict[str, Any]
    exclusion_reasons: dict[str, int]
    metrics: NetEconomicsMetrics | None
    robustness: RobustnessResult | None
    segments: tuple[SegmentMetrics, ...]
    verdict: str
    diagnostic: str | None


def build_net_economics_report(
    rows: tuple[NetEconomicsRow, ...],
    filters: ExitLiquidityFilters,
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    source_artifact: dict[str, Any] | None = None,
) -> NetEconomicsReport:
    coverage = build_coverage(rows)
    exclusions: Counter[str] = Counter(
        c.primary_reason for c in coverage if c.primary_reason is not None
    )
    pairs = tuple(c.comparable for c in coverage if c.comparable is not None)
    metrics = _metrics(pairs)
    robustness = _robustness(pairs)
    segments = _segments(pairs)

    count = len(pairs)
    clusters = len({p.cluster for p in pairs})
    weeks = len({p.utc_week for p in pairs})
    diagnostic: str | None = None

    if metrics is None:
        # No comparable trades at all -- nothing to reject or confirm.
        verdict = "insufficient_data"
    else:
        negative = metrics.mean_adjusted_net_pnl_usd <= 0 or (
            metrics.profit_factor is not None and metrics.profit_factor <= 1
        )
        if count < DECISION_SAMPLE_SIZE:
            # Below the raw comparable-count floor, a negative point
            # estimate is a lead, not a terminal verdict -- 5 trades cannot
            # support a confident FAIL any more than a confident PASS.
            # Recorded as a diagnostic so a reader isn't left wondering
            # why an obviously-bad-looking number didn't fail (hybrid
            # verdict design, colleague review, 2026-08-25).
            verdict = "insufficient_data"
            if negative:
                diagnostic = "negative_point_estimate"
        elif negative:
            # >= 100 comparable trades AND negative: terminal FAIL
            # regardless of asset/week diversity -- a clearly bad result on
            # a properly-sized sample needs no further diversity evidence
            # to reject. Diversity numbers are still recorded either way in
            # readiness.evidence_floor, never hidden by this verdict.
            verdict = "fail"
        elif clusters < MINIMUM_CLUSTERS or weeks < MINIMUM_UTC_WEEKS:
            # Positive but not diversified enough to trust -- only a
            # positive result needs the full evidence floor.
            verdict = "insufficient_data"
        else:
            assert robustness is not None
            interval_crosses_zero = robustness.bootstrap_lower_bound <= 0
            best_asset_flip = robustness.leave_best_asset_out_mean_usd <= 0
            week_flip = any(value <= 0 for _week, value in robustness.leave_one_week_out)
            if interval_crosses_zero or best_asset_flip or week_flip:
                verdict = "fragile_positive"
            else:
                verdict = "historical_positive_requires_forward_confirmation"

    return NetEconomicsReport(
        manifest={
            "contract": REPORT_CONTRACT,
            "formula_version": FORMULA_VERSION,
            "cost_model_version": COST_MODEL_VERSION,
            "allowed_strategy_identities": [list(pair) for pair in ALLOWED_STRATEGY_IDENTITIES],
            "bootstrap": {
                "version": CLUSTER_BOOTSTRAP_VERSION,
                "seed": DEFAULT_BOOTSTRAP_SEED,
                "iterations": DEFAULT_BOOTSTRAP_ITERATIONS,
                "confidence_level": DEFAULT_CONFIDENCE_LEVEL,
            },
            "generated_at": generated_at,
            "code_revision": normalize_code_revision(code_revision),
            "working_tree_dirty": working_tree_dirty,
            "since": filters.since,
            "until": filters.until,
            "input_fingerprint": _fingerprint(rows),
            "funding_is_modeled_not_observed": (
                "funding_usd is calculate_performance()'s fixed conservative rate model, "
                "not real per-trade signed exchange funding -- this codebase does not "
                "capture that anywhere"
            ),
            "adjusted_pnl_never_reads_exit_price_or_exit_slippage_bps": True,
            "source_artifact": source_artifact,
        },
        readiness={
            "closed_short_paper_trades": len(rows),
            "accounting_complete": sum(row.accounting_status == "complete" for row in rows),
            "quote_captured": sum(row.observation_id is not None for row in rows),
            "comparable": count,
            "evidence_floor": {
                "comparable_required": DECISION_SAMPLE_SIZE,
                "clusters_required": MINIMUM_CLUSTERS,
                "utc_weeks_required": MINIMUM_UTC_WEEKS,
                "comparable_actual": count,
                "clusters_actual": clusters,
                "utc_weeks_actual": weeks,
            },
        },
        exclusion_reasons=dict(exclusions),
        metrics=metrics,
        robustness=robustness,
        segments=segments,
        verdict=verdict,
        diagnostic=diagnostic,
    )


def render_json(report: NetEconomicsReport) -> str:
    payload = {
        "manifest": json_ready(report.manifest),
        "readiness": report.readiness,
        "exclusion_reasons": report.exclusion_reasons,
        "metrics": asdict(report.metrics) if report.metrics else None,
        "robustness": asdict(report.robustness) if report.robustness else None,
        "segments": [asdict(segment) for segment in report.segments],
        "verdict": report.verdict,
        "diagnostic": report.diagnostic,
    }
    return json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"


def render_markdown(report: NetEconomicsReport) -> str:
    manifest = report.manifest
    lines = [
        "# Exit-Liquidity Adjusted Net Economics",
        "",
        f"Generated: {manifest['generated_at'].isoformat()}",
        f"Code revision: `{manifest['code_revision']}`",
        f"Working tree dirty: {'yes' if manifest['working_tree_dirty'] else 'no'}",
        f"Contract: `{manifest['contract']}`",
        f"Dataset: {manifest['since'].isoformat()} <= exit < {manifest['until'].isoformat()}",
        f"Input fingerprint: `{manifest['input_fingerprint']}`",
    ]
    source_artifact = manifest["source_artifact"]
    if source_artifact is not None:
        lines.append(
            f"Source artifact: `{source_artifact['fingerprint']}` "
            f"({source_artifact['dataset_name']}@{source_artifact['dataset_version']})"
        )
    lines += [
        "",
        "> Retrospective diagnostic only. A positive verdict authorizes a new forward",
        "> cohort, never live capital directly.",
        "> funding_usd is a fixed conservative-rate model, not observed signed funding.",
        "",
        f"## Verdict: `{report.verdict}`",
    ]
    if report.diagnostic is not None:
        lines.append(f"Diagnostic: `{report.diagnostic}`")
    lines += [
        "",
        "## Readiness",
        "",
    ]
    ready = report.readiness
    lines.extend(
        markdown_table(
            ("Closed", "Accounting-complete", "Quote-captured", "Comparable"),
            [
                (
                    ready["closed_short_paper_trades"],
                    ready["accounting_complete"],
                    ready["quote_captured"],
                    ready["comparable"],
                )
            ],
        )
    )
    lines += ["", "## Exclusion reasons", ""]
    if report.exclusion_reasons:
        lines.extend(
            markdown_table(
                ("Reason", "Count"),
                sorted(report.exclusion_reasons.items(), key=lambda item: -item[1]),
            )
        )
    else:
        lines.append("_None._")

    lines += ["", "## Economics", ""]
    if report.metrics is None:
        lines.append("_No comparable trades yet._")
    else:
        m = report.metrics
        lines.extend(
            markdown_table(
                ("Metric", "Recorded", "Adjusted"),
                [
                    (
                        "Total net PnL (usd)",
                        format_number(m.total_recorded_net_pnl_usd),
                        format_number(m.total_adjusted_net_pnl_usd),
                    ),
                    (
                        "Mean net PnL (usd)",
                        format_number(m.mean_recorded_net_pnl_usd),
                        format_number(m.mean_adjusted_net_pnl_usd),
                    ),
                    (
                        "Median net PnL (usd)",
                        format_number(m.median_recorded_net_pnl_usd),
                        format_number(m.median_adjusted_net_pnl_usd),
                    ),
                ],
            )
        )
        lines += [
            "",
            f"Mean paired delta: {format_number(m.mean_paired_delta_usd)} usd "
            f"(median {format_number(m.median_paired_delta_usd)} usd)",
            f"Win rate (adjusted): {format_percentage(m.win_rate_pct)}",
            "Profit factor (adjusted): "
            + (
                format_number(m.profit_factor) if m.profit_factor is not None else "inf (no losers)"
            ),
            f"Max drawdown (adjusted): {format_number(m.max_drawdown_usd)} usd",
            f"Worst losing streak (adjusted): {m.worst_losing_streak}",
            f"Asset clusters: {m.asset_clusters} "
            f"(largest {format_percentage(m.largest_cluster_share_pct)} of trades)",
            f"UTC weeks: {m.utc_weeks} "
            f"(busiest {format_percentage(m.busiest_week_share_pct)} of trades)",
        ]

    lines += ["", "## Robustness", ""]
    if report.robustness is None:
        lines.append("_No comparable trades yet._")
    else:
        r = report.robustness
        lines += [
            f"Bootstrap mean adjusted net PnL: {format_number(r.bootstrap_point_estimate)} usd "
            f"[{format_number(r.bootstrap_lower_bound)}, {format_number(r.bootstrap_upper_bound)}] "
            f"(95% CI, {r.bootstrap_clusters} clusters)",
            f"Leave-best-asset-out (`{r.leave_best_asset_out_cluster}` excluded): "
            f"{format_number(r.leave_best_asset_out_mean_usd)} usd",
        ]
        lines += ["", "Leave-one-week-out:", ""]
        lines.extend(
            markdown_table(
                ("Week excluded", "Mean adjusted net PnL (usd)"),
                [(week, format_number(value)) for week, value in r.leave_one_week_out],
            )
        )

    lines += ["", "## Segments", ""]
    lines.extend(
        markdown_table(
            ("Dimension", "Bucket", "N", "Mean adjusted net PnL (usd)", "Win rate"),
            [
                (
                    segment.dimension,
                    segment.bucket,
                    segment.observations,
                    format_number(segment.mean_adjusted_net_pnl_usd),
                    format_percentage(segment.win_rate_pct),
                )
                for segment in report.segments
            ],
        )
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare recorded vs. observed-quote-adjusted net economics for pump_short v1"
    )
    parser.add_argument("--since", type=parse_utc_datetime, default=EXIT_LIQUIDITY_COHORT_START)
    parser.add_argument("--until", type=parse_utc_datetime)
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty",
        action=argparse.BooleanOptionalAction,
        required=True,
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--from-artifact",
        metavar="FINGERPRINT",
        help="Read a previously frozen artifact instead of querying the live database.",
    )
    source.add_argument(
        "--freeze-artifact",
        action="store_true",
        help="After querying the database, also freeze the exact rows this run saw.",
    )
    parser.add_argument(
        "--artifact-directory",
        help="Override the research-dataset-artifact store directory (mainly for tests).",
    )
    return parser


def _source_artifact_payload(manifest: DatasetArtifactManifest) -> dict[str, Any]:
    return {
        "fingerprint": manifest.fingerprint,
        "dataset_name": manifest.dataset_name,
        "dataset_version": manifest.dataset_version,
        "schema_version": manifest.schema_version,
        "data_sha256": manifest.data_sha256,
        "code_revision": manifest.code_revision,
        "working_tree_dirty": manifest.working_tree_dirty,
        "generated_at": manifest.generated_at,
    }


async def _run(args: argparse.Namespace) -> str:
    from .exit_liquidity_net_economics_dataset_artifact import freeze, read
    from .exit_liquidity_net_economics_repository import ExitLiquidityNetEconomicsRepository
    from .research_dataset_artifact import ArtifactWriteOutcome, ResearchDatasetArtifactWriteError

    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    generated_at = datetime.now(UTC)
    source_artifact: dict[str, Any] | None = None

    if args.from_artifact:
        manifest, filters, rows = read(args.from_artifact, directory=args.artifact_directory)
        source_artifact = _source_artifact_payload(manifest)
    else:
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            raise ValueError(
                "DATABASE_URL is required for exit-liquidity-adjusted-net-economics-report"
            )
        filters = ExitLiquidityFilters(since=args.since, until=args.until or generated_at)
        repository = ExitLiquidityNetEconomicsRepository.from_url(db_url)
        try:
            rows = await repository.load(filters)
        finally:
            await repository.close()
        if args.freeze_artifact:
            outcome, freeze_manifest = freeze(
                rows,
                filters,
                code_revision=args.code_revision,
                working_tree_dirty=args.working_tree_dirty,
                directory=args.artifact_directory,
            )
            if outcome not in (ArtifactWriteOutcome.CREATED, ArtifactWriteOutcome.ALREADY_EXISTS):
                raise ResearchDatasetArtifactWriteError(
                    f"--freeze-artifact failed: {outcome.value}"
                )
            assert freeze_manifest is not None
            sys.stderr.write(
                f"[research-dataset-artifact] {outcome.value} "
                f"fingerprint={freeze_manifest.fingerprint} row_count={freeze_manifest.row_count}\n"
            )
            # This run's own rendered report must carry the provenance of
            # the artifact it just created, not just print it to stderr and
            # leave manifest.source_artifact null (colleague review,
            # 2026-08-25).
            source_artifact = _source_artifact_payload(freeze_manifest)

    report = build_net_economics_report(
        rows,
        filters,
        generated_at=generated_at,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
        source_artifact=source_artifact,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))
