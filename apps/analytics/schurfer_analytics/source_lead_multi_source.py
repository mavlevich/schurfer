"""HYP-012b: which source venues lead Bybit (registered discovery family, two stages).

Registered before any return was read: docs/research/source-lead-multi-source-hyp012b-v1.md.

Candidate set (outcome-blind): a pump event whose unique earliest source observation is a
family venue (ties excluded), whose source observation passes the HYP-012 identity checks,
and whose base had exactly one Bybit USDT linear perpetual live over the whole episode
(delisted contracts included, from their launchTime/deliveryTime). The route must then
pass a pre-entry price identity check: the source's first price is within a factor of
PRICE_IDENTITY_BAND of the Bybit open of the minute holding the signal.
Unlike the HYP-012 paired design, Bybit is NOT required to confirm the pump later: that
would condition on the future, and the standalone estimand trades at the signal.

Estimand: standalone long net return on Bybit, entry at the open of the first minute after
the source first-seen time, exit at the close of the bar that opens at entry + 30 minutes
(the v2 exit-bar convention). Only the entry and exit bars are required. Costs: taker fee
per side, the HYP-012 frozen 20 bps round-trip impact, and funding prorated over the hold.

Stage 1 (discovery, ISO weeks 33-35): per formal venue, cluster-bootstrap mean and null
p-value; Holm across the formal family; a venue survives if its mean is positive and it is
Holm-rejected. Stage 2 (holdout, ISO weeks 36-39): only survivors, each needing the floor
(100 resolved episodes, 30 assets, no week above 45%); Holm across the survivors; a
candidate needs Holm rejection and a positive mean. Exploratory venues are reported with
no verdict. A mature holdout venue (at least 100 resolved) with a non-positive mean is a
`fail` even below the diversification floor. Nothing on or after 2026-09-29 (the HYP-012
v2 cohort) is ever read, and a stage is refused before its window end plus
MATURATION_LAG.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from statistics import fmean
from typing import TYPE_CHECKING, Any

from .clustered_inference import (
    ClusterObservation,
    cluster_bootstrap_mean,
    cluster_bootstrap_mean_null_p_value,
    derived_seed,
    holm_step_down,
)
from .ohlcv import ONE_MINUTE_MS, next_timeframe_after
from .source_lead import SourceLeadEvent, _identity_reason

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .ohlcv import Candle

FAMILY_VERSION = "hyp012b_multi_source_lead_v1"
TARGET_EXCHANGE = "bybit"
FORMAL_SOURCES: tuple[str, ...] = ("blofin", "mexc", "bingx", "gate", "lbank")
EXPLORATORY_SOURCES: tuple[str, ...] = (
    "coinex",
    "bitget",
    "binance",
    "okx",
    "htx",
    "xt",
    "toobit",
    "kucoin",
)
DISCOVERY_START = datetime(2026, 8, 10, tzinfo=UTC)  # Monday, ISO week 33
HOLDOUT_START = datetime(2026, 8, 31, tzinfo=UTC)  # Monday, ISO week 36
HOLDOUT_END = datetime(2026, 9, 28, tzinfo=UTC)  # exclusive; v2 cohort starts 09-29
HORIZON_MINUTES = 30
TAKER_FEE_BPS_PER_SIDE = 10.0
ROUND_TRIP_IMPACT_BPS = 20.0
FUNDING_BPS_PER_8H = 5.0
HOLDOUT_FLOOR = {"min_resolved": 100, "min_assets": 30, "max_week_share": 0.45}
FAMILY_ALPHA = 0.05
BOOTSTRAP_ITERATIONS = 10_000
BOOTSTRAP_SEED = 20_260_926
STAGES = ("discovery", "holdout")
MATURATION_LAG = timedelta(days=1)
PRICE_IDENTITY_BAND = 2.0


def assert_stage_mature(stage: str, now: datetime) -> None:
    """Refuse a stage whose window (plus the late-data lag) has not fully passed."""
    _, end = stage_window(stage)
    if now < end + MATURATION_LAG:
        raise ValueError(
            f"{stage} window ends {end.isoformat()}; it can run from "
            f"{(end + MATURATION_LAG).isoformat()} (now {now.isoformat()})"
        )


@dataclass(frozen=True)
class BybitInstrument:
    """One Bybit USDT linear perpetual, trading or delisted. `delivery_ms` is 0 while
    the contract still trades."""

    native_id: str
    base: str
    launch_ms: int
    delivery_ms: int

    def live_over(self, start_ms: int, end_ms: int) -> bool:
        return self.launch_ms <= start_ms and (self.delivery_ms == 0 or self.delivery_ms > end_ms)


def stage_window(stage: str) -> tuple[datetime, datetime]:
    if stage == "discovery":
        return DISCOVERY_START, HOLDOUT_START
    if stage == "holdout":
        return HOLDOUT_START, HOLDOUT_END
    raise ValueError(f"unknown stage {stage!r}")


@dataclass(frozen=True)
class Candidate:
    event_id: int
    base: str
    cluster_key: str
    source_exchange: str
    source_at: datetime
    bybit_native_id: str
    source_price: float | None

    @property
    def reference_ms(self) -> int:
        """The Bybit minute holding the signal; its open predates the signal."""
        return self.entry_ms - ONE_MINUTE_MS

    @property
    def entry_ms(self) -> int:
        return next_timeframe_after(int(self.source_at.timestamp() * 1000), ONE_MINUTE_MS)

    @property
    def exit_bar_ms(self) -> int:
        return self.entry_ms + HORIZON_MINUTES * ONE_MINUTE_MS

    @property
    def week(self) -> str:
        iso = self.source_at.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"


def build_candidates(
    events: Sequence[SourceLeadEvent],
    *,
    bybit_instruments: Sequence[BybitInstrument],
    stage: str,
    sources: Sequence[str],
) -> tuple[tuple[Candidate, ...], dict[str, int]]:
    """Outcome-blind candidate set for one stage, before the price identity check.
    Returns the candidates and a status count for the funnel."""
    start, end = stage_window(stage)
    wanted = {s.lower() for s in sources}
    by_base: dict[str, list[BybitInstrument]] = {}
    for instrument in bybit_instruments:
        by_base.setdefault(instrument.base.upper(), []).append(instrument)
    statuses: Counter[str] = Counter()
    out: list[Candidate] = []
    for event in sorted(events, key=lambda e: (e.first_seen_at, e.event_id)):
        if not event.observations:
            statuses["missing_source_attribution"] += 1
            continue
        ordered = sorted(event.observations, key=lambda o: (o.first_seen_at, o.exchange))
        earliest = ordered[0].first_seen_at
        firsts = [o for o in ordered if o.first_seen_at == earliest]
        if len(firsts) != 1:
            statuses["tied_first_source"] += 1
            continue
        source = firsts[0]
        exchange = source.exchange.strip().lower()
        if exchange == TARGET_EXCHANGE:
            statuses["target_first"] += 1
            continue
        if exchange not in wanted:
            statuses["other_first_source"] += 1
            continue
        if not (start <= source.first_seen_at < end):
            statuses["outside_stage_window"] += 1
            continue
        reason = _identity_reason(source, event.base)
        if reason:
            statuses[f"invalid_source:{reason}"] += 1
            continue
        routes = by_base.get(event.base.strip().upper())
        if not routes:
            statuses["no_bybit_perp"] += 1
            continue
        candidate = Candidate(
            event_id=event.event_id,
            base=event.base,
            cluster_key=event.cluster_key,
            source_exchange=exchange,
            source_at=source.first_seen_at,
            bybit_native_id="",
            source_price=source.first_price,
        )
        # The exit bar must close before the stage ends (no crossing into the next window).
        if candidate.exit_bar_ms + ONE_MINUTE_MS > end.timestamp() * 1000:
            statuses["exit_crosses_stage_end"] += 1
            continue
        signal_ms = int(source.first_seen_at.timestamp() * 1000)
        live = [r for r in routes if r.live_over(signal_ms, candidate.exit_bar_ms + ONE_MINUTE_MS)]
        if not live:
            statuses["bybit_not_live_over_episode"] += 1
            continue
        if len(live) > 1:
            statuses["ambiguous_bybit_route"] += 1
            continue
        candidate = replace(candidate, bybit_native_id=live[0].native_id)
        statuses[f"candidate:{exchange}"] += 1
        out.append(candidate)
    return tuple(out), dict(statuses)


def route_identity_reason(candidate: Candidate, reference: Candle | None) -> str | None:
    """Pre-entry identity check of the ticker-matched route: a same-ticker different
    project shows up as a price level far from the source's. Uses only data from before
    the entry, so a live router can apply the same rule."""
    if candidate.source_price is None or not math.isfinite(candidate.source_price):
        return "no_source_price"
    if candidate.source_price <= 0:
        return "no_source_price"
    if reference is None or reference.ts_ms != candidate.reference_ms:
        return "missing_reference_bar"
    if not math.isfinite(reference.open) or reference.open <= 0:
        return "missing_reference_bar"
    ratio = candidate.source_price / reference.open
    if not (1 / PRICE_IDENTITY_BAND <= ratio <= PRICE_IDENTITY_BAND):
        return "price_level_mismatch"
    return None


@dataclass(frozen=True)
class Outcome:
    candidate: Candidate
    resolved: bool
    reason: str | None
    net_return_pct: float | None


def round_trip_cost_pct(holding_minutes: float) -> float:
    bps = (
        2 * TAKER_FEE_BPS_PER_SIDE
        + ROUND_TRIP_IMPACT_BPS
        + FUNDING_BPS_PER_8H * holding_minutes / 480
    )
    return bps / 100


def evaluate(candidate: Candidate, entry: Candle | None, exit_bar: Candle | None) -> Outcome:
    if entry is None or entry.ts_ms != candidate.entry_ms:
        return Outcome(candidate, False, "missing_entry_bar", None)
    if exit_bar is None or exit_bar.ts_ms != candidate.exit_bar_ms:
        return Outcome(candidate, False, "missing_exit_bar", None)
    for price in (entry.open, exit_bar.close):
        if not math.isfinite(price) or price <= 0:
            return Outcome(candidate, False, "invalid_market_data", None)
    gross = (exit_bar.close - entry.open) / entry.open * 100
    holding = HORIZON_MINUTES + 1  # entry open to exit-bar close
    return Outcome(candidate, True, None, gross - round_trip_cost_pct(holding))


@dataclass(frozen=True)
class VenueResult:
    source: str
    candidates: int
    resolved: int
    assets: int
    max_week_share: float | None
    mean_net_pct: float | None
    ci_lower_pct: float | None
    ci_upper_pct: float | None
    p_value: float | None
    unresolved: dict[str, int]


def venue_result(source: str, outcomes: Sequence[Outcome]) -> VenueResult:
    mine = [o for o in outcomes if o.candidate.source_exchange == source]
    resolved = [o for o in mine if o.resolved and o.net_return_pct is not None]
    unresolved = Counter(o.reason for o in mine if not o.resolved)
    weeks = Counter(o.candidate.week for o in resolved)
    base: dict[str, Any] = {
        "source": source,
        "candidates": len(mine),
        "resolved": len(resolved),
        "assets": len({o.candidate.cluster_key for o in resolved}),
        "max_week_share": max(weeks.values()) / len(resolved) if resolved else None,
        "unresolved": {str(k): v for k, v in sorted(unresolved.items())},
    }
    if len(resolved) < 2 or len({o.candidate.cluster_key for o in resolved}) < 2:
        return VenueResult(
            **base, mean_net_pct=None, ci_lower_pct=None, ci_upper_pct=None, p_value=None
        )
    observations = tuple(
        ClusterObservation(o.candidate.cluster_key, float(o.net_return_pct or 0.0))
        for o in resolved
    )
    seed = derived_seed(BOOTSTRAP_SEED, f"{FAMILY_VERSION}:{source}")
    estimate = cluster_bootstrap_mean(
        observations, iterations=BOOTSTRAP_ITERATIONS, seed=seed
    ).estimate
    p_value = cluster_bootstrap_mean_null_p_value(
        observations, iterations=BOOTSTRAP_ITERATIONS, seed=seed
    )
    return VenueResult(
        **base,
        mean_net_pct=fmean(o.value for o in observations),
        ci_lower_pct=estimate.lower_bound,
        ci_upper_pct=estimate.upper_bound,
        p_value=p_value,
    )


def meets_holdout_floor(result: VenueResult) -> bool:
    return (
        result.resolved >= HOLDOUT_FLOOR["min_resolved"]
        and result.assets >= HOLDOUT_FLOOR["min_assets"]
        and result.max_week_share is not None
        and result.max_week_share <= HOLDOUT_FLOOR["max_week_share"]
    )


def floor_shortfalls(result: VenueResult) -> list[str]:
    """Which holdout floor criteria a venue misses (empty when it meets the floor)."""
    missing = []
    if result.resolved < HOLDOUT_FLOOR["min_resolved"]:
        missing.append("resolved")
    if result.assets < HOLDOUT_FLOOR["min_assets"]:
        missing.append("assets")
    if result.max_week_share is None or result.max_week_share > HOLDOUT_FLOOR["max_week_share"]:
        missing.append("week_share")
    return missing


def family_verdicts(results: Sequence[VenueResult], *, stage: str) -> dict[str, str]:
    """Stage verdict per venue in the tested family (formal venues at discovery,
    survivors at holdout). `survives`/`candidate` needs a positive mean and Holm
    rejection. At holdout a venue below the floor is `insufficient_data`, except that a
    mature one (at least the resolved minimum) with a non-positive mean is `fail`: a
    missing diversification criterion cannot rescue a negative result (the artifact
    records the shortfall)."""
    verdicts: dict[str, str] = {}
    testable: dict[str, float] = {}
    for result in results:
        if result.p_value is None:
            verdicts[result.source] = "insufficient_data"
        elif stage == "holdout" and not meets_holdout_floor(result):
            mature = result.resolved >= HOLDOUT_FLOOR["min_resolved"]
            non_positive = (result.mean_net_pct or 0.0) <= 0
            verdicts[result.source] = "fail" if mature and non_positive else "insufficient_data"
        else:
            testable[result.source] = result.p_value
    if testable:
        by_source = {r.source: r for r in results}
        for decision in holm_step_down(testable, family_alpha=FAMILY_ALPHA):
            positive = (by_source[decision.key].mean_net_pct or 0.0) > 0
            passed = decision.rejected and positive
            if stage == "discovery":
                verdicts[decision.key] = "survives" if passed else "does_not_survive"
            else:
                verdicts[decision.key] = "candidate" if passed else "fail"
    return verdicts
