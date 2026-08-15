"""Momentum-flow episode study: descriptive prerequisites for HYP-014.

Implements the `analysis/momentum-flow-episode-study-v1` ROADMAP item. This
report does NOT confirm `momentum_flow_state_v1` (HYP-014, `discovery-
ledger.md`, status `parked`) and does not move it out of `parked` -- it
produces the measurement prerequisites HYP-014's own `confirmation_
requirement` lists (untouched forward cohort, matched controls, WATCH
recall/lead-time). Exact-venue after-cost economics, false-WATCH-rate
precision, capacity, week/asset concentration sensitivity, and the
Holm-corrected family read remain a later report once this one shows the
prerequisites are actually satisfiable. See the manifest's own
`interpretation` field, which is the durable, machine-readable version of
this paragraph.

Primary scope is Bybit-native pump events only (`event.exchange == "bybit"`
in the event cohort): the pump was first observed ON Bybit, and Bybit's own
flow/OI is analyzed around it -- an exact-venue read, not a cross-venue
proxy. Events whose earliest source was a different exchange are counted
in the coverage funnel as a separate `cross_venue_secondary` bucket and are
not part of the primary descriptive comparison in this version: mixing them
in would silently reintroduce the source-lead timing question (HYP-012) into
what is supposed to be a same-venue flow-precursor question.

No CCXT calls. Every price/flow/OI reading comes from already-captured
`timeseries.bybit_momentum_bars_1m` rows. Bars are still fetched and
processed one symbol at a time and released before the next (see `_run` and
the `bars_for_symbol` callback below): "no CCXT" bounds one class of memory
risk, not the total one -- a growing capture epoch can still hold weeks of
one-minute rows per symbol, and this report must not require every symbol's
rows resident at once to stay a safe `prod-*` Makefile target (colleague
review, before any real run).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from statistics import fmean, median
from typing import TYPE_CHECKING, Any

from .momentum_flow_bars_repository import MomentumFlowBarsRepository
from .momentum_flow_capture_contract import BYBIT_MOMENTUM_EXCHANGE, BYBIT_MOMENTUM_MARKET_TYPE
from .momentum_flow_cohort_acceptance import (
    COHORT_STATE_ENV_VAR,
    load_accepted_cohort,
    resolve_capture_cohort_started_at,
    resolve_cohort_state_path,
    save_accepted_cohort,
)
from .momentum_flow_event_repository import (
    MOMENTUM_FLOW_EVENT_COHORT_VERSION,
    MeasurementEvent,
    MomentumFlowEventRepository,
)
from .momentum_flow_matched_controls import (
    CONTROL_SELECTOR_VERSION,
    ControlBalance,
    candidate_control_instants,
    evaluate_control_balance,
)
from .momentum_flow_protocol import (
    LOOKBACK_OFFSETS_MINUTES,
    MOMENTUM_FLOW_FAMILY,
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
from .momentum_flow_watch_contract import WATCH_CONTRACT_SHA256, WATCH_VERSION
from .momentum_flow_watch_linkage_repository import (
    WATCH_LINKAGE_VERSION,
    InstrumentWindow,
    MomentumFlowWatchLinkageRepository,
    WatchLinkage,
)
from .reporting import json_ready, markdown_table, normalize_code_revision, parse_utc_datetime

if TYPE_CHECKING:
    from collections.abc import Callable

    from .momentum_flow_bars_repository import MomentumFlowBarRow

REPORT_VERSION = "momentum_flow_episode_study_v1"
REPORT_INTERPRETATION = "measurement_prerequisites_for_hyp_014_not_confirmation"

# Matches LOOKBACK_OFFSETS_MINUTES[0] (-1440 minutes): an event cannot have a
# usable accumulation window before this much capture history exists.
REQUIRED_PRE_WINDOW = timedelta(minutes=abs(LOOKBACK_OFFSETS_MINUTES[0]))

# Liquidity segment bucketing only. Diagnostic labels -- see momentum_flow_
# matched_controls.py's own module docstring on why v1 does not rank
# controls by this.
LIQUIDITY_BUCKET_LOW_USD = 25_000.0
LIQUIDITY_BUCKET_HIGH_USD = 250_000.0


@dataclass(frozen=True)
class EpisodeStudyManifest:
    report_version: str
    protocol_version: str
    family: str
    event_cohort_version: str
    timeline_version: str
    control_selector_version: str
    watch_linkage_version: str
    watch_version: str
    watch_contract_sha256: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    capture_epoch_started_at: datetime
    watch_cohort_started_at: datetime | None
    dataset_since: datetime
    dataset_until_exclusive: datetime
    watch_denominator_since: datetime | None
    event_input_fingerprint: str
    bars_input_fingerprint: str
    lookback_offsets_minutes: tuple[int, ...]
    interpretation: str = REPORT_INTERPRETATION


@dataclass(frozen=True)
class CountRow:
    name: str
    count: int


@dataclass(frozen=True)
class EpisodeResult:
    pump_event_id: int
    base: str
    exchange: str
    trigger_at: datetime
    status: str
    control_at: datetime | None
    control_offset_days: int | None
    control_attempts: int
    balance: ControlBalance | None
    watch: WatchLinkage | None
    event_timeline: EventTimeline | None
    control_timeline: EventTimeline | None
    liquidity_bucket: str | None
    repeat_token: bool


@dataclass(frozen=True)
class LookbackComparisonRow:
    offset_minutes: int
    price_pair_n: int
    mean_event_price_change_pct: float | None
    mean_control_price_change_pct: float | None
    mean_price_paired_delta_pct: float | None
    oi_pair_n: int
    mean_event_oi_change_pct: float | None
    mean_control_oi_change_pct: float | None
    mean_oi_paired_delta_pct: float | None
    flow_pair_n: int
    mean_event_net_flow_usd: float | None
    mean_control_net_flow_usd: float | None
    mean_flow_paired_delta_usd: float | None


@dataclass(frozen=True)
class WatchRecallSummary:
    denominator_events: int
    watch_before_trigger: int
    recall_pct: float | None
    median_lead_minutes: float | None
    watch_only_after_trigger: int
    watch_only_after_trigger_pct: float | None
    # Mature, in-scope-by-time events excluded from `denominator_events`
    # because WATCH evaluation coverage over their own pre-trigger span was
    # insufficient (`WatchLinkage.watch_observable` is False) -- the worker
    # was not verifiably running, so an absent `watch` decision there cannot
    # be counted as a genuine miss (amended after second colleague review,
    # before any real run).
    unresolved_events: int


@dataclass(frozen=True)
class SegmentRow:
    dimension: str
    bucket: str
    episodes: int
    watch_recall_pct: float | None


@dataclass(frozen=True)
class EpisodeStudyReport:
    manifest: EpisodeStudyManifest
    # The identity-ready cohort (`len(events)`), i.e. ALREADY excludes
    # `no_identity_ready_earliest_source` and any other upstream identity-
    # funnel exclusion reason -- those are counted in `exclusion_reasons`
    # instead, not folded into this count. See `render_markdown`'s own
    # "Identity-ready cohort events" row label.
    dataset_events: int
    exclusion_reasons: tuple[CountRow, ...]
    complete_episodes: int
    watch_recall: WatchRecallSummary
    lookback_comparison: tuple[LookbackComparisonRow, ...]
    segments: tuple[SegmentRow, ...]
    episode_results: tuple[EpisodeResult, ...]


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def _median(values: list[float]) -> float | None:
    return median(values) if values else None


def _to_flow_bar(row: MomentumFlowBarRow) -> FlowBar:
    def _ms(value: datetime | None) -> int | None:
        return int(value.timestamp() * 1000) if value is not None else None

    return FlowBar(
        bucket_start_ms=int(row.bucket_start.timestamp() * 1000),
        close_price=row.close_price,
        open_interest=row.open_interest,
        open_interest_value=row.open_interest_value,
        open_interest_event_at_ms=_ms(row.open_interest_event_at),
        open_interest_observed_at_ms=_ms(row.open_interest_observed_at),
        open_interest_value_event_at_ms=_ms(row.open_interest_value_event_at),
        open_interest_value_observed_at_ms=_ms(row.open_interest_value_observed_at),
        buy_total_notional_usd=row.buy_total_notional_usd,
        sell_total_notional_usd=row.sell_total_notional_usd,
        ticker_observed_this_minute=row.ticker_observed_this_minute,
        complete=row.complete,
    )


def _to_price_bars(flow_bars: tuple[FlowBar, ...]) -> tuple[PriceBar, ...]:
    """Price comes from the same captured bars as flow/OI -- see module
    docstring: no CCXT call in this report. A bar's own `complete` flag
    (ticker_complete AND trades_complete) gates flow/OI, not price -- price
    known-at correctness only needs the close itself to exist, independent of
    whether the trades feed also completed that minute."""
    return tuple(
        PriceBar(ts_ms=bar.bucket_start_ms, close=bar.close_price, duration_ms=60_000)
        for bar in flow_bars
        if bar.close_price is not None and bar.close_price > 0
    )


def _anchor_flow_notional(timeline: EventTimeline) -> float | None:
    """Total buy+sell notional at the FROZEN offset-0 point (the trigger
    instant itself) -- the full cumulative flow over `[-24h, trigger)`, used
    only as the matched-control balance diagnostic's own input, see
    momentum_flow_matched_controls.evaluate_control_balance. Amended after
    colleague review, before any real run: taking whichever point happened
    to resolve FIRST, independently for event and control, could compare
    two structurally different accumulation periods -- e.g. the event's own
    -720m partial accumulation against the control's own -60m partial
    accumulation. Freezing both sides to the same offset makes the
    comparison apples-to-apples; a side that has not resolved AT offset 0
    specifically is reported as unresolved rather than silently substituting
    a shorter window from a different offset."""
    point = next((p for p in timeline.points if p.offset_minutes == 0), None)
    if point is None or point.buy_notional_usd is None or point.sell_notional_usd is None:
        return None
    return point.buy_notional_usd + point.sell_notional_usd


def _liquidity_bucket(notional: float | None) -> str | None:
    if notional is None:
        return None
    if notional < LIQUIDITY_BUCKET_LOW_USD:
        return "low"
    if notional < LIQUIDITY_BUCKET_HIGH_USD:
        return "mid"
    return "high"


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(json_ready(payload), sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _empty_result(
    event: MeasurementEvent,
    *,
    status: str,
    watch: WatchLinkage | None,
    event_timeline: EventTimeline | None,
    control_attempts: int,
    repeat_token: bool,
) -> EpisodeResult:
    return EpisodeResult(
        pump_event_id=event.pump_event_id,
        base=event.base,
        exchange=event.exchange,
        trigger_at=event.trigger_at,
        status=status,
        control_at=None,
        control_offset_days=None,
        control_attempts=control_attempts,
        balance=None,
        watch=watch,
        event_timeline=event_timeline,
        control_timeline=None,
        liquidity_bucket=(
            _liquidity_bucket(_anchor_flow_notional(event_timeline))
            if event_timeline is not None
            else None
        ),
        repeat_token=repeat_token,
    )


def _process_event(
    event: MeasurementEvent,
    *,
    symbol_bars: tuple[FlowBar, ...],
    watch: WatchLinkage | None,
    other_pump_instants: tuple[datetime, ...],
    capture_epoch_started_at: datetime,
    until: datetime,
    control_max_search_days: int,
    repeat_token: bool,
) -> EpisodeResult:
    """Process one event against one symbol's already-fetched bars. Pure
    given its inputs -- the caller (`build_episode_study_report` or `_run`)
    owns fetching and releasing `symbol_bars`."""
    if not event_is_mature(event.trigger_at, until):
        return _empty_result(
            event,
            status="immature",
            watch=watch,
            event_timeline=None,
            control_attempts=0,
            repeat_token=repeat_token,
        )

    event_price_bars = _to_price_bars(symbol_bars)
    event_timeline = build_event_timeline(
        pump_event_id=event.pump_event_id,
        base=event.base,
        trigger_at=event.trigger_at,
        price_bars=event_price_bars,
        flow_bars=symbol_bars,
        lookback_offsets_minutes=LOOKBACK_OFFSETS_MINUTES,
    )
    if not event_timeline.any_flow_available:
        return _empty_result(
            event,
            status="event_flow_unavailable",
            watch=watch,
            event_timeline=event_timeline,
            control_attempts=0,
            repeat_token=repeat_token,
        )

    candidates = candidate_control_instants(
        trigger_at=event.trigger_at,
        other_trigger_instants_same_instrument=other_pump_instants,
        capture_epoch_started_at=capture_epoch_started_at,
        until=until,
        max_search_days=control_max_search_days,
    )

    # Amended after colleague review, before any real run: pick the first
    # candidate whose own flow window is available at all (nearest-in-time
    # selection, per the frozen rule), THEN check its balance once. Balance
    # must never be used to keep searching for a more convenient candidate --
    # that would make it a ranking input in practice despite being
    # documented as diagnostic-only (see momentum_flow_matched_controls.py's
    # own module docstring).
    control_timeline: EventTimeline | None = None
    attempts = 0
    for candidate in candidates:
        attempts += 1
        trial = build_event_timeline(
            pump_event_id=event.pump_event_id,
            base=event.base,
            trigger_at=candidate.candidate_at,
            price_bars=event_price_bars,
            flow_bars=symbol_bars,
            lookback_offsets_minutes=LOOKBACK_OFFSETS_MINUTES,
        )
        if trial.any_flow_available:
            control_timeline = trial
            break

    if control_timeline is None:
        return _empty_result(
            event,
            status="control_unresolved",
            watch=watch,
            event_timeline=event_timeline,
            control_attempts=attempts,
            repeat_token=repeat_token,
        )

    balance = evaluate_control_balance(
        event_flow_notional_usd=_anchor_flow_notional(event_timeline),
        control_flow_notional_usd=_anchor_flow_notional(control_timeline),
    )
    liquidity_bucket = _liquidity_bucket(_anchor_flow_notional(event_timeline))
    control_at = datetime.fromtimestamp(control_timeline.trigger_at_ms / 1000, tz=UTC)
    control_offset_days = round(
        (control_timeline.trigger_at_ms - event_timeline.trigger_at_ms) / (24 * 3600 * 1000)
    )

    if balance.balanced:
        status = "complete"
    elif balance.reason in ("missing_flow_reading", "non_positive_flow_notional"):
        # Amended after colleague review, before any real run: a candidate
        # whose own frozen offset-0 flow reading never resolved (or
        # resolved non-positive) cannot be balance-COMPARED at all --
        # reported the same way a candidate with no usable timeline at all
        # is ("control_unresolved"), not as "compared and found too
        # different" ("control_unbalanced").
        status = "control_unresolved"
    else:
        status = "control_unbalanced"

    return EpisodeResult(
        pump_event_id=event.pump_event_id,
        base=event.base,
        exchange=event.exchange,
        trigger_at=event.trigger_at,
        status=status,
        control_at=control_at,
        control_offset_days=control_offset_days,
        control_attempts=attempts,
        balance=balance,
        watch=watch,
        event_timeline=event_timeline,
        control_timeline=control_timeline,
        liquidity_bucket=liquidity_bucket,
        repeat_token=repeat_token,
    )


def _paired_stats(
    pairs: list[tuple[float, float]],
) -> tuple[int, float | None, float | None, float | None]:
    """N, mean(event), mean(control), mean(event - control) -- only over
    pairs where BOTH sides already resolved (amended after colleague
    review, before any real run: appending event/control values
    independently let the two means come from different, unequal-length,
    non-corresponding sets of episodes, which is not a paired comparison at
    all)."""
    if not pairs:
        return 0, None, None, None
    events = [event for event, _ in pairs]
    controls = [control for _, control in pairs]
    deltas = [event - control for event, control in pairs]
    return len(pairs), _mean(events), _mean(controls), _mean(deltas)


def _validate_scope(
    events: tuple[MeasurementEvent, ...],
    *,
    capture_epoch_started_at: datetime,
    dataset_since: datetime,
    until: datetime,
) -> None:
    """Shared by `_aggregate_report` (the pure, single-call test API) and
    `_run` (the real streaming entry point) so both fail the same way on
    the same caller-contract violations, rather than the real run trusting
    its own SQL scoping silently and only the test path checking."""
    if capture_epoch_started_at.utcoffset() is None or until.utcoffset() is None:
        raise ValueError("capture_epoch_started_at and until must be timezone-aware")
    if dataset_since < capture_epoch_started_at + REQUIRED_PRE_WINDOW:
        raise ValueError(
            "dataset_since must allow every event its full pre-trigger lookback window"
        )
    # The repository's own query already filters to `since=dataset_since`
    # (see _run) -- an event before that instant reaching this function
    # indicates a caller-contract violation, not a normal, reachable
    # production state. Fail loud rather than silently counting it as an
    # ordinary exclusion reason (amended after colleague review: the
    # earlier soft-exclusion path here was dead code in every real run and
    # misleadingly implied otherwise).
    if any(event.trigger_at < dataset_since for event in events):
        raise ValueError("received an event before dataset_since; caller scoping is broken")


def build_episode_study_report(
    events: tuple[MeasurementEvent, ...],
    bars_by_symbol: dict[str, tuple[FlowBar, ...]],
    watch_linkage: dict[int, WatchLinkage],
    *,
    capture_epoch_started_at: datetime,
    watch_cohort_started_at: datetime | None,
    dataset_since: datetime,
    until: datetime,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    event_input_fingerprint: str,
    bars_input_fingerprint: str,
    control_max_search_days: int,
    contamination_instants_by_base: dict[str, tuple[datetime, ...]] | None = None,
    upstream_exclusion_reasons: tuple[tuple[str, int], ...] = (),
) -> EpisodeStudyReport:
    """Single-call convenience wrapper (used directly by tests) around
    `_process_event` -- takes a fully pre-loaded `bars_by_symbol` dict, so
    it does not itself bound peak memory across many symbols. `_run` (the
    real I/O entry point) does not call this with a pre-loaded dict; it
    fetches and releases one symbol's bars at a time, calls
    `_process_event` directly per event, and hands the accumulated results
    to `_finish_report` (the aggregation shared with `_aggregate_report`
    below) -- see `_run`'s own docstring note."""
    return _aggregate_report(
        events,
        lambda symbol: bars_by_symbol.get(symbol, ()),
        watch_linkage,
        capture_epoch_started_at=capture_epoch_started_at,
        watch_cohort_started_at=watch_cohort_started_at,
        dataset_since=dataset_since,
        until=until,
        generated_at=generated_at,
        code_revision=code_revision,
        working_tree_dirty=working_tree_dirty,
        event_input_fingerprint=event_input_fingerprint,
        bars_input_fingerprint=bars_input_fingerprint,
        control_max_search_days=control_max_search_days,
        contamination_instants_by_base=contamination_instants_by_base or {},
        upstream_exclusion_reasons=upstream_exclusion_reasons,
    )


