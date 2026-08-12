"""Pure, DB-free timeline engine for `analysis/bybit-early-momentum-event-
study-v0`. Builds one pump event's -24h..+4h feature timeline from
already-loaded price bars and (optionally) already-loaded momentum-flow
bars -- see `momentum_flow_protocol.py` for the frozen feature
definitions, lookback offsets, point-in-time known-at rule, and
completeness rule this module implements. No I/O here; every function
takes data the caller already fetched, so this is unit-testable against
hand-built synthetic bars without a live database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import log
from statistics import pstdev

from .momentum_flow_protocol import (
    FLOW_AVAILABLE,
    FLOW_FULL_COVERAGE_FRACTION,
    FLOW_GAP_EXCLUDED,
    FLOW_PARTIAL_COVERAGE,
    FLOW_UNAVAILABLE_PRE_CAPTURE,
    MOMENTUM_FLOW_OI_MAX_STALENESS_MINUTES,
    FlowAvailability,
    flow_bars_available_at,
)

MOMENTUM_FLOW_TIMELINE_VERSION = "momentum_flow_timeline_v2"
_OI_MAX_STALENESS_MS = MOMENTUM_FLOW_OI_MAX_STALENESS_MINUTES * 60_000

# Fixed by the momentum-capture contract (0024_bybit_momentum_bars_1m.py):
# every row is exactly one UTC minute. Not a per-bar field -- unlike
# PriceBar's duration, which genuinely varies by caller/timeframe, this is
# a property of the table itself.
_FLOW_BAR_DURATION_MS = 60_000


@dataclass(frozen=True)
class PriceBar:
    """One already-loaded historical price observation (old-data source).
    `ts_ms` is the bar's own OPEN time, matching `ohlcv.Candle`/CCXT
    convention -- its `close` only becomes known once the bar's own period
    has fully elapsed (`known_at_ms`), same point-in-time hazard
    `token_behavior_descriptors.DailyBar` documents for daily bars.
    `duration_ms` is a required field (no default): a caller must
    consciously state the bar's own timeframe rather than this module
    guessing it from spacing between bars, which would misclassify a
    genuine gap as a different timeframe."""

    ts_ms: int
    close: float
    duration_ms: int

    @property
    def known_at_ms(self) -> int:
        return self.ts_ms + self.duration_ms


@dataclass(frozen=True)
class FlowBar:
    """One minute of `timeseries.bybit_momentum_bars_1m`, only the columns
    this module's v0 feature set reads. `complete` mirrors the table's own
    `complete` column (`ticker_complete AND trades_complete`); a bar with
    `complete=False` must never be zero-filled into a cumulative sum -- see
    `momentum_flow_protocol.py`'s fail-closed exclusion rule.

    The OI event/observed timestamp pairs are the real freshness signals (see
    momentum_flow_protocol.py's "Open-interest staleness policy"): the Go
    collector only touches it when a ticker message actually carried a
    new OI value, unlike `open_interest` itself (carried forward every
    minute regardless) or `ticker_observed_this_minute` (set on ANY
    ticker message that minute, with or without an OI update -- a
    colleague review, 2026-08-12, found this insufficient on its own)."""

    bucket_start_ms: int
    close_price: float | None
    open_interest: float | None
    open_interest_value: float | None
    open_interest_event_at_ms: int | None
    open_interest_observed_at_ms: int | None
    open_interest_value_event_at_ms: int | None
    open_interest_value_observed_at_ms: int | None
    buy_total_notional_usd: float
    sell_total_notional_usd: float
    ticker_observed_this_minute: bool
    complete: bool

    @property
    def known_at_ms(self) -> int:
        return self.bucket_start_ms + _FLOW_BAR_DURATION_MS


@dataclass(frozen=True)
class TimelinePoint:
    offset_minutes: int
    at_ms: int
    price_available: bool
    price_change_pct: float | None
    realized_volatility: float | None
    # flow_availability/flow_coverage_pct describe ONLY the buy/sell
    # cumulative sums below -- OI is a point-in-time level, not a sum, and
    # is resolved independently (see oi_change_pct's own docstring note in
    # build_event_timeline): a point can have a resolved oi_change_pct
    # while flow_availability is FLOW_GAP_EXCLUDED, or vice versa.
    flow_availability: FlowAvailability
    flow_coverage_pct: float | None
    oi_change_pct: float | None
    oi_value_change_pct: float | None
    buy_notional_usd: float | None
    sell_notional_usd: float | None
    net_flow_notional_usd: float | None


@dataclass(frozen=True)
class EventTimeline:
    pump_event_id: int
    base: str
    trigger_at_ms: int
    reference_price: float | None
    reference_oi: float | None
    reference_oi_value: float | None
    points: tuple[TimelinePoint, ...]

    @property
    def any_flow_available(self) -> bool:
        return any(point.flow_availability == FLOW_AVAILABLE for point in self.points)


def _closest_known_price_at_or_before(bars: list[PriceBar], target_ms: int) -> PriceBar | None:
    """The most recent price bar whose own data is already KNOWN as of
    target_ms (`known_at_ms <= target_ms`, not `ts_ms <= target_ms` --
    see momentum_flow_protocol.py's point-in-time known-at rule). Using a
    bar's start time instead would leak up to one full bar-period of
    future price into a lookback point that falls inside a still-forming
    bar."""
    candidate: PriceBar | None = None
    for bar in bars:
        if bar.known_at_ms <= target_ms and (
            candidate is None or bar.known_at_ms > candidate.known_at_ms
        ):
            candidate = bar
    return candidate


def _closest_known_oi_at_or_before(
    bars: list[FlowBar], target_ms: int, *, tzinfo: object, value_metric: bool = False
) -> FlowBar | None:
    """OI is a point-in-time LEVEL, not a cumulative flow -- the most
    recent known-at-time-safe bar carrying a GENUINE `open_interest`
    observation at or before target_ms, independent of the buy/sell
    cumulative coverage-fraction gate below (which does not apply to a
    level, only to a sum). Still gated by `flow_bars_available_at` on the
    bar's OWN bucket time: real production data never has a row before
    `MOMENTUM_FLOW_BARS_AVAILABLE_FROM`, but this is a defensive,
    independent check rather than trusting an arbitrary input bar's
    timestamp alone.

    Staleness policy (frozen; see momentum_flow_protocol.py's "Open-
    interest staleness policy", twice amended before any real run): two
    separate conditions, both required.
    1. The metric's own event timestamp must fall inside that bar's own
       one-minute bucket -- proof that this amount/value was updated for
       this bar rather than carried forward. The observation timestamp may
       cross the bucket boundary due to network lag, but must be at or
       before the query point. Amount and USD value use their own timestamp
       pairs independently.
    2. The observation must not be older than `MOMENTUM_FLOW_OI_MAX_
       STALENESS_MINUTES` relative to `target_ms`: the closest genuinely
       fresh reading can still be too old to represent "now" for a
       specific lookback point far away from it.
    A stretch with no sufficiently fresh observation correctly resolves
    to no OI value here (fail-closed), rather than silently reporting a
    0% change that only reflects the absence of new data, not real
    price/OI behavior."""
    candidate: FlowBar | None = None
    for bar in bars:
        metric = bar.open_interest_value if value_metric else bar.open_interest
        event_at_ms = (
            bar.open_interest_value_event_at_ms if value_metric else bar.open_interest_event_at_ms
        )
        observed_at_ms = (
            bar.open_interest_value_observed_at_ms
            if value_metric
            else bar.open_interest_observed_at_ms
        )
        bucket_at = datetime.fromtimestamp(bar.bucket_start_ms / 1000, tz=tzinfo)  # type: ignore[arg-type]
        if (
            metric is not None
            and event_at_ms is not None
            and observed_at_ms is not None
            and bar.bucket_start_ms <= event_at_ms < bar.bucket_start_ms + _FLOW_BAR_DURATION_MS
            and observed_at_ms <= target_ms
            and target_ms - observed_at_ms <= _OI_MAX_STALENESS_MS
            and bar.complete
            and bar.known_at_ms <= target_ms
            and flow_bars_available_at(bucket_at)
            and (candidate is None or bar.known_at_ms > candidate.known_at_ms)
        ):
            candidate = bar
    return candidate


def _log_returns(closes: list[float]) -> list[float]:
    return [log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]


def _expected_minute_buckets(start_ms: int, end_ms: int) -> int:
    """Count of UTC-minute-aligned bucket_start instants in [start_ms,
    end_ms] -- the denominator for flow coverage. Independent of
    start_ms/end_ms's own alignment (they inherit trigger_at's arbitrary
    sub-minute offset): rounds to the actual minute grid real flow bars
    live on, not to whatever offset the trigger itself happens to fall
    on."""
    if end_ms < start_ms:
        return 0
    first_bucket = -(-start_ms // _FLOW_BAR_DURATION_MS) * _FLOW_BAR_DURATION_MS  # ceil
    last_bucket = (end_ms // _FLOW_BAR_DURATION_MS) * _FLOW_BAR_DURATION_MS  # floor
    if last_bucket < first_bucket:
        return 0
    return (last_bucket - first_bucket) // _FLOW_BAR_DURATION_MS + 1


@dataclass(frozen=True)
class _FlowWindow:
    availability: FlowAvailability
    coverage_pct: float | None
    bars: tuple[FlowBar, ...]


def _flow_window(
    sorted_flow_bars: list[FlowBar],
    *,
    timeline_start_ms: int,
    target_ms: int,
    target_at: datetime,
) -> _FlowWindow:
    """The complete, known-at-time-safe flow bars covering [timeline_
    start_ms, target_ms], plus the coverage-based availability
    classification for the CUMULATIVE buy/sell sums specifically (see
    momentum_flow_protocol.py's FLOW_AVAILABLE / FLOW_PARTIAL_COVERAGE /
    FLOW_GAP_EXCLUDED / FLOW_UNAVAILABLE_PRE_CAPTURE). Structurally, the
    very first requested lookback offset's own window is always zero-
    width (timeline_start_ms == target_ms there) and therefore always
    GAP_EXCLUDED for the cumulative sums -- nothing has accumulated yet
    at the instant the timeline just started, which is the correct
    "running total starts at zero observations" reading, not a bug. OI's
    own reference anchor is resolved separately (see
    `_closest_known_oi_at_or_before`) precisely because a snapshot level
    does not share this structural limitation."""
    if not flow_bars_available_at(target_at):
        return _FlowWindow(FLOW_UNAVAILABLE_PRE_CAPTURE, None, ())
    bars = tuple(
        bar
        for bar in sorted_flow_bars
        if timeline_start_ms <= bar.bucket_start_ms <= target_ms
        and bar.complete
        and bar.known_at_ms <= target_ms
    )
    if not bars:
        return _FlowWindow(FLOW_GAP_EXCLUDED, 0.0, ())
    # The achievable range excludes the last _FLOW_BAR_DURATION_MS: a
    # bucket starting any later than target_ms - 60_000 cannot possibly be
    # known yet at target_ms (its own known_at_ms would exceed target_ms),
    # so counting it as "expected" would make 100% coverage mathematically
    # unreachable even with perfect real data.
    expected = _expected_minute_buckets(timeline_start_ms, target_ms - _FLOW_BAR_DURATION_MS)
    coverage = len(bars) / expected if expected > 0 else 0.0
    availability = (
        FLOW_AVAILABLE if coverage >= FLOW_FULL_COVERAGE_FRACTION else FLOW_PARTIAL_COVERAGE
    )
    return _FlowWindow(availability, coverage, bars)


def build_event_timeline(
    *,
    pump_event_id: int,
    base: str,
    trigger_at: datetime,
    price_bars: tuple[PriceBar, ...],
    flow_bars: tuple[FlowBar, ...] = (),
    lookback_offsets_minutes: tuple[int, ...],
) -> EventTimeline:
    """Build one event's timeline. `price_bars`/`flow_bars` may cover a
    wider range than the requested lookbacks; this function only reads
    what each lookback point needs. `lookback_offsets_minutes` is a
    required keyword (no default) so a caller must consciously pass the
    frozen `momentum_flow_protocol.LOOKBACK_OFFSETS_MINUTES` rather than
    silently drift from it.

    Reference anchors (price and OI) are each FIXED to the earliest
    requested offset (`lookback_offsets_minutes[0]`) -- never "whichever
    offset happens to have data first" -- so the same offset measures the
    same kind of quantity across every event; see momentum_flow_protocol.
    py's amended feature-set section. If an anchor's own data is
    unavailable, every point's corresponding *_change_pct is None for the
    WHOLE timeline, not silently re-anchored. Price and OI use "closest
    known bar at or before the anchor" (both are point-in-time levels);
    buy/sell notional is a genuine cumulative sum instead, gated by
    coverage fraction rather than a single anchor bar -- see
    `_flow_window`."""
    if not lookback_offsets_minutes:
        raise ValueError("lookback_offsets_minutes must be non-empty")
    offsets = tuple(sorted(set(lookback_offsets_minutes)))
    trigger_ms = int(trigger_at.timestamp() * 1000)
    sorted_price_bars = sorted(price_bars, key=lambda bar: bar.ts_ms)
    sorted_flow_bars = sorted(flow_bars, key=lambda bar: bar.bucket_start_ms)

    timeline_start_ms = trigger_ms + offsets[0] * 60_000
    anchor_ms = timeline_start_ms  # offsets[0]'s own target, by construction

    # --- fixed reference anchors ---
    price_anchor_bar = _closest_known_price_at_or_before(sorted_price_bars, anchor_ms)
    reference_price = price_anchor_bar.close if price_anchor_bar is not None else None

    oi_anchor_bar = _closest_known_oi_at_or_before(
        sorted_flow_bars, anchor_ms, tzinfo=trigger_at.tzinfo
    )
    oi_value_anchor_bar = _closest_known_oi_at_or_before(
        sorted_flow_bars,
        anchor_ms,
        tzinfo=trigger_at.tzinfo,
        value_metric=True,
    )
    reference_oi = oi_anchor_bar.open_interest if oi_anchor_bar is not None else None
    reference_oi_value = (
        oi_value_anchor_bar.open_interest_value if oi_value_anchor_bar is not None else None
    )

    # --- per-point features against the fixed anchors above ---
    points: list[TimelinePoint] = []
    for offset in offsets:
        target_ms = trigger_ms + offset * 60_000
        price_bar = _closest_known_price_at_or_before(sorted_price_bars, target_ms)
        price_available = price_bar is not None
        price_change_pct = (
            (price_bar.close / reference_price - 1) * 100.0
            if price_bar is not None and reference_price
            else None
        )

        window_closes = [
            bar.close
            for bar in sorted_price_bars
            if timeline_start_ms <= bar.known_at_ms <= target_ms
        ]
        realized_volatility = (
            pstdev(_log_returns(window_closes)) if len(window_closes) >= 3 else None
        )

        target_at = datetime.fromtimestamp(target_ms / 1000, tz=trigger_at.tzinfo)

        oi_change_pct = oi_value_change_pct = None
        if reference_oi is not None:
            latest_oi_bar = _closest_known_oi_at_or_before(
                sorted_flow_bars, target_ms, tzinfo=trigger_at.tzinfo
            )
            if latest_oi_bar is not None and latest_oi_bar.open_interest is not None:
                oi_change_pct = (latest_oi_bar.open_interest / reference_oi - 1) * 100.0
        if reference_oi_value is not None:
            latest_oi_value_bar = _closest_known_oi_at_or_before(
                sorted_flow_bars,
                target_ms,
                tzinfo=trigger_at.tzinfo,
                value_metric=True,
            )
            if (
                latest_oi_value_bar is not None
                and latest_oi_value_bar.open_interest_value is not None
            ):
                oi_value_change_pct = (
                    latest_oi_value_bar.open_interest_value / reference_oi_value - 1
                ) * 100.0

        window = _flow_window(
            sorted_flow_bars,
            timeline_start_ms=timeline_start_ms,
            target_ms=target_ms,
            target_at=target_at,
        )
        buy_notional_usd = sell_notional_usd = net_flow_notional_usd = None
        if window.availability in (FLOW_AVAILABLE, FLOW_PARTIAL_COVERAGE):
            buy_notional_usd = sum(bar.buy_total_notional_usd for bar in window.bars)
            sell_notional_usd = sum(bar.sell_total_notional_usd for bar in window.bars)
            net_flow_notional_usd = buy_notional_usd - sell_notional_usd

        points.append(
            TimelinePoint(
                offset_minutes=offset,
                at_ms=target_ms,
                price_available=price_available,
                price_change_pct=price_change_pct,
                realized_volatility=realized_volatility,
                flow_availability=window.availability,
                flow_coverage_pct=window.coverage_pct,
                oi_change_pct=oi_change_pct,
                oi_value_change_pct=oi_value_change_pct,
                buy_notional_usd=buy_notional_usd,
                sell_notional_usd=sell_notional_usd,
                net_flow_notional_usd=net_flow_notional_usd,
            )
        )

    return EventTimeline(
        pump_event_id=pump_event_id,
        base=base,
        trigger_at_ms=trigger_ms,
        reference_price=reference_price,
        reference_oi=reference_oi,
        reference_oi_value=reference_oi_value,
        points=tuple(points),
    )
