"""Read-only, calibration-only report for `analysis/bybit-early-momentum-
event-study-v0`. Joins old (price/decision) data with the new Bybit
momentum-flow bars around real pump-event triggers, per the frozen
protocol in `momentum_flow_protocol.py`. This report NEVER emits a
p-value, Holm correction, promotion verdict, or profitability claim -- see
that module's own "Banned claims" section. It only describes what the
calibration window looks like.

Intended to run locally through the DB tunnel (`make db-tunnel` or
equivalent), never as a `prod-*` Makefile target: it makes live CCXT
OHLCV calls per pump event across the full lookback window, which is
exactly the VPS-load pattern the 2026-08-11 canary queue-pressure
incident showed this host cannot currently absorb alongside the always-on
collector (see ROADMAP.md item 6). No Makefile target in this PR points
it at production; only `token-behavior-discovery-report`-style manual
invocation via `--database-url`/`DATABASE_URL` pointed at a tunnel.

The event cohort itself comes from `momentum_flow_event_repository.py`,
NOT `replay.py`/`replay_repository.py` -- see that module's own docstring
on why reusing the outcome-challenger eligibility contract would have
biased this report's cohort (colleague review, 2026-08-12).
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
from statistics import fmean, median
from typing import TYPE_CHECKING, Any

from .momentum_flow_bars_repository import (
    MomentumFlowBarsRepository,
    bybit_linear_symbol,
)
from .momentum_flow_event_repository import MOMENTUM_FLOW_EVENT_COHORT_VERSION
from .momentum_flow_protocol import (
    CALIBRATION_WINDOW_UNTIL,
    EXIT_HORIZONS_MINUTES,
    FLOW_AVAILABLE,
    FLOW_GAP_EXCLUDED,
    FLOW_PARTIAL_COVERAGE,
    FLOW_UNAVAILABLE_PRE_CAPTURE,
    LOOKBACK_OFFSETS_MINUTES,
    MOMENTUM_FLOW_BARS_AVAILABLE_FROM,
    MOMENTUM_FLOW_FAMILY,
    MOMENTUM_FLOW_LANES,
    MOMENTUM_FLOW_PROTOCOL_VERSION,
    event_is_mature,
)
from .momentum_flow_timeline import (
    MOMENTUM_FLOW_TIMELINE_VERSION,
    EventTimeline,
    FlowBar,
    PriceBar,
    build_event_timeline,
)
from .ohlcv import TIMEFRAME, TIMEFRAME_MS, fetch_symbol_candles
from .reporting import (
    ReportWindowNotStartedError,
    json_ready,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
)

if TYPE_CHECKING:
    from .momentum_flow_event_repository import MeasurementEvent

EVENT_STUDY_REPORT_VERSION = "momentum_flow_event_study_report_v2"
_LOOKBACK_START_MS = LOOKBACK_OFFSETS_MINUTES[0] * 60_000
_LOOKBACK_END_MS = LOOKBACK_OFFSETS_MINUTES[-1] * 60_000
_MAX_CONCURRENT_FETCHES = 4

# Price-fetch outcome statuses, tracked per event so a discrepancy between
# two runs of this report can actually be explained (colleague review,
# 2026-08-11: the original version swallowed every exception and kept
# only aggregated means, with no record of exclusion reason, CCXT/source
# identity, or event-level inputs).
FETCH_STATUS_FETCHED = "fetched"
FETCH_STATUS_EMPTY_RESULT = "empty_result"
FETCH_STATUS_FAILED = "fetch_failed"
FETCH_STATUS_IMMATURE = "immature_window_skipped"
FETCH_STATUS_UNSUPPORTED_EXCHANGE = "unsupported_exchange"


@dataclass(frozen=True)
class PriceFetchResult:
    status: str
    bars: tuple[PriceBar, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class LookbackAggregate:
    offset_minutes: int
    event_count: int
    price_available_count: int
    mean_price_change_pct: float | None
    median_price_change_pct: float | None
    mean_realized_volatility: float | None
    flow_available_count: int
    flow_partial_coverage_count: int
    flow_gap_excluded_count: int
    flow_unavailable_pre_capture_count: int
    # Resolved independently of flow_available_count -- see the module's
    # own comment in aggregate_lookback on why OI is never gated by
    # buy/sell coverage.
    oi_available_count: int
    mean_oi_change_pct: float | None
    oi_value_available_count: int
    mean_oi_value_change_pct: float | None
    mean_net_flow_notional_usd: float | None
    mean_buy_notional_usd: float | None
    mean_sell_notional_usd: float | None


@dataclass(frozen=True)
class EventExclusionReason:
    reason: str
    count: int


@dataclass(frozen=True)
class EventRecord:
    """Per-event provenance for archival/reproducibility -- see this
    module's own docstring on why the original version's aggregated-only
    output could not explain a discrepancy between two runs. `flow_bar_
    count`/`any_complete_flow_bar` are scoped to THIS event's own
    -24h..+4h window, never the full multi-day symbol history a shared
    batch query happened to load (colleague review, 2026-08-12: the
    original version conflated the two, making both numbers meaningless
    for a specific event).

    `any_complete_flow_bar` means only "at least one bar in this event's
    own window has `complete=True`" -- a much weaker claim than the
    report-level `events_with_any_flow` (which requires a lookback point
    to reach FULL cumulative coverage). Named accordingly (colleague
    review, 2026-08-12) so the two are never confused for the same
    thing."""

    pump_event_id: int
    base: str
    exchange: str
    identity_key: str
    market_id: str
    unified_symbol: str
    market_type: str
    onboarded_at: datetime
    trigger_at: datetime
    price_fetch_status: str
    price_bar_count: int
    price_fetch_error: str | None
    flow_bar_count: int
    any_complete_flow_bar: bool


@dataclass(frozen=True)
class EventStudyManifest:
    protocol_version: str
    timeline_version: str
    report_version: str
    event_cohort_version: str
    ccxt_version: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    dataset_since: datetime | None
    dataset_until: datetime
    calibration_window_until: datetime
    flow_bars_available_from: datetime
    flow_source_exchange: str
    # Three separate fingerprints, not one: the event cohort (WHICH
    # events were in scope), the price inputs actually fetched, and the
    # flow inputs actually used can each drift independently between two
    # runs (a symbol re-listing, a fetch retry landing different data, a
    # collector backfill). Colleague review, 2026-08-12: one fingerprint
    # covering only decision identity could not explain a numeric
    # discrepancy caused by either of the other two.
    event_cohort_fingerprint: str
    price_input_fingerprint: str
    flow_input_fingerprint: str
    family: str
    lanes: tuple[str, ...]
    lookback_offsets_minutes: tuple[int, ...]
    exit_horizons_minutes: tuple[int, ...]
    report_scope: str = "calibration_only_descriptive_no_promotion"


@dataclass(frozen=True)
class EventStudyReport:
    manifest: EventStudyManifest
    cohort_events: int
    event_exclusion_reasons: tuple[EventExclusionReason, ...]
    events_with_timeline: int
    events_with_any_flow: int
    events_entirely_pre_capture: int
    lookback_aggregates: tuple[LookbackAggregate, ...]
    event_records: tuple[EventRecord, ...]


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def _median(values: list[float]) -> float | None:
    return median(values) if values else None


def aggregate_lookback(
    offset_minutes: int, timelines: tuple[EventTimeline, ...]
) -> LookbackAggregate:
    points = []
    for timeline in timelines:
        for point in timeline.points:
            if point.offset_minutes == offset_minutes:
                points.append(point)
                break
    price_changes = [p.price_change_pct for p in points if p.price_change_pct is not None]
    vols = [p.realized_volatility for p in points if p.realized_volatility is not None]
    # Only FULL coverage feeds the clean CUMULATIVE-SUM aggregates (buy/
    # sell/net-flow) -- FLOW_PARTIAL_COVERAGE is tracked (count only) but
    # excluded here the same way FLOW_GAP_EXCLUDED already is, per
    # momentum_flow_protocol.py's amended completeness rule: an
    # undercounted sum must never blend into a mean presented as if every
    # contributing point were fully covered.
    #
    # OI is NOT a sum -- it is a point-in-time level, resolved
    # independently of the buy/sell coverage gate (see
    # momentum_flow_timeline.py's own `_closest_known_oi_at_or_before`).
    # Filtering oi_changes through the flow-coverage-gated `available` list
    # here would silently reintroduce exactly the OI/flow coupling the
    # timeline engine was fixed to remove (colleague review, 2026-08-12):
    # a single missing trade-minute would drop an otherwise-valid OI
    # reading from the mean for no OI-related reason at all. oi_changes is
    # therefore drawn from ALL points, gated only by oi_change_pct itself
    # being resolved.
    available = [p for p in points if p.flow_availability == FLOW_AVAILABLE]
    oi_changes = [p.oi_change_pct for p in points if p.oi_change_pct is not None]
    oi_value_changes = [p.oi_value_change_pct for p in points if p.oi_value_change_pct is not None]
    net_flows = [p.net_flow_notional_usd for p in available if p.net_flow_notional_usd is not None]
    buys = [p.buy_notional_usd for p in available if p.buy_notional_usd is not None]
    sells = [p.sell_notional_usd for p in available if p.sell_notional_usd is not None]
    return LookbackAggregate(
        offset_minutes=offset_minutes,
        event_count=len(points),
        price_available_count=sum(1 for p in points if p.price_available),
        mean_price_change_pct=_mean(price_changes),
        median_price_change_pct=_median(price_changes),
        mean_realized_volatility=_mean(vols),
        flow_available_count=len(available),
        flow_partial_coverage_count=sum(
            1 for p in points if p.flow_availability == FLOW_PARTIAL_COVERAGE
        ),
        flow_gap_excluded_count=sum(1 for p in points if p.flow_availability == FLOW_GAP_EXCLUDED),
        flow_unavailable_pre_capture_count=sum(
            1 for p in points if p.flow_availability == FLOW_UNAVAILABLE_PRE_CAPTURE
        ),
        oi_available_count=len(oi_changes),
        mean_oi_change_pct=_mean(oi_changes),
        oi_value_available_count=len(oi_value_changes),
        mean_oi_value_change_pct=_mean(oi_value_changes),
        mean_net_flow_notional_usd=_mean(net_flows),
        mean_buy_notional_usd=_mean(buys),
        mean_sell_notional_usd=_mean(sells),
    )


def _sha256_json(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _event_cohort_fingerprint(events: tuple[MeasurementEvent, ...]) -> str:
    return _sha256_json(
        [
            {
                "pump_event_id": e.pump_event_id,
                "base": e.base,
                "exchange": e.exchange,
                "identity_key": e.identity_key,
                "market_id": e.market_id,
                "unified_symbol": e.unified_symbol,
                "market_type": e.market_type,
                "onboarded_at": e.onboarded_at.isoformat(),
                "trigger_at": e.trigger_at.isoformat(),
            }
            for e in sorted(events, key=lambda e: e.pump_event_id)
        ]
    )


def _price_input_fingerprint(price_fetch_by_event: dict[int, PriceFetchResult]) -> str:
    return _sha256_json(
        [
            {
                "pump_event_id": event_id,
                "status": price_fetch_by_event[event_id].status,
                "error": price_fetch_by_event[event_id].error,
                "bars": [
                    {"ts_ms": bar.ts_ms, "close": bar.close, "duration_ms": bar.duration_ms}
                    for bar in sorted(price_fetch_by_event[event_id].bars, key=lambda b: b.ts_ms)
                ],
            }
            for event_id in sorted(price_fetch_by_event)
        ]
    )


def _flow_input_fingerprint(flow_bars_by_event: dict[int, tuple[FlowBar, ...]]) -> str:
    return _sha256_json(
        [
            {
                "pump_event_id": event_id,
                "bucket_start_ms": bar.bucket_start_ms,
                "open_interest": bar.open_interest,
                "open_interest_value": bar.open_interest_value,
                "open_interest_event_at_ms": bar.open_interest_event_at_ms,
                "open_interest_observed_at_ms": bar.open_interest_observed_at_ms,
                "open_interest_value_event_at_ms": bar.open_interest_value_event_at_ms,
                "open_interest_value_observed_at_ms": (bar.open_interest_value_observed_at_ms),
                "buy_total_notional_usd": bar.buy_total_notional_usd,
                "sell_total_notional_usd": bar.sell_total_notional_usd,
                "ticker_observed_this_minute": bar.ticker_observed_this_minute,
                "complete": bar.complete,
            }
            for event_id in sorted(flow_bars_by_event)
            for bar in sorted(flow_bars_by_event[event_id], key=lambda b: b.bucket_start_ms)
        ]
    )


def build_momentum_flow_event_study_report(
    events: tuple[MeasurementEvent, ...],
    *,
    price_fetch_by_event: dict[int, PriceFetchResult],
    flow_bars_by_event: dict[int, tuple[FlowBar, ...]],
    since: datetime | None,
    until: datetime,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    ccxt_version: str,
    pre_fetch_exclusions: tuple[tuple[str, int], ...] = (),
) -> EventStudyReport:
    """`pre_fetch_exclusions`: events dropped upstream of this function
    (currently: `momentum_flow_event_repository.MeasurementCohort`'s own
    identity-selection funnel -- an event with zero non-conflicted,
    identity-confirmed sources never becomes a `MeasurementEvent` at all,
    so it must be folded in here to keep the funnel honest about the
    TRUE starting population, not just what already made it through
    upstream filtering)."""
    revision = normalize_code_revision(code_revision)
    if until != CALIBRATION_WINDOW_UNTIL:
        raise ValueError(
            "the calibration-only event study requires the registered "
            f"calibration window end ({CALIBRATION_WINDOW_UNTIL.isoformat()}); "
            "extending past it would consume data reserved for the later "
            "untouched forward-contract confirmation window"
        )

    timelines: list[EventTimeline] = []
    event_records: list[EventRecord] = []
    exclusion_reasons: Counter[str] = Counter(dict(pre_fetch_exclusions))

    for event in events:
        if not event_is_mature(event.trigger_at, until):
            exclusion_reasons[FETCH_STATUS_IMMATURE] += 1
            event_records.append(
                EventRecord(
                    pump_event_id=event.pump_event_id,
                    base=event.base,
                    exchange=event.exchange,
                    identity_key=event.identity_key,
                    market_id=event.market_id,
                    unified_symbol=event.unified_symbol,
                    market_type=event.market_type,
                    onboarded_at=event.onboarded_at,
                    trigger_at=event.trigger_at,
                    price_fetch_status=FETCH_STATUS_IMMATURE,
                    price_bar_count=0,
                    price_fetch_error=None,
                    flow_bar_count=0,
                    any_complete_flow_bar=False,
                )
            )
            continue

        fetch_result = price_fetch_by_event.get(
            event.pump_event_id, PriceFetchResult(status=FETCH_STATUS_UNSUPPORTED_EXCHANGE)
        )
        # Scoped to this event's own window already (see _run()'s own
        # windowing) -- never the full multi-day symbol history a shared
        # batch query loaded. See EventRecord's own docstring.
        flow_bars = flow_bars_by_event.get(event.pump_event_id, ())
        event_records.append(
            EventRecord(
                pump_event_id=event.pump_event_id,
                base=event.base,
                exchange=event.exchange,
                identity_key=event.identity_key,
                market_id=event.market_id,
                unified_symbol=event.unified_symbol,
                market_type=event.market_type,
                onboarded_at=event.onboarded_at,
                trigger_at=event.trigger_at,
                price_fetch_status=fetch_result.status,
                price_bar_count=len(fetch_result.bars),
                price_fetch_error=fetch_result.error,
                flow_bar_count=len(flow_bars),
                any_complete_flow_bar=any(bar.complete for bar in flow_bars),
            )
        )
        if not fetch_result.bars:
            exclusion_reasons[fetch_result.status] += 1
            continue

        timelines.append(
            build_event_timeline(
                pump_event_id=event.pump_event_id,
                base=event.base,
                trigger_at=event.trigger_at,
                price_bars=fetch_result.bars,
                flow_bars=flow_bars,
                lookback_offsets_minutes=LOOKBACK_OFFSETS_MINUTES,
            )
        )

    timelines_tuple = tuple(timelines)
    aggregates = tuple(
        aggregate_lookback(offset, timelines_tuple) for offset in LOOKBACK_OFFSETS_MINUTES
    )

    return EventStudyReport(
        manifest=EventStudyManifest(
            protocol_version=MOMENTUM_FLOW_PROTOCOL_VERSION,
            timeline_version=MOMENTUM_FLOW_TIMELINE_VERSION,
            report_version=EVENT_STUDY_REPORT_VERSION,
            event_cohort_version=MOMENTUM_FLOW_EVENT_COHORT_VERSION,
            ccxt_version=ccxt_version,
            code_revision=revision,
            working_tree_dirty=working_tree_dirty,
            generated_at=generated_at,
            dataset_since=since,
            dataset_until=until,
            calibration_window_until=CALIBRATION_WINDOW_UNTIL,
            flow_bars_available_from=MOMENTUM_FLOW_BARS_AVAILABLE_FROM,
            flow_source_exchange="bybit",
            event_cohort_fingerprint=_event_cohort_fingerprint(events),
            price_input_fingerprint=_price_input_fingerprint(price_fetch_by_event),
            flow_input_fingerprint=_flow_input_fingerprint(flow_bars_by_event),
            family=MOMENTUM_FLOW_FAMILY,
            lanes=MOMENTUM_FLOW_LANES,
            lookback_offsets_minutes=LOOKBACK_OFFSETS_MINUTES,
            exit_horizons_minutes=EXIT_HORIZONS_MINUTES,
        ),
        cohort_events=len(events) + sum(count for _, count in pre_fetch_exclusions),
        event_exclusion_reasons=tuple(
            EventExclusionReason(reason, count)
            for reason, count in sorted(
                exclusion_reasons.items(), key=lambda item: (-item[1], item[0])
            )
        ),
        events_with_timeline=len(timelines_tuple),
        events_with_any_flow=sum(1 for t in timelines_tuple if t.any_flow_available),
        # Genuinely "every single lookback point is pre-capture" -- NOT
        # "no point happened to reach full coverage for any reason",
        # which the property this replaced conflated (colleague review,
        # 2026-08-12): a post-capture event stuck at PARTIAL_COVERAGE or
        # GAP_EXCLUDED everywhere is a coverage problem, not evidence the
        # event predates the capture window.
        events_entirely_pre_capture=sum(
            1
            for t in timelines_tuple
            if all(p.flow_availability == FLOW_UNAVAILABLE_PRE_CAPTURE for p in t.points)
        ),
        lookback_aggregates=aggregates,
        event_records=tuple(event_records),
    )


def render_json(report: EventStudyReport) -> str:
    return json.dumps(json_ready(asdict(report)), indent=2, sort_keys=True, allow_nan=False)


def _dataset_since_label(dataset_since: datetime | None) -> str:
    return dataset_since.isoformat() if dataset_since is not None else "(earliest available)"


def render_markdown(report: EventStudyReport) -> str:
    manifest = report.manifest
    lines = [
        "# Bybit Early-Momentum Event Study (Calibration Pass, No Promotion)",
        "",
        f"Generated: {manifest.generated_at.isoformat()}",
        f"Code revision: `{manifest.code_revision}`",
        f"Working tree dirty: {'yes' if manifest.working_tree_dirty else 'no'}",
        f"CCXT version: `{manifest.ccxt_version}`",
        f"Event cohort fingerprint: `{manifest.event_cohort_fingerprint}`",
        f"Price input fingerprint: `{manifest.price_input_fingerprint}`",
        f"Flow input fingerprint: `{manifest.flow_input_fingerprint}`",
        (
            f"Scope: {_dataset_since_label(manifest.dataset_since)} "
            f"<= trigger < {manifest.dataset_until.isoformat()}"
        ),
        f"Flow bars available from: {manifest.flow_bars_available_from.isoformat()}",
        f"Flow source exchange (cross-venue proxy): `{manifest.flow_source_exchange}`",
        f"Family: `{manifest.family}` -- lanes: {', '.join(manifest.lanes)}",
        "",
        (
            "> **This is a calibration-only descriptive pass with no matched-control "
            "baseline yet -- see `momentum_flow_protocol.py`'s \"What v0 actually "
            'answers" section. It computes no p-value, no Holm correction, no '
            "promotion verdict, and no profit factor, and never authorizes paper "
            "trading or a production change.**"
        ),
        "",
        "## Funnel",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Metric", "Value"),
            [
                ("Cohort events (measurement-only, no outcome required)", report.cohort_events),
                ("Events with a computed timeline", report.events_with_timeline),
                ("Events with any flow-enriched point", report.events_with_any_flow),
                ("Events entirely pre-dating flow capture", report.events_entirely_pre_capture),
            ],
        )
    )
    lines.extend(["", "## Exclusion reasons (cohort event -> no timeline)", ""])
    lines.extend(
        markdown_table(
            ("Reason", "Count"),
            [(row.reason, row.count) for row in report.event_exclusion_reasons],
        )
    )
    lines.extend(["", "## Per-lookback descriptive statistics", ""])
    header = (
        "Offset (min)",
        "N",
        "Price avail.",
        "Mean Δprice %",
        "Median Δprice %",
        "Mean realized vol",
        "Flow avail. (full)",
        "Flow partial",
        "Flow gap-excl.",
        "Flow pre-capture",
        "OI avail.",
        "Mean ΔOI %",
        "OI value avail.",
        "Mean ΔOI value %",
        "Mean net flow ($)",
    )
    rows = [
        (
            row.offset_minutes,
            row.event_count,
            row.price_available_count,
            _fmt(row.mean_price_change_pct),
            _fmt(row.median_price_change_pct),
            _fmt(row.mean_realized_volatility, 6),
            row.flow_available_count,
            row.flow_partial_coverage_count,
            row.flow_gap_excluded_count,
            row.flow_unavailable_pre_capture_count,
            row.oi_available_count,
            _fmt(row.mean_oi_change_pct),
            row.oi_value_available_count,
            _fmt(row.mean_oi_value_change_pct),
            _fmt(row.mean_net_flow_notional_usd, 2),
        )
        for row in report.lookback_aggregates
    ]
    lines.extend(markdown_table(header, rows))
    lines.extend(
        [
            "",
            (
                "_Full per-event provenance (exact identity, fetch status/error, bar "
                "counts, exchange) is in the JSON output's `event_records`, not this "
                "table -- use it to explain any discrepancy between two runs._"
            ),
            "",
            (
                "_At most one of the three lanes above may be nominated as a "
                "forward candidate from a later canonical statistical read of "
                "this family -- never from this calibration pass. See "
                "`momentum_flow_protocol.py`._"
            ),
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def _fmt(value: float | None, digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Calibration-only event study for the momentum_flow_state_v1 family. "
            "Read-only; makes no production change and no promotion claim."
        )
    )
    parser.add_argument("--since", type=parse_utc_datetime, default=None)
    parser.add_argument(
        "--until",
        type=parse_utc_datetime,
        default=CALIBRATION_WINDOW_UNTIL,
        help="fixed to the registered calibration window end; overriding it is rejected",
    )
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty",
        action=argparse.BooleanOptionalAction,
        required=True,
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


async def _fetch_price_bars_by_event(
    events: tuple[MeasurementEvent, ...],
    factories: dict[str, Any],
) -> dict[int, PriceFetchResult]:
    """One exchange client per venue, reused across every event on that
    venue -- same shape as `virtual_market.fetch_decision_market_paths`,
    reimplemented here rather than importing that module's private helper
    (see this module's own docstring on why this stays local-only). Every
    outcome (success, empty result, or a specific exception) is recorded
    per event -- see `PriceFetchResult` and this module's own docstring on
    the colleague review that caught the original bare-except version.
    Only mature events are passed in by `_run()`; this function assumes
    that filtering already happened."""
    by_exchange: dict[str, list[MeasurementEvent]] = {}
    unsupported: list[int] = []
    for event in events:
        if event.exchange not in factories:
            unsupported.append(event.pump_event_id)
            continue
        by_exchange.setdefault(event.exchange, []).append(event)

    results: dict[int, PriceFetchResult] = {
        event_id: PriceFetchResult(status=FETCH_STATUS_UNSUPPORTED_EXCHANGE)
        for event_id in unsupported
    }
    for exchange_name, exchange_events in sorted(by_exchange.items()):
        exchange = factories[exchange_name]()
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_FETCHES)

        async def fetch_one(
            event: MeasurementEvent,
            exchange: Any = exchange,
            semaphore: asyncio.Semaphore = semaphore,
        ) -> None:
            # exchange/semaphore bound as default arguments, not closed
            # over from the enclosing loop: this function is redefined
            # fresh every iteration, so a plain closure would (harmlessly,
            # since every gather() below completes before the next
            # iteration rebinds either one, but still flagged by the
            # linter as a general late-binding risk) reference whatever is
            # current at call time.
            trigger_ms = int(event.trigger_at.timestamp() * 1000)
            anchor_ms = trigger_ms + _LOOKBACK_START_MS
            # closed_candles() (ohlcv.py) keeps only bars with `ts_ms >=
            # start_ms`. The anchor point's own price needs the bar that
            # CLOSES (becomes known) exactly at (or just after) the
            # anchor instant. A REAL exchange candle grid is aligned to
            # UTC-epoch multiples of TIMEFRAME_MS (e.g. :00/:05/:10), NOT
            # to the anchor's own arbitrary sub-minute offset -- a
            # colleague review (2026-08-12) caught that `anchor_ms -
            # TIMEFRAME_MS` alone still misses the needed candle whenever
            # trigger_at has non-zero seconds (e.g. an anchor at :00:41
            # needs the candle opening at :55:00, not :55:41). Floor the
            # anchor down to the grid FIRST, then step back one bar.
            grid_floor_ms = anchor_ms // TIMEFRAME_MS * TIMEFRAME_MS
            start_ms = grid_floor_ms - TIMEFRAME_MS
            end_ms = trigger_ms + _LOOKBACK_END_MS
            try:
                async with semaphore:
                    candles = await fetch_symbol_candles(
                        exchange,
                        event.unified_symbol,
                        start_ms,
                        end_ms,
                        timeframe=TIMEFRAME,
                        timeframe_ms=TIMEFRAME_MS,
                    )
            except Exception as exc:
                results[event.pump_event_id] = PriceFetchResult(
                    status=FETCH_STATUS_FAILED,
                    error=f"{type(exc).__name__}: {str(exc)[:200]}",
                )
                return
            bars = tuple(
                PriceBar(ts_ms=candle.ts_ms, close=candle.close, duration_ms=TIMEFRAME_MS)
                for candle in candles
            )
            results[event.pump_event_id] = PriceFetchResult(
                status=FETCH_STATUS_FETCHED if bars else FETCH_STATUS_EMPTY_RESULT,
                bars=bars,
            )

        try:
            for offset in range(0, len(exchange_events), _MAX_CONCURRENT_FETCHES):
                batch = exchange_events[offset : offset + _MAX_CONCURRENT_FETCHES]
                await asyncio.gather(*(fetch_one(event) for event in batch))
        finally:
            await asyncio.gather(exchange.close(clean_instance_data=True), return_exceptions=True)
    return results


def _window_flow_bars(
    flow_bars_by_symbol: dict[str, list[FlowBar]],
    events: tuple[MeasurementEvent, ...],
) -> dict[int, tuple[FlowBar, ...]]:
    """Scope each event's own flow bars to its own -24h..+4h window,
    never the full multi-day symbol history a shared batch query loaded
    (colleague review, 2026-08-12 -- see `EventRecord`'s own docstring).
    """
    result: dict[int, tuple[FlowBar, ...]] = {}
    for event in events:
        trigger_ms = int(event.trigger_at.timestamp() * 1000)
        start_ms = trigger_ms + _LOOKBACK_START_MS
        end_ms = trigger_ms + _LOOKBACK_END_MS
        symbol_bars = flow_bars_by_symbol.get(bybit_linear_symbol(event.base), ())
        result[event.pump_event_id] = tuple(
            bar for bar in symbol_bars if start_ms <= bar.bucket_start_ms <= end_ms
        )
    return result


async def _run(args: argparse.Namespace) -> str:
    import ccxt

    from .exchange_registry import EXCHANGE_FACTORIES
    from .momentum_flow_event_repository import MomentumFlowEventRepository

    generated_at = datetime.now(UTC)
    if generated_at < CALIBRATION_WINDOW_UNTIL:
        raise ReportWindowNotStartedError(
            "the registered calibration window for momentum_flow_state_v1 ends at "
            f"{CALIBRATION_WINDOW_UNTIL.isoformat()}; this report refuses to run "
            "against live data before that instant, even though it makes no "
            "promotion claim -- see momentum_flow_protocol.py"
        )
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for momentum-flow-event-study-report")

    event_repository = MomentumFlowEventRepository.from_url(db_url)
    try:
        cohort = await event_repository.load(since=args.since, until=args.until)
    finally:
        await event_repository.close()
    all_events = cohort.events

    # Maturity filtering happens HERE, before any fetch: an event whose
    # full lookback span would reach past `until` never gets a live fetch
    # issued for it at all, which is what keeps the calibration/
    # confirmation boundary clean (see momentum_flow_protocol.py's "Event
    # maturity" section) rather than relying on a downstream clamp.
    mature_events = tuple(e for e in all_events if event_is_mature(e.trigger_at, args.until))

    price_fetch_by_event = await _fetch_price_bars_by_event(mature_events, EXCHANGE_FACTORIES)

    symbols = tuple(sorted({bybit_linear_symbol(event.base) for event in mature_events}))
    flow_repository = MomentumFlowBarsRepository.from_url(db_url)
    try:
        flow_rows = await flow_repository.load(
            symbols=symbols,
            since=MOMENTUM_FLOW_BARS_AVAILABLE_FROM,
            until=CALIBRATION_WINDOW_UNTIL,
        )
    finally:
        await flow_repository.close()

    flow_bars_by_symbol: dict[str, list[FlowBar]] = {}
    for row in flow_rows:
        flow_bars_by_symbol.setdefault(row.symbol, []).append(
            FlowBar(
                bucket_start_ms=int(row.bucket_start.timestamp() * 1000),
                close_price=row.close_price,
                open_interest=row.open_interest,
                open_interest_value=row.open_interest_value,
                open_interest_event_at_ms=(
                    int(row.open_interest_event_at.timestamp() * 1000)
                    if row.open_interest_event_at is not None
                    else None
                ),
                open_interest_observed_at_ms=(
                    int(row.open_interest_observed_at.timestamp() * 1000)
                    if row.open_interest_observed_at is not None
                    else None
                ),
                open_interest_value_event_at_ms=(
                    int(row.open_interest_value_event_at.timestamp() * 1000)
                    if row.open_interest_value_event_at is not None
                    else None
                ),
                open_interest_value_observed_at_ms=(
                    int(row.open_interest_value_observed_at.timestamp() * 1000)
                    if row.open_interest_value_observed_at is not None
                    else None
                ),
                buy_total_notional_usd=row.buy_total_notional_usd,
                sell_total_notional_usd=row.sell_total_notional_usd,
                ticker_observed_this_minute=row.ticker_observed_this_minute,
                complete=row.complete,
            )
        )
    flow_bars_by_event = _window_flow_bars(flow_bars_by_symbol, mature_events)

    report = build_momentum_flow_event_study_report(
        all_events,
        price_fetch_by_event=price_fetch_by_event,
        flow_bars_by_event=flow_bars_by_event,
        since=args.since,
        until=args.until,
        generated_at=generated_at,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
        ccxt_version=ccxt.__version__,
        pre_fetch_exclusions=cohort.exclusion_reasons,
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