def _aggregate_report(
    events: tuple[MeasurementEvent, ...],
    bars_for_symbol: Callable[[str], tuple[FlowBar, ...]],
    watch_linkage: dict[int, WatchLinkage],
    *,
    capture_epoch_started_at: datetime,
    watch_cohort_started_at: datetime | None,
    dataset_since: datetime,
    until: datetime,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    event_input_fingerprint: str,
    bars_input_fingerprint: str,
    control_max_search_days: int,
    contamination_instants_by_base: dict[str, tuple[datetime, ...]],
    upstream_exclusion_reasons: tuple[tuple[str, int], ...],
) -> EpisodeStudyReport:
    _validate_scope(
        events,
        capture_epoch_started_at=capture_epoch_started_at,
        dataset_since=dataset_since,
        until=until,
    )

    bybit_native = [event for event in events if event.exchange == "bybit"]
    seen_bases: set[str] = set()
    episode_results: list[EpisodeResult] = []
    for event in sorted(bybit_native, key=lambda item: (item.trigger_at, item.pump_event_id)):
        repeat_token = event.base in seen_bases
        seen_bases.add(event.base)
        # `event.market_id` is Bybit's own EXACT traded market id for this
        # bybit_native event (the identity-ready earliest source for it WAS
        # Bybit -- see momentum_flow_event_repository._select_events's own
        # identity contract), not a naive `{base}USDT` reconstruction.
        # Amended after third colleague review, before any real run:
        # reconstructing via bybit_linear_symbol(event.base) discards
        # identity the event cohort already resolved and established as
        # exact -- harmless while every current instrument happens to match
        # the naive pattern, but silently wrong the moment one does not
        # (an unusual market id, or a relisting).
        symbol = event.market_id
        # Wider than "other events in THIS cohort": every real Bybit pump
        # source instant for this base, from an independent query, so a
        # control point cannot land next to a pump that this primary cohort
        # happened not to select (cross-venue-first, identity-excluded, or
        # just outside [dataset_since, until) -- see momentum_flow_event_
        # repository.bybit_source_instants_statement's own docstring).
        other_pump_instants = tuple(
            at
            for at in contamination_instants_by_base.get(event.base, ())
            if at != event.trigger_at
        )
        episode_results.append(
            _process_event(
                event,
                symbol_bars=bars_for_symbol(symbol),
                watch=watch_linkage.get(event.pump_event_id),
                other_pump_instants=other_pump_instants,
                capture_epoch_started_at=capture_epoch_started_at,
                until=until,
                control_max_search_days=control_max_search_days,
                repeat_token=repeat_token,
            )
        )

    return _finish_report(
        events,
        episode_results,
        capture_epoch_started_at=capture_epoch_started_at,
        watch_cohort_started_at=watch_cohort_started_at,
        dataset_since=dataset_since,
        until=until,
        generated_at=generated_at,
        code_revision=code_revision,
        working_tree_dirty=working_tree_dirty,
        event_input_fingerprint=event_input_fingerprint,
        bars_input_fingerprint=bars_input_fingerprint,
        upstream_exclusion_reasons=upstream_exclusion_reasons,
    )


