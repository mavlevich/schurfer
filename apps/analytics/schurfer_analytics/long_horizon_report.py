"""Descriptive 24h, 72h, and 7d pump-short research with signed funding."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from statistics import fmean, median
from typing import Any

from .derivatives_context_resolver import LONG_HORIZON_FUNDING_RESOLVER_VERSION
from .long_horizon_funding_repository import (
    FundingSeries,
    funding_series_fingerprint,
)
from .ohlcv import ceil_to_timeframe
from .outcomes import RESOLVER_VERSION
from .replay import (
    FOUNDATION_VERSION,
    QUERY_VERSION,
    ReplayDataset,
    ReplayDecision,
    ReplayEpisode,
    ReplayFilters,
    ReplayOutcome,
    build_replay_dataset,
    decision_exclusion_reasons,
)
from .reporting import (
    format_number,
    format_percentage,
    horizon_label,
    json_ready,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
)
from .virtual_strategy import DEFAULT_COSTS, decision_impact_bps, exit_parameters

LONG_HORIZON_REPORT_VERSION = "long_horizon_signed_funding_report_v1"
LONG_HORIZON_SELECTION_VERSION = "recorded_open_else_first_decision_v1"
LONG_HORIZON_ELIGIBILITY_VERSION = "selected_decision_long_horizon_outcomes_v1"
SHORT_FUNDING_SIGN_CONVENTION = "positive_rate_long_pays_short_v1"
LONG_HORIZON_COHORT_START = datetime(2026, 7, 22, tzinfo=UTC)
LONG_HORIZONS = (1_440, 4_320, 10_080)
LONG_HORIZON_STRATEGY_VERSIONS = ("pump_short_v1_market_quality",)
TIMEFRAME_MINUTES = 5


@dataclass(frozen=True)
class CountRow:
    name: str
    count: int


@dataclass(frozen=True)
class LongHorizonManifest:
    report_version: str
    replay_engine_version: str
    replay_query_version: str
    selection_version: str
    eligibility_version: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    dataset_since: datetime
    dataset_until_exclusive: datetime
    decision_input_fingerprint: str
    funding_input_fingerprint: str
    strategy_versions: tuple[str, ...]
    outcome_resolver_version: str
    funding_resolver_version: str
    funding_sign_convention: str
    required_horizons: tuple[int, ...]
    taker_fee_bps_per_side: float
    interpretation: str = "descriptive_discovery_only"


@dataclass(frozen=True)
class LongHorizonResult:
    pump_event_id: int
    cluster_key: str
    base: str
    exchange: str
    decision_id: str
    decision_at: datetime
    horizon_minutes: int
    status: str
    gross_short_return_pct: float | None
    funding_settlements: int | None
    signed_funding_return_pct: float | None
    modeled_signed_funding_cash_usd: float | None
    funding_direction: str | None
    execution_cost_bps: float | None
    net_fixed_horizon_return_pct: float | None
    mfe_pct: float | None
    mae_pct: float | None
    initial_stop_pct: float | None
    survived_initial_stop: bool | None
    position_usd: float | None
    error: str | None = None


@dataclass(frozen=True)
class HorizonMetrics:
    horizon_minutes: int
    selected_episodes: int
    resolved_episodes: int
    funding_resolved: int
    unresolved: int
    asset_clusters: int
    mean_gross_short_return_pct: float | None
    median_gross_short_return_pct: float | None
    mean_signed_funding_return_pct: float | None
    median_signed_funding_return_pct: float | None
    funding_credit_rate_pct: float | None
    mean_execution_cost_bps: float | None
    mean_net_fixed_horizon_return_pct: float | None
    median_net_fixed_horizon_return_pct: float | None
    net_positive_rate_pct: float | None
    initial_stop_survival_rate_pct: float | None
    survivor_mean_net_return_pct: float | None
    mean_mfe_pct: float | None
    mean_mae_pct: float | None
    opportunities_per_calendar_day: float | None
    expected_concurrent_positions_upper_bound: float | None
    expected_occupied_notional_usd_upper_bound: float | None


@dataclass(frozen=True)
class LongHorizonReport:
    manifest: LongHorizonManifest
    dataset_episodes: int
    eligible_episodes: int
    excluded_episodes: int
    input_exclusion_reasons: tuple[CountRow, ...]
    funding_run_statuses: tuple[CountRow, ...]
    result_statuses: tuple[CountRow, ...]
    unresolved_reasons: tuple[CountRow, ...]
    horizon_metrics: tuple[HorizonMetrics, ...]
    results: tuple[LongHorizonResult, ...]


def _finite_number(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or (positive and parsed <= 0):
        return None
    return parsed


def _position_usd(decision: ReplayDecision) -> float | None:
    if not isinstance(decision.features, dict):
        return None
    config = decision.features.get("config")
    if not isinstance(config, dict):
        return None
    return _finite_number(config.get("signal_position_usd"), positive=True)


def _select_decision(episode: ReplayEpisode) -> ReplayDecision:
    opened = next(
        (
            decision
            for decision in episode.decisions
            if decision.action in {"opened", "opened_dry_run"}
        ),
        None,
    )
    return opened or episode.decisions[0]


def build_long_horizon_dataset(
    decisions: list[ReplayDecision],
    filters: ReplayFilters,
) -> ReplayDataset:
    """Require long outcomes on the selected decision, not every repeated decision."""
    dataset = build_replay_dataset(decisions, filters)
    outcome_prefixes = (
        "missing_outcome:",
        "outcome_status:",
        "duplicate_outcome:",
    )
    episodes: list[ReplayEpisode] = []
    for episode in dataset.episodes:
        selected = _select_decision(episode)
        reasons = {
            reason
            for reason in episode.exclusion_reasons
            if not reason.startswith(outcome_prefixes)
        }
        reasons.update(
            reason
            for reason in decision_exclusion_reasons(selected, filters)
            if reason.startswith(outcome_prefixes)
        )
        episodes.append(
            replace(
                episode,
                exclusion_reasons=tuple(sorted(reasons)),
            )
        )
    return replace(dataset, episodes=tuple(episodes))


def _outcome(decision: ReplayDecision, horizon: int) -> ReplayOutcome | None:
    return next(
        (item for item in decision.outcomes if item.horizon_minutes == horizon),
        None,
    )


def _funding_rate(payload: dict[str, Any] | list[Any]) -> float | None:
    if not isinstance(payload, dict):
        return None
    return _finite_number(payload.get("fundingRate"))


def signed_funding_for_window(
    series: FundingSeries | None,
    *,
    entry_at: datetime,
    exit_at: datetime,
) -> tuple[int | None, float | None, str | None]:
    """Return settlement count and signed short return, failing closed on gaps.

    CCXT's unified funding rate keeps the venue-reported sign. Under the versioned
    convention, a positive rate is paid by longs to shorts and is therefore a
    positive return for this short model. A negative rate is a short debit.
    """
    if series is None:
        return None, None, "funding_run_missing"
    if series.status != "sampled":
        return None, None, f"funding_run_status:{series.status}"
    if series.requested_since > entry_at or series.requested_until < exit_at:
        return None, None, "funding_window_not_covered"
    samples = tuple(sample for sample in series.samples if entry_at < sample.source_at <= exit_at)
    if not samples:
        return None, None, "funding_settlements_missing"
    if len({sample.source_at for sample in samples}) != len(samples):
        return None, None, "duplicate_funding_settlement"
    rates = tuple(_funding_rate(sample.payload) for sample in samples)
    if any(rate is None for rate in rates):
        return None, None, "invalid_funding_rate"
    signed_rate = sum(rate for rate in rates if rate is not None)
    return len(rates), signed_rate * 100.0, None


def _funding_direction(signed_return_pct: float) -> str:
    if signed_return_pct > 0:
        return "credit"
    if signed_return_pct < 0:
        return "debit"
    return "flat"


def _result(
    episode: ReplayEpisode,
    decision: ReplayDecision,
    horizon: int,
    series: FundingSeries | None,
    *,
    taker_fee_bps_per_side: float,
) -> LongHorizonResult:
    outcome = _outcome(decision, horizon)
    if outcome is None:
        return LongHorizonResult(
            pump_event_id=episode.pump_event_id,
            cluster_key=episode.cluster_key,
            base=episode.base,
            exchange=decision.exchange,
            decision_id=decision.decision_id or "",
            decision_at=decision.ts,
            horizon_minutes=horizon,
            status="unresolved",
            gross_short_return_pct=None,
            funding_settlements=None,
            signed_funding_return_pct=None,
            modeled_signed_funding_cash_usd=None,
            funding_direction=None,
            execution_cost_bps=None,
            net_fixed_horizon_return_pct=None,
            mfe_pct=None,
            mae_pct=None,
            initial_stop_pct=None,
            survived_initial_stop=None,
            position_usd=_position_usd(decision),
            error="outcome_missing",
        )
    exact_venue = (
        outcome.status == "complete"
        and outcome.anchor_exchange == decision.exchange
        and outcome.source_exchange == decision.exchange
    )
    if not exact_venue or outcome.short_return_pct is None:
        return LongHorizonResult(
            pump_event_id=episode.pump_event_id,
            cluster_key=episode.cluster_key,
            base=episode.base,
            exchange=decision.exchange,
            decision_id=decision.decision_id or "",
            decision_at=decision.ts,
            horizon_minutes=horizon,
            status="unresolved",
            gross_short_return_pct=None,
            funding_settlements=None,
            signed_funding_return_pct=None,
            modeled_signed_funding_cash_usd=None,
            funding_direction=None,
            execution_cost_bps=None,
            net_fixed_horizon_return_pct=None,
            mfe_pct=outcome.mfe_pct,
            mae_pct=outcome.mae_pct,
            initial_stop_pct=None,
            survived_initial_stop=None,
            position_usd=_position_usd(decision),
            error=f"outcome_not_exact:{outcome.status}",
        )
    entry_ms = ceil_to_timeframe(int(decision.ts.timestamp() * 1000))
    entry_at = datetime.fromtimestamp(entry_ms / 1000, tz=UTC)
    exit_at = entry_at + timedelta(minutes=horizon)
    settlement_count, funding_return_pct, funding_error = signed_funding_for_window(
        series,
        entry_at=entry_at,
        exit_at=exit_at,
    )
    bid_impact = decision_impact_bps(decision, "bid")
    ask_impact = decision_impact_bps(decision, "ask")
    cost_error = "execution_impact_missing" if bid_impact is None or ask_impact is None else None
    position_usd = _position_usd(decision)
    position_error = "position_usd_missing" if position_usd is None else None
    errors = tuple(
        error for error in (funding_error, cost_error, position_error) if error is not None
    )
    params = exit_parameters(decision.pump_pct)
    survived = (
        outcome.mae_pct < params.initial_sl_pct
        if outcome.mae_pct is not None and math.isfinite(outcome.mae_pct)
        else None
    )
    if errors:
        return LongHorizonResult(
            pump_event_id=episode.pump_event_id,
            cluster_key=episode.cluster_key,
            base=episode.base,
            exchange=decision.exchange,
            decision_id=decision.decision_id or "",
            decision_at=decision.ts,
            horizon_minutes=horizon,
            status="unresolved",
            gross_short_return_pct=outcome.short_return_pct,
            funding_settlements=settlement_count,
            signed_funding_return_pct=funding_return_pct,
            modeled_signed_funding_cash_usd=None,
            funding_direction=(
                _funding_direction(funding_return_pct) if funding_return_pct is not None else None
            ),
            execution_cost_bps=None,
            net_fixed_horizon_return_pct=None,
            mfe_pct=outcome.mfe_pct,
            mae_pct=outcome.mae_pct,
            initial_stop_pct=params.initial_sl_pct,
            survived_initial_stop=survived,
            position_usd=position_usd,
            error=";".join(errors),
        )
    if (
        funding_return_pct is None
        or settlement_count is None
        or bid_impact is None
        or ask_impact is None
        or position_usd is None
    ):
        raise RuntimeError("resolved long-horizon inputs unexpectedly missing")
    execution_cost_bps = taker_fee_bps_per_side * 2.0 + bid_impact + ask_impact
    net_return_pct = outcome.short_return_pct + funding_return_pct - execution_cost_bps / 100.0
    return LongHorizonResult(
        pump_event_id=episode.pump_event_id,
        cluster_key=episode.cluster_key,
        base=episode.base,
        exchange=decision.exchange,
        decision_id=decision.decision_id or "",
        decision_at=decision.ts,
        horizon_minutes=horizon,
        status="resolved",
        gross_short_return_pct=outcome.short_return_pct,
        funding_settlements=settlement_count,
        signed_funding_return_pct=funding_return_pct,
        modeled_signed_funding_cash_usd=position_usd * funding_return_pct / 100.0,
        funding_direction=_funding_direction(funding_return_pct),
        execution_cost_bps=execution_cost_bps,
        net_fixed_horizon_return_pct=net_return_pct,
        mfe_pct=outcome.mfe_pct,
        mae_pct=outcome.mae_pct,
        initial_stop_pct=params.initial_sl_pct,
        survived_initial_stop=survived,
        position_usd=position_usd,
    )


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def _median(values: list[float]) -> float | None:
    return median(values) if values else None


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator * 100.0 if denominator else None


def _horizon_metrics(
    horizon: int,
    results: tuple[LongHorizonResult, ...],
    *,
    calendar_days: float,
) -> HorizonMetrics:
    rows = tuple(row for row in results if row.horizon_minutes == horizon)
    resolved = tuple(row for row in rows if row.status == "resolved")
    gross = [
        row.gross_short_return_pct for row in resolved if row.gross_short_return_pct is not None
    ]
    funding = [
        row.signed_funding_return_pct
        for row in resolved
        if row.signed_funding_return_pct is not None
    ]
    costs = [row.execution_cost_bps for row in resolved if row.execution_cost_bps is not None]
    net = [
        row.net_fixed_horizon_return_pct
        for row in resolved
        if row.net_fixed_horizon_return_pct is not None
    ]
    survivor_net = [
        row.net_fixed_horizon_return_pct
        for row in resolved
        if row.survived_initial_stop is True and row.net_fixed_horizon_return_pct is not None
    ]
    stop_observed = [row for row in resolved if row.survived_initial_stop is not None]
    positions = [row.position_usd for row in resolved if row.position_usd is not None]
    opportunities_per_day = len(rows) / calendar_days if calendar_days > 0 else None
    concurrency = (
        opportunities_per_day * horizon / 1_440.0 if opportunities_per_day is not None else None
    )
    return HorizonMetrics(
        horizon_minutes=horizon,
        selected_episodes=len(rows),
        resolved_episodes=len(resolved),
        funding_resolved=len(funding),
        unresolved=len(rows) - len(resolved),
        asset_clusters=len({row.cluster_key for row in resolved}),
        mean_gross_short_return_pct=_mean(gross),
        median_gross_short_return_pct=_median(gross),
        mean_signed_funding_return_pct=_mean(funding),
        median_signed_funding_return_pct=_median(funding),
        funding_credit_rate_pct=_rate(
            sum(value > 0 for value in funding),
            len(funding),
        ),
        mean_execution_cost_bps=_mean(costs),
        mean_net_fixed_horizon_return_pct=_mean(net),
        median_net_fixed_horizon_return_pct=_median(net),
        net_positive_rate_pct=_rate(sum(value > 0 for value in net), len(net)),
        initial_stop_survival_rate_pct=_rate(
            sum(row.survived_initial_stop is True for row in stop_observed),
            len(stop_observed),
        ),
        survivor_mean_net_return_pct=_mean(survivor_net),
        mean_mfe_pct=_mean([row.mfe_pct for row in resolved if row.mfe_pct is not None]),
        mean_mae_pct=_mean([row.mae_pct for row in resolved if row.mae_pct is not None]),
        opportunities_per_calendar_day=opportunities_per_day,
        expected_concurrent_positions_upper_bound=concurrency,
        expected_occupied_notional_usd_upper_bound=(
            concurrency * fmean(positions) if concurrency is not None and positions else None
        ),
    )


def build_long_horizon_report(
    dataset: ReplayDataset,
    filters: ReplayFilters,
    funding_series: tuple[FundingSeries, ...],
    *,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    taker_fee_bps_per_side: float = DEFAULT_COSTS.taker_fee_bps_per_side,
) -> LongHorizonReport:
    if filters.since is None:
        raise ValueError("long-horizon report requires an inclusive cohort start")
    if filters.required_horizons != LONG_HORIZONS:
        raise ValueError("long-horizon report requires the registered horizons")
    if not math.isfinite(taker_fee_bps_per_side) or taker_fee_bps_per_side < 0:
        raise ValueError("taker fee must be finite and non-negative")
    series_by_key = {(series.event_id, series.exchange): series for series in funding_series}
    if len(series_by_key) != len(funding_series):
        raise ValueError("duplicate long-horizon funding series")
    results = tuple(
        _result(
            episode,
            decision,
            horizon,
            series_by_key.get((episode.pump_event_id, decision.exchange)),
            taker_fee_bps_per_side=taker_fee_bps_per_side,
        )
        for episode in dataset.eligible_episodes
        for decision in (_select_decision(episode),)
        for horizon in LONG_HORIZONS
    )
    calendar_days = max(
        (filters.until - filters.since).total_seconds() / 86_400.0,
        1 / 86_400.0,
    )
    exclusion_counts = Counter(
        reason for episode in dataset.excluded_episodes for reason in episode.exclusion_reasons
    )
    return LongHorizonReport(
        manifest=LongHorizonManifest(
            report_version=LONG_HORIZON_REPORT_VERSION,
            replay_engine_version=FOUNDATION_VERSION,
            replay_query_version=QUERY_VERSION,
            selection_version=LONG_HORIZON_SELECTION_VERSION,
            eligibility_version=LONG_HORIZON_ELIGIBILITY_VERSION,
            code_revision=normalize_code_revision(code_revision),
            working_tree_dirty=working_tree_dirty,
            generated_at=generated_at,
            dataset_since=filters.since,
            dataset_until_exclusive=filters.until,
            decision_input_fingerprint=dataset.input_fingerprint,
            funding_input_fingerprint=funding_series_fingerprint(funding_series),
            strategy_versions=filters.strategy_versions,
            outcome_resolver_version=filters.resolver_version,
            funding_resolver_version=LONG_HORIZON_FUNDING_RESOLVER_VERSION,
            funding_sign_convention=SHORT_FUNDING_SIGN_CONVENTION,
            required_horizons=filters.required_horizons,
            taker_fee_bps_per_side=taker_fee_bps_per_side,
        ),
        dataset_episodes=len(dataset.episodes),
        eligible_episodes=len(dataset.eligible_episodes),
        excluded_episodes=len(dataset.excluded_episodes),
        input_exclusion_reasons=tuple(
            CountRow(name, count) for name, count in sorted(exclusion_counts.items())
        ),
        funding_run_statuses=tuple(
            CountRow(name, count)
            for name, count in sorted(Counter(series.status for series in funding_series).items())
        ),
        result_statuses=tuple(
            CountRow(name, count)
            for name, count in sorted(Counter(row.status for row in results).items())
        ),
        unresolved_reasons=tuple(
            CountRow(name, count)
            for name, count in sorted(
                Counter(
                    error
                    for row in results
                    if row.error is not None
                    for error in row.error.split(";")
                ).items()
            )
        ),
        horizon_metrics=tuple(
            _horizon_metrics(horizon, results, calendar_days=calendar_days)
            for horizon in LONG_HORIZONS
        ),
        results=results,
    )


def render_json(report: LongHorizonReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True, allow_nan=False)


def render_markdown(report: LongHorizonReport) -> str:
    manifest = report.manifest
    lines = [
        "# Pump Short Long-Horizon and Signed-Funding Research",
        "",
        f"Generated: {manifest.generated_at.isoformat()}",
        f"Code revision: `{manifest.code_revision}`",
        f"Working tree dirty: {'yes' if manifest.working_tree_dirty else 'no'}",
        f"Decision fingerprint: `{manifest.decision_input_fingerprint}`",
        f"Funding fingerprint: `{manifest.funding_input_fingerprint}`",
        (
            f"Scope: {manifest.dataset_since.isoformat()} <= decision "
            f"< {manifest.dataset_until_exclusive.isoformat()}"
        ),
        "",
        (
            "> Descriptive discovery only. Fixed-horizon rows do not authorize a longer "
            "production hold and do not claim that a trade survived the baseline stop."
        ),
        "",
        "## Coverage",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Dataset episodes", report.dataset_episodes),
                ("Eligible matched episodes", report.eligible_episodes),
                ("Excluded episodes", report.excluded_episodes),
                ("Outcome resolver", manifest.outcome_resolver_version),
                ("Funding resolver", manifest.funding_resolver_version),
                ("Funding sign", manifest.funding_sign_convention),
            ],
        )
    )
    lines.extend(["", "## Input exclusions", ""])
    lines.extend(
        markdown_table(
            ("Reason", "Episodes"),
            [(row.name, row.count) for row in report.input_exclusion_reasons],
        )
    )
    lines.extend(["", "## Funding runs", ""])
    lines.extend(
        markdown_table(
            ("Status", "Runs"),
            [(row.name, row.count) for row in report.funding_run_statuses],
        )
    )
    lines.extend(["", "## Long-horizon economics", ""])
    lines.extend(
        markdown_table(
            (
                "Horizon",
                "Resolved",
                "Clusters",
                "Gross mean",
                "Signed funding",
                "Funding credit",
                "Execution",
                "Net mean",
                "Net median",
                "Net positive",
            ),
            [
                (
                    horizon_label(row.horizon_minutes),
                    f"{row.resolved_episodes}/{row.selected_episodes}",
                    row.asset_clusters,
                    format_percentage(
                        row.mean_gross_short_return_pct,
                        missing="n/a",
                    ),
                    format_percentage(
                        row.mean_signed_funding_return_pct,
                        missing="n/a",
                    ),
                    format_percentage(
                        row.funding_credit_rate_pct,
                        missing="n/a",
                    ),
                    format_number(
                        row.mean_execution_cost_bps,
                        suffix=" bps",
                        missing="n/a",
                    ),
                    format_percentage(
                        row.mean_net_fixed_horizon_return_pct,
                        missing="n/a",
                    ),
                    format_percentage(
                        row.median_net_fixed_horizon_return_pct,
                        missing="n/a",
                    ),
                    format_percentage(
                        row.net_positive_rate_pct,
                        missing="n/a",
                    ),
                )
                for row in report.horizon_metrics
            ],
        )
    )
    lines.extend(["", "## Stop survival and capital occupancy", ""])
    lines.extend(
        markdown_table(
            (
                "Horizon",
                "Initial-SL survival",
                "Survivor net mean",
                "MFE mean",
                "MAE mean",
                "Opportunities/day",
                "Concurrent upper bound",
                "Notional upper bound",
            ),
            [
                (
                    horizon_label(row.horizon_minutes),
                    format_percentage(
                        row.initial_stop_survival_rate_pct,
                        missing="n/a",
                    ),
                    format_percentage(
                        row.survivor_mean_net_return_pct,
                        missing="n/a",
                    ),
                    format_percentage(row.mean_mfe_pct, missing="n/a"),
                    format_percentage(row.mean_mae_pct, missing="n/a"),
                    format_number(
                        row.opportunities_per_calendar_day,
                        missing="n/a",
                    ),
                    format_number(
                        row.expected_concurrent_positions_upper_bound,
                        missing="n/a",
                    ),
                    format_number(
                        row.expected_occupied_notional_usd_upper_bound,
                        suffix=" USD",
                        missing="n/a",
                    ),
                )
                for row in report.horizon_metrics
            ],
        )
    )
    lines.extend(
        [
            "",
            (
                "_Concurrency and occupied notional assume every selected episode stays "
                "open for the full horizon. They are conservative capacity upper bounds, "
                "not forecasts. Signed funding assumes the short is open at each published "
                "settlement and applies each rate to initial notional. It is a model, not "
                "an exchange ledger. Positive rates credit the short and negative rates "
                "debit it._"
            ),
            "",
            "## Result statuses",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            ("Status", "Rows"),
            [(row.name, row.count) for row in report.result_statuses],
        )
    )
    lines.extend(["", "## Unresolved reasons", ""])
    lines.extend(
        markdown_table(
            ("Reason", "Rows"),
            [(row.name, row.count) for row in report.unresolved_reasons],
        )
    )
    lines.extend(["", "## Episode results", ""])
    lines.extend(
        markdown_table(
            (
                "Episode",
                "Base",
                "Exchange",
                "Horizon",
                "Status",
                "Gross",
                "Funding",
                "Settlements",
                "Net fixed",
                "Initial SL",
                "Survived",
                "Error",
            ),
            [
                (
                    row.pump_event_id,
                    row.base,
                    row.exchange,
                    horizon_label(row.horizon_minutes),
                    row.status,
                    format_percentage(row.gross_short_return_pct, missing="n/a"),
                    format_percentage(row.signed_funding_return_pct, missing="n/a"),
                    row.funding_settlements if row.funding_settlements is not None else "n/a",
                    format_percentage(
                        row.net_fixed_horizon_return_pct,
                        missing="n/a",
                    ),
                    format_percentage(row.initial_stop_pct, missing="n/a"),
                    (
                        "yes"
                        if row.survived_initial_stop is True
                        else "no"
                        if row.survived_initial_stop is False
                        else "n/a"
                    ),
                    row.error or "",
                )
                for row in report.results
            ],
        )
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Describe 24h, 72h, and 7d exact-venue returns with signed funding"
    )
    parser.add_argument(
        "--since",
        type=parse_utc_datetime,
        default=LONG_HORIZON_COHORT_START,
    )
    parser.add_argument("--until", type=parse_utc_datetime)
    parser.add_argument("--strategy-version", action="append")
    parser.add_argument("--resolver-version", default=RESOLVER_VERSION)
    parser.add_argument(
        "--taker-fee-bps-per-side",
        type=float,
        default=DEFAULT_COSTS.taker_fee_bps_per_side,
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
    from .long_horizon_funding_repository import LongHorizonFundingRepository
    from .replay_repository import ReplayRepository

    generated_at = datetime.now(UTC)
    until = args.until or generated_at
    if args.since >= until:
        raise ValueError("since must be earlier than until")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for long-horizon-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    filters = ReplayFilters(
        since=args.since,
        until=until,
        strategy_versions=tuple(args.strategy_version or LONG_HORIZON_STRATEGY_VERSIONS),
        resolver_version=args.resolver_version,
        required_horizons=LONG_HORIZONS,
        allow_fallback=False,
    )
    replay_repository = ReplayRepository.from_url(db_url)
    funding_repository = LongHorizonFundingRepository.from_url(db_url)
    try:
        decisions = await replay_repository.load(filters)
        dataset = build_long_horizon_dataset(decisions, filters)
        keys = tuple(
            (
                episode.pump_event_id,
                _select_decision(episode).exchange,
            )
            for episode in dataset.eligible_episodes
        )
        funding_series = await funding_repository.load(keys)
    finally:
        await asyncio.gather(
            replay_repository.close(),
            funding_repository.close(),
        )
    report = build_long_horizon_report(
        dataset,
        filters,
        funding_series,
        generated_at=generated_at,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
        taker_fee_bps_per_side=args.taker_fee_bps_per_side,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    try:
        sys.stdout.write(asyncio.run(_run(build_parser().parse_args())))
    except ValueError as exc:
        build_parser().error(str(exc))
