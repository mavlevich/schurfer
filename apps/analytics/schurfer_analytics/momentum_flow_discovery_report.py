"""Read-only discovery report for the frozen momentum-flow WATCH/paper baseline.

The report is descriptive. It can declare the dataset ready for human review, but it
cannot promote or stop a strategy automatically: this discovery cohort was not
registered with an outcome-based decision threshold before its outcomes existed.
Missing inputs remain explicit and never become neutral zeroes.
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
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean, median
from typing import TYPE_CHECKING, Any

from .momentum_flow_cohort_acceptance import (
    load_accepted_cohort,
    resolve_capture_cohort_started_at,
    save_accepted_cohort,
)
from .momentum_flow_discovery_repository import (
    DiscoveryDataset,
    MomentumFlowDiscoveryRepository,
    PaperProbe,
    Pump,
    VenueVersions,
    WatchDecision,
)
from .momentum_flow_paper_contract import (
    BINANCE_PAPER_CONTRACT,
    BINANCE_PAPER_CONTRACT_SHA256,
    FROZEN_PAPER_CONTRACT,
    PAPER_CONTRACT_SHA256,
)
from .momentum_flow_watch_contract import (
    BINANCE_WATCH_CONTRACT,
    BINANCE_WATCH_CONTRACT_SHA256,
    FROZEN_WATCH_CONTRACT,
    WATCH_CONTRACT_SHA256,
)
from .reporting import json_ready, normalize_code_revision, parse_utc_datetime

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

REPORT_VERSION = "momentum_flow_discovery_read_v1"
REPORT_INTERPRETATION = "descriptive_discovery_read_not_promotion_evidence"
DISCOVERY_COHORT_STATE_ENV_VAR = "MOMENTUM_FLOW_DISCOVERY_COHORT_STATE_PATH"
DEFAULT_DISCOVERY_COHORT_STATE_PATH = "runtime/momentum-flow-discovery-cohort.json"
PUMP_LEAD_MINUTES = 240
MIN_DISTINCT_UTC_WEEKS = 4
MAX_WINDOW_DAYS = 28
MIN_AVAILABILITY_COVERAGE = 0.99

VENUE_VERSIONS = (
    VenueVersions(
        exchange="bybit",
        watch_version=FROZEN_WATCH_CONTRACT.watch_version,
        watch_contract_sha256=WATCH_CONTRACT_SHA256,
        paper_version=FROZEN_PAPER_CONTRACT.paper_version,
        paper_contract_sha256=PAPER_CONTRACT_SHA256,
    ),
    VenueVersions(
        exchange="binance",
        watch_version=BINANCE_WATCH_CONTRACT.watch_version,
        watch_contract_sha256=BINANCE_WATCH_CONTRACT_SHA256,
        paper_version=BINANCE_PAPER_CONTRACT.paper_version,
        paper_contract_sha256=BINANCE_PAPER_CONTRACT_SHA256,
    ),
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", type=parse_utc_datetime, required=True)
    parser.add_argument("--until", type=parse_utc_datetime, required=True)
    parser.add_argument("--capture-epoch-started-at", type=parse_utc_datetime, required=True)
    parser.add_argument("--accept-new-cohort-boundary", action="store_true")
    parser.add_argument("--cohort-state-path", type=str)
    parser.add_argument("--code-revision", type=str, required=True)
    dirty = parser.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--no-working-tree-dirty", action="store_true")
    dirty.add_argument("--working-tree-dirty", action="store_true")
    return parser.parse_args()


def _cohort_state_path(explicit: str | None, env: Mapping[str, str]) -> Path:
    return Path(
        explicit or env.get(DISCOVERY_COHORT_STATE_ENV_VAR) or DEFAULT_DISCOVERY_COHORT_STATE_PATH
    )


def _utc_week(value: datetime) -> str:
    iso = value.isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def _expected_minute_buckets(start: datetime, end_exclusive: datetime) -> int:
    if end_exclusive <= start:
        return 0
    minute = 60
    first = math.ceil(start.timestamp() / minute)
    last = math.ceil(end_exclusive.timestamp() / minute) - 1
    return max(0, last - first + 1)


def _expected_pump_window_minutes(trigger_at: datetime) -> int:
    start = trigger_at - timedelta(minutes=PUMP_LEAD_MINUTES)
    minute = 60
    first = math.ceil(start.timestamp() / minute)
    last = math.floor(trigger_at.timestamp() / minute)
    return max(0, last - first + 1)


def _mean(values: Iterable[float | int | None]) -> float | None:
    resolved = [float(value) for value in values if value is not None and math.isfinite(value)]
    return fmean(resolved) if resolved else None


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _distribution(values: Iterable[float | int | None]) -> dict[str, float | int | None]:
    inputs = list(values)
    resolved = [float(value) for value in inputs if value is not None and math.isfinite(value)]
    return {
        "total": len(inputs),
        "count": len(resolved),
        "unresolved": len(inputs) - len(resolved),
        "mean": _mean(resolved),
        "p50": median(resolved) if resolved else None,
        "p99": _percentile(resolved, 0.99),
        "max": max(resolved) if resolved else None,
    }


def _rate(
    numerator: int,
    denominator: int,
    *,
    unresolved: int,
    assets: Iterable[str],
    instants: Iterable[datetime],
) -> dict[str, float | int | None]:
    asset_set = set(assets)
    week_set = {_utc_week(value) for value in instants}
    return {
        "value": numerator / denominator if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
        "unresolved": unresolved,
        "distinct_assets": len(asset_set),
        "distinct_weeks": len(week_set),
    }


def _availability(
    minutes: Sequence[datetime], *, since: datetime, until: datetime
) -> dict[str, Any]:
    observed = sorted({value.replace(second=0, microsecond=0) for value in minutes})
    expected = _expected_minute_buckets(since, until)
    observed_in_window = [value for value in observed if since <= value < until]
    gaps: list[dict[str, Any]] = []
    cursor = since.replace(second=0, microsecond=0)
    if cursor < since:
        cursor += timedelta(minutes=1)
    for value in observed_in_window:
        if value > cursor:
            gaps.append(
                {
                    "since": cursor,
                    "until_exclusive": value,
                    "missing_minutes": int((value - cursor).total_seconds() // 60),
                }
            )
        cursor = max(cursor, value + timedelta(minutes=1))
    end = until.replace(second=0, microsecond=0)
    if end < until:
        end += timedelta(minutes=1)
    if cursor < end:
        gaps.append(
            {
                "since": cursor,
                "until_exclusive": end,
                "missing_minutes": int((end - cursor).total_seconds() // 60),
            }
        )
    observed_count = len(observed_in_window)
    return {
        "expected_minutes": expected,
        "observed_minutes": observed_count,
        "coverage": observed_count / expected if expected else None,
        "gaps": gaps,
    }


def _watch_latency(watches: Sequence[WatchDecision]) -> dict[str, Any]:
    def milliseconds(later: datetime, earlier: datetime | None) -> float | None:
        return (later - earlier).total_seconds() * 1000 if earlier is not None else None

    return {
        "exchange_event_to_decision_ms": _distribution(
            milliseconds(row.decision_at, row.source_event_at) for row in watches
        ),
        "receive_to_decision_ms": _distribution(
            milliseconds(row.decision_at, row.source_received_at) for row in watches
        ),
        "bucket_ready_to_decision_ms": _distribution(
            milliseconds(row.decision_at, row.bucket_ready_at) for row in watches
        ),
        "evaluator_runtime_ms": _distribution(
            milliseconds(row.evaluator_completed_at, row.evaluator_started_at) for row in watches
        ),
    }


def _max_drawdown_usd(probes: Sequence[PaperProbe]) -> float | None:
    closed = sorted(
        (row for row in probes if row.net_pnl_usd is not None and row.exit_at is not None),
        key=lambda row: row.exit_at or datetime.min.replace(tzinfo=UTC),
    )
    if not closed:
        return None
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for row in closed:
        assert row.net_pnl_usd is not None
        equity += row.net_pnl_usd
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return max_drawdown


def _stability(probes: Sequence[PaperProbe]) -> dict[str, Any]:
    by_asset: dict[str, list[float]] = defaultdict(list)
    by_week: dict[str, list[float]] = defaultdict(list)
    for row in probes:
        if row.net_return_pct is None:
            continue
        by_asset[row.symbol].append(row.net_return_pct)
        by_week[_utc_week(row.watch_decision_at)].append(row.net_return_pct)
    return {
        "asset": {
            key: {"n": len(values), "mean_net_return_pct": fmean(values)}
            for key, values in sorted(by_asset.items())
        },
        "utc_week": {
            key: {"n": len(values), "mean_net_return_pct": fmean(values)}
            for key, values in sorted(by_week.items())
        },
        "btc_regime": {
            "status": "unresolved",
            "reason": "BTC regime was not frozen as an input to this baseline",
        },
    }


def _venue_result(
    dataset: DiscoveryDataset,
    *,
    versions: VenueVersions,
    since: datetime,
    until: datetime,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    exchange = versions.exchange
    watch_run = dataset.watch_runs.get(versions.watch_version)
    paper_run = dataset.paper_runs.get(versions.paper_version)
    effective_since = max(
        [
            since,
            *(row.cohort_started_at for row in (watch_run, paper_run) if row is not None),
        ]
    )
    availability = _availability(
        dataset.available_minutes.get(exchange, ()), since=effective_since, until=until
    )
    venue_minutes = tuple(
        value
        for value in dataset.available_minutes.get(exchange, ())
        if effective_since <= value < until
    )
    watches = [
        row
        for row in dataset.watches
        if row.exchange == exchange and effective_since <= row.decision_at < until
    ]
    pumps = [
        row
        for row in dataset.pumps
        if row.exchange == exchange and effective_since <= row.trigger_at < until
    ]
    probes = [
        row
        for row in dataset.probes
        if row.paper_version == versions.paper_version
        and effective_since <= row.watch_decision_at < until
    ]

    classification_until = until - timedelta(minutes=PUMP_LEAD_MINUTES)
    mature_watches = [row for row in watches if row.decision_at < classification_until]
    matched_watch_ids: set[int] = set()
    for index, watch in enumerate(mature_watches):
        if any(
            pump.symbol == watch.symbol
            and watch.decision_at
            <= pump.trigger_at
            <= watch.decision_at + timedelta(minutes=PUMP_LEAD_MINUTES)
            for pump in pumps
        ):
            matched_watch_ids.add(index)
    precision = _rate(
        len(matched_watch_ids),
        len(mature_watches),
        unresolved=len(watches) - len(mature_watches),
        assets=(row.symbol for row in mature_watches),
        instants=(row.decision_at for row in mature_watches),
    )
    false_watch = _rate(
        len(mature_watches) - len(matched_watch_ids),
        len(mature_watches),
        unresolved=len(watches) - len(mature_watches),
        assets=(row.symbol for row in mature_watches),
        instants=(row.decision_at for row in mature_watches),
    )

    observable_pumps: list[Pump] = []
    recalled_pumps: list[Pump] = []
    lead_minutes: list[float] = []
    for pump in pumps:
        observation = dataset.pump_observability.get(pump.pump_id)
        expected = _expected_pump_window_minutes(pump.trigger_at)
        if observation is None or observation.quality_minutes < expected:
            continue
        observable_pumps.append(pump)
        if observation.earliest_watch_at is not None:
            recalled_pumps.append(pump)
            lead_minutes.append(
                (pump.trigger_at - observation.earliest_watch_at).total_seconds() / 60
            )
    recall = _rate(
        len(recalled_pumps),
        len(observable_pumps),
        unresolved=len(pumps) - len(observable_pumps),
        assets=(row.symbol for row in observable_pumps),
        instants=(row.trigger_at for row in observable_pumps),
    )
    recall["median_lead_minutes"] = median(lead_minutes) if lead_minutes else None

    available_days = availability["observed_minutes"] / 1440
    opportunity = {
        "value": len(watches) / available_days if available_days else None,
        "watches": len(watches),
        "observable_days": available_days,
        "unresolved_minutes": sum(row["missing_minutes"] for row in availability["gaps"]),
        "distinct_assets": len({row.symbol for row in watches}),
        "distinct_weeks": len({_utc_week(row.decision_at) for row in watches}),
    }

    completed = [
        row
        for row in probes
        if row.position_status == "closed"
        and row.accounting_status == "complete"
        and row.net_return_pct is not None
        and row.net_pnl_usd is not None
    ]
    rejected = [row for row in probes if row.entry_status in {"rejected_stale", "rejected_quote"}]
    unresolved_probes = [row for row in probes if row not in completed and row not in rejected]
    wins = [row for row in completed if row.net_return_pct is not None and row.net_return_pct > 0]
    positives = sum(max(row.net_pnl_usd or 0.0, 0.0) for row in completed)
    negatives = abs(sum(min(row.net_pnl_usd or 0.0, 0.0) for row in completed))
    resolved_signal_count = len(completed) + len(rejected)
    notional = FROZEN_PAPER_CONTRACT.position_notional_usd
    cash_inclusive_return = (
        sum(row.net_pnl_usd or 0.0 for row in completed) / (notional * resolved_signal_count) * 100
        if resolved_signal_count
        else None
    )
    paper_win_rate = _rate(
        len(wins),
        len(completed),
        unresolved=len(unresolved_probes),
        assets=(row.symbol for row in completed),
        instants=(row.watch_decision_at for row in completed),
    )

    contract_errors: list[str] = []
    if watch_run is None:
        contract_errors.append("watch_run_missing")
    elif watch_run.contract_sha256 != versions.watch_contract_sha256:
        contract_errors.append("watch_contract_hash_mismatch")
    if paper_run is None:
        contract_errors.append("paper_run_missing")
    elif paper_run.contract_sha256 != versions.paper_contract_sha256:
        contract_errors.append("paper_contract_hash_mismatch")
    distinct_weeks = len({_utc_week(value) for value in venue_minutes})
    readiness_reasons = [*contract_errors]
    if distinct_weeks < MIN_DISTINCT_UTC_WEEKS:
        readiness_reasons.append("fewer_than_four_distinct_utc_weeks")
    if not watches:
        readiness_reasons.append("no_watch_decisions")
    if not probes:
        readiness_reasons.append("no_paper_probes")
    elif not completed:
        readiness_reasons.append("no_complete_paper_probes")
    coverage = availability["coverage"]
    if coverage is None or coverage < MIN_AVAILABILITY_COVERAGE:
        readiness_reasons.append("watch_availability_below_99pct")
    readiness = {
        "status": "READY_FOR_MANUAL_REVIEW" if not readiness_reasons else "COLLECTING",
        "reasons": readiness_reasons,
        "distinct_utc_weeks": distinct_weeks,
        "availability_coverage": coverage,
        "paper_probes": len(probes),
        "complete_paper_probes": len(completed),
        "unresolved_paper_probes": len(unresolved_probes),
    }

    results = {
        "entry_funnel": {
            "total_probes": len(probes),
            "entry_status": dict(sorted(Counter(row.entry_status for row in probes).items())),
            "rejection_reasons": dict(
                sorted(Counter(row.entry_reason for row in rejected if row.entry_reason).items())
            ),
            "complete_exits": len(completed),
            "unresolved": len(unresolved_probes),
        },
        "opportunities_per_day": opportunity,
        "precursor_recall": recall,
        "watch_precision": precision,
        "false_watch_rate": false_watch,
        "lead_time_minutes": _distribution(lead_minutes),
        "mfe_pct": _distribution(row.max_favorable_return_pct for row in completed),
        "mae_pct": _distribution(row.max_adverse_return_pct for row in completed),
        "paper_win_rate": paper_win_rate,
        "profit_factor": positives / negatives if negatives > 0 else None,
        "trade_expectancy_pct": _mean(row.net_return_pct for row in completed),
        "cash_inclusive_expectancy_pct": cash_inclusive_return,
        "costs": {
            "cost_model_version": FROZEN_PAPER_CONTRACT.cost_model_version,
            "fees_usd_total": sum(row.fees_usd or 0.0 for row in completed),
            "funding_usd_total": sum(row.funding_usd or 0.0 for row in completed),
            "entry_spread_bps": _distribution(row.entry_spread_bps for row in completed),
            "entry_impact_bps": _distribution(row.entry_impact_bps for row in completed),
            "exit_spread_bps": _distribution(row.exit_spread_bps for row in completed),
            "exit_impact_bps": _distribution(row.exit_impact_bps for row in completed),
            "note": "VWAP already includes observed spread and book impact",
        },
        "max_drawdown_usd": _max_drawdown_usd(completed),
        "holding_time_minutes": _distribution(
            (row.exit_at - row.entry_at).total_seconds() / 60
            if row.exit_at is not None and row.entry_at is not None
            else None
            for row in completed
        ),
        "capital_occupancy": {
            "position_days": sum(
                (row.exit_at - row.entry_at).total_seconds() / 86400
                for row in completed
                if row.exit_at is not None and row.entry_at is not None
            ),
            "window_days": (until - effective_since).total_seconds() / 86400,
        },
        "entry_capacity": {
            "quoted_probes": len([row for row in probes if row.entry_status == "opened"]),
            "rejected_probes": len(rejected),
            "filled_notional_usd": _distribution(
                row.entry_filled_notional_usd for row in probes if row.entry_status == "opened"
            ),
            "liquidity_capacity_usd": None,
            "unresolved_reason": "the frozen $50 probe does not measure book depth above its size",
        },
        "latency_decomposition": {
            "watch": _watch_latency(watches),
            "entry_quote_ms": _distribution(row.entry_quote_latency_ms for row in probes),
            "exit_quote_ms": _distribution(row.exit_quote_latency_ms for row in completed),
        },
        "stability": _stability(completed),
    }
    return results, readiness, availability["gaps"]


def build_discovery_report(
    dataset: DiscoveryDataset,
    *,
    since: datetime,
    until: datetime,
    capture_epoch_started_at: datetime,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    venue_readiness: dict[str, Any] = {}
    downtime_intervals: dict[str, Any] = {}
    for versions in VENUE_VERSIONS:
        venue_result, readiness, gaps = _venue_result(
            dataset, versions=versions, since=since, until=until
        )
        results[versions.exchange] = venue_result
        venue_readiness[versions.exchange] = readiness
        downtime_intervals[versions.exchange] = gaps

    contracts = {
        item.exchange: {
            "watch_version": item.watch_version,
            "watch_contract_sha256": item.watch_contract_sha256,
            "paper_version": item.paper_version,
            "paper_contract_sha256": item.paper_contract_sha256,
        }
        for item in VENUE_VERSIONS
    }
    contract_hash = hashlib.sha256(
        json.dumps(contracts, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    input_fingerprint = hashlib.sha256(
        json.dumps(
            json_ready(
                {
                    "watches": [asdict(row) for row in dataset.watches],
                    "pumps": [asdict(row) for row in dataset.pumps],
                    "probes": [asdict(row) for row in dataset.probes],
                    "pump_observability": {
                        key: asdict(value)
                        for key, value in sorted(dataset.pump_observability.items())
                    },
                    "available_minutes": dataset.available_minutes,
                    "watch_runs": {
                        key: asdict(value) for key, value in sorted(dataset.watch_runs.items())
                    },
                    "paper_runs": {
                        key: asdict(value) for key, value in sorted(dataset.paper_runs.items())
                    },
                }
            ),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    ready = all(row["status"] == "READY_FOR_MANUAL_REVIEW" for row in venue_readiness.values())
    return {
        "report_version": REPORT_VERSION,
        "interpretation": REPORT_INTERPRETATION,
        "code_revision": normalize_code_revision(code_revision),
        "working_tree_dirty": working_tree_dirty,
        "generated_at": generated_at,
        "contract_hash": contract_hash,
        "contracts": contracts,
        "cost_model_version": FROZEN_PAPER_CONTRACT.cost_model_version,
        "window": {"since": since, "until_exclusive": until},
        "cohort_boundaries": {
            "capture_epoch_started_at": capture_epoch_started_at,
            "watch": {key: value.cohort_started_at for key, value in dataset.watch_runs.items()},
            "paper": {key: value.cohort_started_at for key, value in dataset.paper_runs.items()},
        },
        "cutoff": until,
        "outcome_maturity_cutoff": generated_at
        - timedelta(minutes=FROZEN_PAPER_CONTRACT.max_hold_minutes),
        "input_fingerprint": input_fingerprint,
        "downtime_intervals": downtime_intervals,
        "readiness": {
            "status": "READY_FOR_MANUAL_REVIEW" if ready else "COLLECTING",
            "venues": venue_readiness,
        },
        "recommendation": "MANUAL_REVIEW_REQUIRED" if ready else "NOT_READY",
        "results": results,
    }


async def _run(
    args: argparse.Namespace,
    *,
    repository: MomentumFlowDiscoveryRepository | None = None,
    now: datetime | None = None,
) -> str:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for momentum-flow-discovery-report")
    if args.since >= args.until:
        raise ValueError("since must be strictly before until")
    for name, value in (
        ("since", args.since),
        ("until", args.until),
        ("capture epoch", args.capture_epoch_started_at),
    ):
        if value.utcoffset() != timedelta(0):
            raise ValueError(f"{name} must be UTC")
    if args.until - args.since > timedelta(days=MAX_WINDOW_DAYS):
        raise ValueError(f"maximum window width is {MAX_WINDOW_DAYS} days")
    if bool(args.working_tree_dirty) == bool(args.no_working_tree_dirty):
        raise ValueError("exactly one working-tree dirty flag is required")

    generated_at = now or datetime.now(UTC)
    maturity_cutoff = generated_at - timedelta(minutes=FROZEN_PAPER_CONTRACT.max_hold_minutes)
    if args.until > maturity_cutoff:
        raise ValueError("outcome window is not fully mature")

    state_path = _cohort_state_path(args.cohort_state_path, os.environ)
    accepted = load_accepted_cohort(state_path)
    capture_epoch_started_at, acceptance, changed = resolve_capture_cohort_started_at(
        requested=args.capture_epoch_started_at,
        accepted=accepted,
        accept_new_cohort=args.accept_new_cohort_boundary,
        now=generated_at,
    )
    if args.since < capture_epoch_started_at:
        raise ValueError("analysis window begins before the accepted capture cohort")

    owned_repository = repository is None
    repo = repository or MomentumFlowDiscoveryRepository.from_url(db_url)
    try:
        dataset = await repo.load(
            since=args.since,
            until=args.until,
            versions=VENUE_VERSIONS,
            pump_lead_minutes=PUMP_LEAD_MINUTES,
        )
        report = build_discovery_report(
            dataset,
            since=args.since,
            until=args.until,
            capture_epoch_started_at=capture_epoch_started_at,
            generated_at=generated_at,
            code_revision=args.code_revision,
            working_tree_dirty=args.working_tree_dirty,
        )
        payload = json.dumps(json_ready(report), indent=2, sort_keys=True)
    finally:
        if owned_repository:
            await repo.close()

    # Persist a new/re-baselined cohort only after a complete successful read. A DB
    # or calculation failure must not mutate research provenance.
    if changed:
        save_accepted_cohort(state_path, acceptance)
    return payload


def main() -> None:
    args = _parse_args()
    try:
        sys.stdout.write(asyncio.run(_run(args)) + "\n")
    except ValueError as error:
        sys.stderr.write(f"ERROR: {error}\n")
        raise SystemExit(1) from error
    except Exception as error:
        sys.stderr.write(f"ERROR: {error}\n")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