def render_json(report: EpisodeStudyReport) -> str:
    return json.dumps(json_ready(asdict(report)), sort_keys=True, indent=2, default=str) + "\n"


def _fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    return f"{value:.{digits}f}{suffix}" if value is not None else "n/a"


def render_markdown(report: EpisodeStudyReport) -> str:
    manifest = report.manifest
    recall = report.watch_recall
    lines = [
        "# Momentum Flow Episode Study",
        "",
        f"Generated: {manifest.generated_at.isoformat()}",
        f"Code revision: `{manifest.code_revision}`",
        f"Working tree dirty: {'yes' if manifest.working_tree_dirty else 'no'}",
        f"Family: `{manifest.family}` (HYP-014, `discovery-ledger.md`, status `parked`)",
        (
            f"Scope: {manifest.dataset_since.isoformat()} <= trigger < "
            f"{manifest.dataset_until_exclusive.isoformat()}"
        ),
        f"Capture epoch started at: {manifest.capture_epoch_started_at.isoformat()}",
        (
            f"WATCH cohort started at: {manifest.watch_cohort_started_at.isoformat()}"
            if manifest.watch_cohort_started_at
            else "WATCH cohort started at: n/a (no momentum_flow_watch_v1 run registered)"
        ),
        "",
        "> Measurement prerequisites for HYP-014 only. Does not confirm the family and does",
        "> not move it out of `parked`. No p-value, Holm correction, profit factor, or",
        "> promotion verdict is computed by this report.",
        "",
        "## Coverage funnel",
        "",
        *markdown_table(
            ("Stage", "Count"),
            [
                # Amended after third colleague review, before any real
                # run: `dataset_events` is `len(events)`, i.e. the
                # already IDENTITY-READY cohort -- it already excludes
                # `no_identity_ready_earliest_source` and every other
                # upstream exclusion reason, which are listed as their own
                # rows immediately below. The old "(all sources)" label
                # implied this row's own count already included them,
                # which made the funnel look like it did not add up.
                ("Identity-ready cohort events", report.dataset_events),
                *[(row.name, row.count) for row in report.exclusion_reasons],
                (
                    "Complete episodes (Bybit-native, matured, balanced control)",
                    report.complete_episodes,
                ),
            ],
        ),
        "",
        "## WATCH recall",
        "",
        *markdown_table(
            ("Metric", "Value"),
            [
                (
                    "Denominator events (watch cohort started, mature, observable)",
                    recall.denominator_events,
                ),
                ("Eligible WATCH before trigger", recall.watch_before_trigger),
                ("Recall", _fmt(recall.recall_pct, 1, "%")),
                ("Median lead (minutes)", _fmt(recall.median_lead_minutes, 1)),
                ("WATCH arrived only after trigger", recall.watch_only_after_trigger),
                (
                    "Unresolved (in-scope but WATCH coverage insufficient)",
                    recall.unresolved_events,
                ),
            ],
        ),
        "",
        "## Per-lookback event vs. matched control (paired, descriptive)",
        "",
        *markdown_table(
            (
                "Offset (min)",
                "Price N",
                "Event price %",
                "Control price %",
                "Price delta",
                "OI N",
                "Event OI %",
                "Control OI %",
                "OI delta",
                "Flow N",
                "Event flow USD",
                "Control flow USD",
                "Flow delta",
            ),
            [
                (
                    row.offset_minutes,
                    row.price_pair_n,
                    _fmt(row.mean_event_price_change_pct),
                    _fmt(row.mean_control_price_change_pct),
                    _fmt(row.mean_price_paired_delta_pct),
                    row.oi_pair_n,
                    _fmt(row.mean_event_oi_change_pct),
                    _fmt(row.mean_control_oi_change_pct),
                    _fmt(row.mean_oi_paired_delta_pct),
                    row.flow_pair_n,
                    _fmt(row.mean_event_net_flow_usd, 0),
                    _fmt(row.mean_control_net_flow_usd, 0),
                    _fmt(row.mean_flow_paired_delta_usd, 0),
                )
                for row in report.lookback_comparison
            ],
        ),
        "",
        "## Segments",
        "",
        *markdown_table(
            ("Dimension", "Bucket", "Episodes", "WATCH recall"),
            [
                (row.dimension, row.bucket, row.episodes, _fmt(row.watch_recall_pct, 1, "%"))
                for row in report.segments
            ],
        ),
        "",
    ]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--capture-epoch-started-at",
        type=parse_utc_datetime,
        required=True,
        help=(
            "The momentum-capture cohort's ACCEPTED started_at_ms for this research line. "
            "On the very first run, take it from market:momentumcapture:health:bybit or "
            "runtime/momentum-canary-checkpoints.json and it becomes frozen from then on "
            "(see momentum_flow_cohort_acceptance.py) -- on every later run, re-supply the "
            "SAME already-accepted value. A differing value is refused unless "
            "--accept-new-cohort-boundary is also passed: this is not 'the current epoch', "
            "since a momentum-capture restart moving it would otherwise silently re-baseline "
            "the whole cohort and discard comparability with already-accumulated data."
        ),
    )
    parser.add_argument(
        "--accept-new-cohort-boundary",
        action="store_true",
        help=(
            "Deliberately accept --capture-epoch-started-at as a NEW frozen cohort boundary "
            "even though a different one is already recorded. A genuine, human-decided "
            "re-baseline -- never pass this as a routine default."
        ),
    )
    parser.add_argument(
        "--cohort-state-path",
        default=None,
        help=(
            "Override the accepted-cohort state file path (otherwise "
            f"{COHORT_STATE_ENV_VAR} or the relative default). Mainly for tests/CI isolation."
        ),
    )
    parser.add_argument("--until", type=parse_utc_datetime, help="exclusive UTC cutoff")
    parser.add_argument(
        "--control-max-search-days",
        type=int,
        default=28,
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
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for momentum-flow-episode-study-report")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    generated_at = datetime.now(UTC)
    until = args.until or generated_at

    # Freeze the capture-cohort boundary across runs rather than trusting
    # whatever `--capture-epoch-started-at` this particular invocation
    # happened to be given (amended after third colleague review, before
    # any real run -- see momentum_flow_cohort_acceptance.py's own module
    # docstring). A conflicting value raises CohortBoundaryConflictError
    # (a ValueError) unless --accept-new-cohort-boundary was explicitly
    # passed.
    cohort_state_path = resolve_cohort_state_path(args.cohort_state_path, env=os.environ)
    accepted_cohort = load_accepted_cohort(cohort_state_path)
    capture_epoch_started_at, acceptance, changed = resolve_capture_cohort_started_at(
        requested=args.capture_epoch_started_at,
        accepted=accepted_cohort,
        accept_new_cohort=args.accept_new_cohort_boundary,
        now=generated_at,
    )
    if changed:
        save_accepted_cohort(cohort_state_path, acceptance)

    dataset_since = capture_epoch_started_at + REQUIRED_PRE_WINDOW
    if dataset_since >= until:
        raise ValueError("capture epoch has not accumulated its required pre-trigger window yet")

    event_repo = MomentumFlowEventRepository.from_url(db_url)
    watch_repo = MomentumFlowWatchLinkageRepository.from_url(db_url)
    bars_repo = MomentumFlowBarsRepository.from_url(db_url)
    try:
        cohort = await event_repo.load(since=dataset_since, until=until)
        # Fail fast, before spending a per-symbol bars fetch loop against a
        # live database, on the same caller-contract violations the pure
        # test API checks (see _validate_scope's own docstring).
        _validate_scope(
            cohort.events,
            capture_epoch_started_at=capture_epoch_started_at,
            dataset_since=dataset_since,
            until=until,
        )
        watch_cohort_started_at = await watch_repo.watch_cohort_started_at()
        # Wider than the primary cohort on purpose -- see momentum_flow_
        # event_repository.bybit_source_instants_statement's own docstring
        # and this report's own module docstring on contamination exclusion.
        contamination_instants_by_base = await event_repo.load_bybit_source_instants(
            since=capture_epoch_started_at, until=until
        )

        bybit_native = [event for event in cohort.events if event.exchange == "bybit"]
        # `event.market_id` is Bybit's own EXACT traded market id for a
        # bybit_native event, not a naive `{base}USDT` reconstruction --
        # see the matching note in `_aggregate_report`'s own loop.
        events_by_symbol: dict[str, list[MeasurementEvent]] = defaultdict(list)
        for event in bybit_native:
            events_by_symbol[event.market_id].append(event)

        windows = tuple(
            InstrumentWindow(
                pump_event_id=event.pump_event_id,
                # The event cohort's own market_type is always "swap" (the
                # identity gate in momentum_flow_event_repository.py requires
                # it); the WATCH tables use the capture contract's own
                # "linear" market_type -- a different vocabulary for the same
                # instrument class. Using event.market_type here silently
                # matched nothing in production (colleague review).
                exchange=BYBIT_MOMENTUM_EXCHANGE,
                market_type=BYBIT_MOMENTUM_MARKET_TYPE,
                symbol=event.market_id,
                trigger_at=event.trigger_at,
            )
            for event in bybit_native
        )
        watch_linkage = await watch_repo.load_linkage(windows)

        # Bounded per symbol to what candidate search can actually reach
        # (+-control_max_search_days around each of that symbol's own
        # triggers, clipped to the report scope), not the whole capture
        # epoch. One symbol's rows are fetched, folded into this run's
        # fingerprint and episode results, and then released before the
        # next symbol -- holding every symbol's bars resident at once would
        # grow unbounded week over week on a host with a tight memory
        # budget (colleague review, before any real run; see ROADMAP.md's
        # OOM-prevention precedent for market-path fetches).
        search_span = timedelta(days=args.control_max_search_days) + timedelta(hours=24)
        bars_digest = hashlib.sha256()
        episode_results = []
        for symbol in sorted(events_by_symbol):
            symbol_events = events_by_symbol[symbol]
            window_since = max(
                capture_epoch_started_at,
                min(event.trigger_at for event in symbol_events) - search_span,
            )
            window_until = min(
                until, max(event.trigger_at for event in symbol_events) + search_span
            )
            symbol_rows = await bars_repo.load(
                symbols=(symbol,), since=window_since, until=window_until
            )
            for row in sorted(symbol_rows, key=lambda item: item.bucket_start):
                bars_digest.update(
                    json.dumps(
                        json_ready(asdict(row)), sort_keys=True, separators=(",", ":"), default=str
                    ).encode()
                )
            symbol_bars = tuple(
                _to_flow_bar(row) for row in sorted(symbol_rows, key=lambda item: item.bucket_start)
            )
            seen_bases_for_symbol: set[str] = set()
            symbol_events_sorted = sorted(
                symbol_events, key=lambda item: (item.trigger_at, item.pump_event_id)
            )
            for event in symbol_events_sorted:
                repeat_token = event.base in seen_bases_for_symbol
                seen_bases_for_symbol.add(event.base)
                other_pump_instants = tuple(
                    at
                    for at in contamination_instants_by_base.get(event.base, ())
                    if at != event.trigger_at
                )
                episode_results.append(
                    (
                        event,
                        _process_event(
                            event,
                            symbol_bars=symbol_bars,
                            watch=watch_linkage.get(event.pump_event_id),
                            other_pump_instants=other_pump_instants,
                            capture_epoch_started_at=capture_epoch_started_at,
                            until=until,
                            control_max_search_days=args.control_max_search_days,
                            repeat_token=repeat_token,
                        ),
                    )
                )
            # symbol_rows/symbol_bars go out of scope on the next iteration;
            # nothing above retains a reference beyond this symbol's own
            # episode_results entries (small dataclasses, not raw bars).
            del symbol_rows, symbol_bars

        event_fingerprint = _fingerprint([asdict(event) for event in cohort.events])
        report = _finish_report(
            cohort.events,
            [result for _, result in episode_results],
            capture_epoch_started_at=capture_epoch_started_at,
            watch_cohort_started_at=watch_cohort_started_at,
            dataset_since=dataset_since,
            until=until,
            generated_at=generated_at,
            code_revision=args.code_revision,
            working_tree_dirty=args.working_tree_dirty,
            event_input_fingerprint=event_fingerprint,
            bars_input_fingerprint=bars_digest.hexdigest(),
            upstream_exclusion_reasons=cohort.exclusion_reasons,
        )
    finally:
        await event_repo.close()
        await watch_repo.close()
        await bars_repo.close()
    return render_json(report) if args.format == "json" else render_markdown(report)


def _finish_report(
    events: tuple[MeasurementEvent, ...],
    episode_results: list[EpisodeResult],
    *,
    capture_epoch_started_at: datetime,
    watch_cohort_started_at: datetime | None,
    dataset_since: datetime,
    until: datetime,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    event_input_fingerprint: str,
    bars_input_fingerprint: str,
    upstream_exclusion_reasons: tuple[tuple[str, int], ...],
) -> EpisodeStudyReport:
    """Aggregate pre-computed per-event results from `_run`'s own streaming
    symbol loop into the final report shape. Kept separate from
    `_aggregate_report` (which also RUNS `_process_event` itself, for the
    single-call convenience the pure test API needs) purely to avoid
    recomputing every episode a second time here."""
    exclusions: Counter[str] = Counter(dict(upstream_exclusion_reasons))
    cross_venue = sum(1 for event in events if event.exchange != "bybit")
    exclusions["cross_venue_secondary"] += cross_venue
    for result in episode_results:
        exclusions[result.status] += 0 if result.status == "complete" else 1

    complete = [row for row in episode_results if row.status == "complete"]
    watch_denominator_since = (
        max(dataset_since, watch_cohort_started_at) if watch_cohort_started_at else None
    )
    # Time-based eligibility only (mature, at/after the WATCH cohort start).
    # This is NOT the full recall denominator by itself -- an eligible event
    # still needs `watch.watch_observable` (see momentum_flow_watch_linkage_
    # repository's own module docstring) before an absent `watch` decision
    # can be counted as a genuine miss rather than a coverage gap.
    eligible_by_time_ids = {
        row.pump_event_id
        for row in episode_results
        if event_is_mature(row.trigger_at, until)
        and watch_denominator_since is not None
        and row.trigger_at >= watch_denominator_since
    }
    watch_rows = [
        row.watch
        for row in episode_results
        if row.pump_event_id in eligible_by_time_ids
        and row.watch is not None
        and row.watch.watch_observable
    ]
    unresolved_events = sum(
        1
        for row in episode_results
        if row.pump_event_id in eligible_by_time_ids
        and (row.watch is None or not row.watch.watch_observable)
    )
    before = [w for w in watch_rows if w.earliest_watch_before_trigger_at is not None]
    only_after = [w for w in watch_rows if w.watch_arrived_only_after_trigger]
    watch_recall = WatchRecallSummary(
        denominator_events=len(watch_rows),
        watch_before_trigger=len(before),
        recall_pct=(len(before) / len(watch_rows) * 100) if watch_rows else None,
        median_lead_minutes=_median([w.lead_minutes for w in before if w.lead_minutes is not None]),
        watch_only_after_trigger=len(only_after),
        watch_only_after_trigger_pct=(
            (len(only_after) / len(watch_rows) * 100) if watch_rows else None
        ),
        unresolved_events=unresolved_events,
    )

    lookback_rows: list[LookbackComparisonRow] = []
    for offset in LOOKBACK_OFFSETS_MINUTES:
        price_pairs: list[tuple[float, float]] = []
        oi_pairs: list[tuple[float, float]] = []
        flow_pairs: list[tuple[float, float]] = []
        for row in complete:
            assert row.event_timeline is not None and row.control_timeline is not None
            event_point = next(p for p in row.event_timeline.points if p.offset_minutes == offset)
            control_point = next(
                p for p in row.control_timeline.points if p.offset_minutes == offset
            )
            if (
                event_point.price_change_pct is not None
                and control_point.price_change_pct is not None
            ):
                price_pairs.append((event_point.price_change_pct, control_point.price_change_pct))
            if event_point.oi_change_pct is not None and control_point.oi_change_pct is not None:
                oi_pairs.append((event_point.oi_change_pct, control_point.oi_change_pct))
            if (
                event_point.net_flow_notional_usd is not None
                and control_point.net_flow_notional_usd is not None
            ):
                flow_pairs.append(
                    (event_point.net_flow_notional_usd, control_point.net_flow_notional_usd)
                )
        price_n, price_event_mean, price_control_mean, price_delta = _paired_stats(price_pairs)
        oi_n, oi_event_mean, oi_control_mean, oi_delta = _paired_stats(oi_pairs)
        flow_n, flow_event_mean, flow_control_mean, flow_delta = _paired_stats(flow_pairs)
        lookback_rows.append(
            LookbackComparisonRow(
                offset_minutes=offset,
                price_pair_n=price_n,
                mean_event_price_change_pct=price_event_mean,
                mean_control_price_change_pct=price_control_mean,
                mean_price_paired_delta_pct=price_delta,
                oi_pair_n=oi_n,
                mean_event_oi_change_pct=oi_event_mean,
                mean_control_oi_change_pct=oi_control_mean,
                mean_oi_paired_delta_pct=oi_delta,
                flow_pair_n=flow_n,
                mean_event_net_flow_usd=flow_event_mean,
                mean_control_net_flow_usd=flow_control_mean,
                mean_flow_paired_delta_usd=flow_delta,
            )
        )

    def _liquidity_key(row: EpisodeResult) -> str:
        return row.liquidity_bucket or "unresolved"

    def _repeat_token_key(row: EpisodeResult) -> str:
        return "repeat" if row.repeat_token else "first_seen"

    segment_dimensions: tuple[tuple[str, Callable[[EpisodeResult], str]], ...] = (
        ("liquidity", _liquidity_key),
        ("repeat_token", _repeat_token_key),
    )
    segments: list[SegmentRow] = []
    for dimension, keyer in segment_dimensions:
        buckets: dict[str, list[EpisodeResult]] = defaultdict(list)
        for row in complete:
            buckets[keyer(row)].append(row)
        for bucket, rows in sorted(buckets.items()):
            bucket_watch = [
                row.watch
                for row in rows
                if row.pump_event_id in eligible_by_time_ids
                and row.watch is not None
                and row.watch.watch_observable
            ]
            bucket_before = [
                w for w in bucket_watch if w.earliest_watch_before_trigger_at is not None
            ]
            segments.append(
                SegmentRow(
                    dimension=dimension,
                    bucket=bucket,
                    episodes=len(rows),
                    watch_recall_pct=(
                        (len(bucket_before) / len(bucket_watch) * 100) if bucket_watch else None
                    ),
                )
            )

    return EpisodeStudyReport(
        manifest=EpisodeStudyManifest(
            report_version=REPORT_VERSION,
            protocol_version=MOMENTUM_FLOW_PROTOCOL_VERSION,
            family=MOMENTUM_FLOW_FAMILY,
            event_cohort_version=MOMENTUM_FLOW_EVENT_COHORT_VERSION,
            timeline_version=MOMENTUM_FLOW_TIMELINE_VERSION,
            control_selector_version=CONTROL_SELECTOR_VERSION,
            watch_linkage_version=WATCH_LINKAGE_VERSION,
            watch_version=WATCH_VERSION,
            watch_contract_sha256=WATCH_CONTRACT_SHA256,
            code_revision=normalize_code_revision(code_revision),
            working_tree_dirty=working_tree_dirty,
            generated_at=generated_at,
            capture_epoch_started_at=capture_epoch_started_at,
            watch_cohort_started_at=watch_cohort_started_at,
            dataset_since=dataset_since,
            dataset_until_exclusive=until,
            watch_denominator_since=watch_denominator_since,
            event_input_fingerprint=event_input_fingerprint,
            bars_input_fingerprint=bars_input_fingerprint,
            lookback_offsets_minutes=LOOKBACK_OFFSETS_MINUTES,
        ),
        dataset_events=len(events),
        exclusion_reasons=tuple(
            CountRow(name, count)
            for name, count in sorted(exclusions.items(), key=lambda item: (-item[1], item[0]))
            if count > 0
        ),
        complete_episodes=len(complete),
        watch_recall=watch_recall,
        lookback_comparison=tuple(lookback_rows),
        segments=tuple(segments),
        episode_results=tuple(episode_results),
    )


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))
