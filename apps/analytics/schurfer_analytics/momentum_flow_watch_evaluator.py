"""Pure evaluator and state machine for ``momentum_flow_watch_v1``."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import ceil, isfinite
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from .momentum_flow_watch_contract import FROZEN_WATCH_CONTRACT, WatchContract

QualityReason = Literal[
    "identity_unresolved",
    "insufficient_history",
    "missing_current_bar",
    "non_consecutive_bars",
    "incomplete_bar",
    "feed_gap",
    "missing_price",
    "stale_quote",
    "missing_fresh_oi",
    "input_not_available_at_decision",
    "invalid_numeric_input",
    "insufficient_flow_baseline",
]

SignalReason = Literal[
    "cross_section_too_small",
    "oi_growth_below_threshold",
    "buy_imbalance_below_threshold",
    "flow_acceleration_below_threshold",
    "flow_notional_below_floor",
    "price_outside_containment",
]

DecisionStatus = Literal[
    "watch",
    "rejected_quality",
    "rejected_signal",
    "suppressed_active_episode",
    "suppressed_cooldown",
]


@dataclass(frozen=True)
class WatchBar:
    symbol: str
    universe_version: str
    bucket_start: datetime
    created_at: datetime
    close_price: float | None
    buy_total_notional_usd: float
    sell_total_notional_usd: float
    open_interest: float | None
    open_interest_event_at: datetime | None
    open_interest_observed_at: datetime | None
    last_trade_event_at: datetime | None
    last_trade_received_at: datetime | None
    last_ticker_event_at: datetime | None
    last_ticker_received_at: datetime | None
    unbackfilled_gap_minutes: int
    complete: bool

    # feat/momentum-trade-price-source-v1's own canonical price-provenance
    # fields (apps/collector/internal/momentum.Bar's own PriceSource/
    # Price* doc comments): populated identically in spirit for both
    # venues -- Bybit's own AddTickerObservation mirrors these from its
    # existing Ticker* fields on every call that carries a LastPrice
    # (mechanically identical to last_ticker_event_at/last_ticker_received_at
    # whenever that happens, which is every call in normal Bybit operation
    # -- see momentum.go's own AddTickerObservation), Binance's own AddTrade
    # populates them from accepted aggTrade prices (Binance has no ticker/
    # price feed at all -- see cmd/momentumcapturebinance's own package doc
    # comment). stale_quote (below) reads last_price_received_at instead of
    # last_ticker_received_at specifically so it means the same thing on
    # both venues; the reason code name itself is not changed (frozen v1
    # contract).
    price_source: str | None
    first_price_event_at: datetime | None
    last_price_event_at: datetime | None
    first_price_received_at: datetime | None
    last_price_received_at: datetime | None
    price_observed_this_minute: bool
    open_interest_complete: bool
    price_complete: bool


@dataclass(frozen=True)
class WatchFeatures:
    price_return_60m_pct: float
    price_return_15m_pct: float
    oi_growth_60m_pct: float
    buy_notional_15m_usd: float
    sell_notional_15m_usd: float
    flow_notional_15m_usd: float
    buy_imbalance_15m: float
    flow_acceleration_15m_vs_prior_45m: float


@dataclass(frozen=True)
class PreparedEvaluation:
    symbol: str
    bucket_start: datetime
    universe_version: str
    source_event_at: datetime | None
    source_received_at: datetime | None
    bucket_ready_at: datetime | None
    quality_reasons: tuple[QualityReason, ...]
    features: WatchFeatures | None

    @property
    def quality_ready(self) -> bool:
        return not self.quality_reasons and self.features is not None


@dataclass(frozen=True)
class CrossSectionThresholds:
    sample_size: int
    oi_growth_60m_pct: float | None
    buy_imbalance_15m: float | None
    flow_acceleration_15m_vs_prior_45m: float | None


@dataclass(frozen=True)
class SymbolWatchState:
    active_episode: bool = False
    clear_streak: int = 0
    last_watch_at: datetime | None = None
    episode_id: UUID | None = None


@dataclass(frozen=True)
class WatchEvaluation:
    symbol: str
    bucket_start: datetime
    universe_version: str
    source_event_at: datetime | None
    source_received_at: datetime | None
    bucket_ready_at: datetime | None
    quality_ready: bool
    raw_qualified: bool
    decision_status: DecisionStatus
    reason_codes: tuple[str, ...]
    features: WatchFeatures | None
    thresholds: CrossSectionThresholds
    episode_id: UUID | None
    watch_id: UUID | None


def percentile(values: list[float], fraction: float) -> float:
    """Deterministic nearest-rank percentile used by the frozen contract."""
    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0.0 < fraction <= 1.0:
        raise ValueError("fraction must be in (0, 1]")
    ordered = sorted(values)
    return ordered[max(0, ceil(fraction * len(ordered)) - 1)]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _latest(*values: datetime | None) -> datetime | None:
    present = [_utc(value) for value in values if value is not None]
    return max(present) if present else None


def _latest_source_pair(bar: WatchBar) -> tuple[datetime | None, datetime | None]:
    pairs = [
        (bar.last_trade_event_at, bar.last_trade_received_at),
        (bar.last_ticker_event_at, bar.last_ticker_received_at),
    ]
    complete = [
        (_utc(event_at), _utc(received_at))
        for event_at, received_at in pairs
        if event_at is not None and received_at is not None
    ]
    if not complete:
        return None, None
    return max(complete, key=lambda pair: pair[0])


def _identity_ready(symbol: str, universe_version: str) -> bool:
    return bool(universe_version) and symbol.endswith("USDT") and symbol[:-4].isalnum()


def _fresh_oi(bar: WatchBar, *, known_at: datetime) -> bool:
    if (
        bar.open_interest is None
        or bar.open_interest <= 0
        or bar.open_interest_event_at is None
        or bar.open_interest_observed_at is None
    ):
        return False
    event_at = _utc(bar.open_interest_event_at)
    observed_at = _utc(bar.open_interest_observed_at)
    bucket_start = _utc(bar.bucket_start)
    return (
        bucket_start <= event_at < bucket_start + timedelta(minutes=1) and observed_at <= known_at
    )


def prepare_symbol_evaluation(
    *,
    symbol: str,
    bucket_start: datetime,
    bars: tuple[WatchBar, ...],
    evaluator_started_at: datetime,
    expected_universe_version: str | None = None,
    contract: WatchContract = FROZEN_WATCH_CONTRACT,
) -> PreparedEvaluation:
    target = _utc(bucket_start)
    started = _utc(evaluator_started_at)
    expected_count = contract.lookback_minutes + 1
    ordered = tuple(sorted(bars, key=lambda bar: bar.bucket_start))
    universe_version = ordered[-1].universe_version if ordered else ""
    reasons: list[QualityReason] = []

    if (
        not _identity_ready(symbol, universe_version)
        or (expected_universe_version is not None and universe_version != expected_universe_version)
        or any(bar.symbol != symbol for bar in ordered)
        or any(bar.universe_version != universe_version for bar in ordered)
    ):
        reasons.append("identity_unresolved")
    if not ordered or _utc(ordered[-1].bucket_start) != target:
        reasons.append("missing_current_bar")
    if len(ordered) != expected_count:
        reasons.append("insufficient_history")
    if len(ordered) == expected_count:
        expected = target - timedelta(minutes=contract.lookback_minutes)
        if any(
            _utc(bar.bucket_start) != expected + timedelta(minutes=index)
            for index, bar in enumerate(ordered)
        ):
            reasons.append("non_consecutive_bars")
    if any(not bar.complete for bar in ordered):
        reasons.append("incomplete_bar")
    if any(bar.unbackfilled_gap_minutes > 0 for bar in ordered):
        reasons.append("feed_gap")
    if any(bar.close_price is None or bar.close_price <= 0 for bar in ordered):
        reasons.append("missing_price")
    if any(_utc(bar.created_at) > started for bar in ordered):
        reasons.append("input_not_available_at_decision")
    if any(
        (bar.close_price is not None and not isfinite(bar.close_price))
        or (bar.open_interest is not None and not isfinite(bar.open_interest))
        or not isfinite(bar.buy_total_notional_usd)
        or not isfinite(bar.sell_total_notional_usd)
        or bar.buy_total_notional_usd < 0
        or bar.sell_total_notional_usd < 0
        for bar in ordered
    ):
        reasons.append("invalid_numeric_input")

    current = ordered[-1] if ordered else None
    if current is not None:
        # last_price_received_at, not last_ticker_received_at: the latter
        # is Binance's own OI-poll timestamp (Binance's only
        # AddTickerObservation caller is its OI poller, never a real
        # price feed), which would make this check silently mean
        # "is our OI poller alive" instead of "is our price fresh" for
        # that venue. last_price_received_at is the canonical,
        # source-agnostic field (see WatchBar's own doc comment above);
        # for Bybit it matches last_ticker_received_at whenever a ticker
        # observation carries a LastPrice (every call in normal operation),
        # so this is a no-op there in practice.
        received = _latest(current.last_price_received_at)
        ready_at = _utc(current.created_at)
        max_delay = timedelta(seconds=contract.max_bucket_decision_delay_seconds)
        if (
            received is None
            or received > started
            or ready_at > started
            or started - received > max_delay
            or started - ready_at > max_delay
        ):
            reasons.append("stale_quote")
    if len(ordered) == expected_count and (
        not _fresh_oi(ordered[0], known_at=started) or not _fresh_oi(ordered[-1], known_at=started)
    ):
        reasons.append("missing_fresh_oi")

    source_event_at, source_received_at = (
        _latest_source_pair(current) if current is not None else (None, None)
    )
    bucket_ready_at = _utc(current.created_at) if current is not None else None
    unique_reasons = tuple(dict.fromkeys(reasons))
    if unique_reasons:
        return PreparedEvaluation(
            symbol=symbol,
            bucket_start=target,
            universe_version=universe_version,
            source_event_at=source_event_at,
            source_received_at=source_received_at,
            bucket_ready_at=bucket_ready_at,
            quality_reasons=unique_reasons,
            features=None,
        )

    assert current is not None
    anchor = ordered[0]
    fifteen_minute_anchor = ordered[-(contract.flow_window_minutes + 1)]
    assert current.close_price is not None
    assert anchor.close_price is not None
    assert fifteen_minute_anchor.close_price is not None
    assert current.open_interest is not None
    assert anchor.open_interest is not None
    current_flow = ordered[-contract.flow_window_minutes :]
    baseline_flow = ordered[1 : 1 + contract.flow_baseline_minutes]
    buy = sum(bar.buy_total_notional_usd for bar in current_flow)
    sell = sum(bar.sell_total_notional_usd for bar in current_flow)
    gross = buy + sell
    baseline_gross = sum(
        bar.buy_total_notional_usd + bar.sell_total_notional_usd for bar in baseline_flow
    )
    if baseline_gross <= 0:
        return PreparedEvaluation(
            symbol=symbol,
            bucket_start=target,
            universe_version=universe_version,
            source_event_at=source_event_at,
            source_received_at=source_received_at,
            bucket_ready_at=bucket_ready_at,
            quality_reasons=("insufficient_flow_baseline",),
            features=None,
        )
    baseline_equivalent = baseline_gross * (
        contract.flow_window_minutes / contract.flow_baseline_minutes
    )
    features = WatchFeatures(
        price_return_60m_pct=(current.close_price / anchor.close_price - 1.0) * 100.0,
        price_return_15m_pct=(current.close_price / fifteen_minute_anchor.close_price - 1.0)
        * 100.0,
        oi_growth_60m_pct=(current.open_interest / anchor.open_interest - 1.0) * 100.0,
        buy_notional_15m_usd=buy,
        sell_notional_15m_usd=sell,
        flow_notional_15m_usd=gross,
        buy_imbalance_15m=(buy - sell) / gross if gross > 0 else 0.0,
        flow_acceleration_15m_vs_prior_45m=(
            gross / baseline_equivalent if baseline_equivalent > 0 else 0.0
        ),
    )
    if any(
        not isfinite(value)
        for value in (
            features.price_return_60m_pct,
            features.price_return_15m_pct,
            features.oi_growth_60m_pct,
            features.buy_notional_15m_usd,
            features.sell_notional_15m_usd,
            features.flow_notional_15m_usd,
            features.buy_imbalance_15m,
            features.flow_acceleration_15m_vs_prior_45m,
        )
    ):
        return PreparedEvaluation(
            symbol=symbol,
            bucket_start=target,
            universe_version=universe_version,
            source_event_at=source_event_at,
            source_received_at=source_received_at,
            bucket_ready_at=bucket_ready_at,
            quality_reasons=("invalid_numeric_input",),
            features=None,
        )
    return PreparedEvaluation(
        symbol=symbol,
        bucket_start=target,
        universe_version=universe_version,
        source_event_at=source_event_at,
        source_received_at=source_received_at,
        bucket_ready_at=bucket_ready_at,
        quality_reasons=(),
        features=features,
    )


def build_cross_section_thresholds(
    prepared: tuple[PreparedEvaluation, ...],
    *,
    contract: WatchContract = FROZEN_WATCH_CONTRACT,
) -> CrossSectionThresholds:
    features = [row.features for row in prepared if row.quality_ready and row.features]
    if len(features) < contract.min_cross_section_size:
        return CrossSectionThresholds(len(features), None, None, None)
    return CrossSectionThresholds(
        sample_size=len(features),
        oi_growth_60m_pct=percentile(
            [feature.oi_growth_60m_pct for feature in features],
            contract.oi_growth_percentile,
        ),
        buy_imbalance_15m=percentile(
            [feature.buy_imbalance_15m for feature in features],
            contract.buy_imbalance_percentile,
        ),
        flow_acceleration_15m_vs_prior_45m=percentile(
            [feature.flow_acceleration_15m_vs_prior_45m for feature in features],
            contract.flow_acceleration_percentile,
        ),
    )


def _signal_reasons(
    features: WatchFeatures,
    thresholds: CrossSectionThresholds,
    contract: WatchContract,
) -> tuple[SignalReason, ...]:
    if (
        thresholds.oi_growth_60m_pct is None
        or thresholds.buy_imbalance_15m is None
        or thresholds.flow_acceleration_15m_vs_prior_45m is None
    ):
        return ("cross_section_too_small",)
    reasons: list[SignalReason] = []
    if features.oi_growth_60m_pct < max(contract.min_oi_growth_pct, thresholds.oi_growth_60m_pct):
        reasons.append("oi_growth_below_threshold")
    if features.buy_imbalance_15m < max(contract.min_buy_imbalance, thresholds.buy_imbalance_15m):
        reasons.append("buy_imbalance_below_threshold")
    if features.flow_acceleration_15m_vs_prior_45m < max(
        contract.min_flow_acceleration,
        thresholds.flow_acceleration_15m_vs_prior_45m,
    ):
        reasons.append("flow_acceleration_below_threshold")
    if features.flow_notional_15m_usd < contract.min_flow_notional_usd_15m:
        reasons.append("flow_notional_below_floor")
    if not (
        contract.min_price_return_60m_pct
        <= features.price_return_60m_pct
        <= contract.max_price_return_60m_pct
        and features.price_return_15m_pct <= contract.max_price_return_15m_pct
    ):
        reasons.append("price_outside_containment")
    return tuple(reasons)


def evaluate_prepared(
    prepared: PreparedEvaluation,
    *,
    thresholds: CrossSectionThresholds,
    state: SymbolWatchState,
    decision_at: datetime,
    contract: WatchContract = FROZEN_WATCH_CONTRACT,
) -> tuple[WatchEvaluation, SymbolWatchState]:
    decision_time = _utc(decision_at)
    event_time = _utc(prepared.bucket_start)
    if decision_time < event_time:
        raise ValueError("decision_at cannot precede bucket_start")
    if not prepared.quality_ready or prepared.features is None:
        evaluation = WatchEvaluation(
            symbol=prepared.symbol,
            bucket_start=prepared.bucket_start,
            universe_version=prepared.universe_version,
            source_event_at=prepared.source_event_at,
            source_received_at=prepared.source_received_at,
            bucket_ready_at=prepared.bucket_ready_at,
            quality_ready=False,
            raw_qualified=False,
            decision_status="rejected_quality",
            reason_codes=prepared.quality_reasons,
            features=None,
            thresholds=thresholds,
            episode_id=state.episode_id,
            watch_id=None,
        )
        return evaluation, state

    signal_reasons = _signal_reasons(prepared.features, thresholds, contract)
    if signal_reasons:
        if signal_reasons == ("cross_section_too_small",):
            next_state = state
        elif not state.active_episode:
            next_state = replace(state, clear_streak=0, episode_id=None)
        else:
            clear_streak = state.clear_streak + 1
            active = True
            episode_id = state.episode_id
            if clear_streak >= contract.rearm_clear_minutes:
                active = False
                episode_id = None
                clear_streak = 0
            next_state = replace(
                state,
                active_episode=active,
                clear_streak=clear_streak,
                episode_id=episode_id,
            )
        return (
            WatchEvaluation(
                symbol=prepared.symbol,
                bucket_start=prepared.bucket_start,
                universe_version=prepared.universe_version,
                source_event_at=prepared.source_event_at,
                source_received_at=prepared.source_received_at,
                bucket_ready_at=prepared.bucket_ready_at,
                quality_ready=True,
                raw_qualified=False,
                decision_status="rejected_signal",
                reason_codes=signal_reasons,
                features=prepared.features,
                thresholds=thresholds,
                episode_id=state.episode_id,
                watch_id=None,
            ),
            next_state,
        )

    episode_id = state.episode_id or uuid5(
        NAMESPACE_URL,
        f"{contract.watch_version}:episode:{prepared.symbol}:{prepared.bucket_start.isoformat()}",
    )
    if state.active_episode:
        status: DecisionStatus = "suppressed_active_episode"
        watch_id = None
        next_state = replace(state, clear_streak=0, episode_id=episode_id)
    elif state.last_watch_at is not None and event_time - _utc(state.last_watch_at) < timedelta(
        minutes=contract.watch_cooldown_minutes
    ):
        status = "suppressed_cooldown"
        watch_id = None
        next_state = SymbolWatchState(
            active_episode=True,
            clear_streak=0,
            last_watch_at=state.last_watch_at,
            episode_id=episode_id,
        )
    else:
        status = "watch"
        watch_id = uuid5(
            NAMESPACE_URL,
            f"{contract.watch_version}:watch:{prepared.symbol}:{prepared.bucket_start.isoformat()}",
        )
        next_state = SymbolWatchState(
            active_episode=True,
            clear_streak=0,
            last_watch_at=event_time,
            episode_id=episode_id,
        )
    return (
        WatchEvaluation(
            symbol=prepared.symbol,
            bucket_start=prepared.bucket_start,
            universe_version=prepared.universe_version,
            source_event_at=prepared.source_event_at,
            source_received_at=prepared.source_received_at,
            bucket_ready_at=prepared.bucket_ready_at,
            quality_ready=True,
            raw_qualified=True,
            decision_status=status,
            reason_codes=() if status == "watch" else (status,),
            features=prepared.features,
            thresholds=thresholds,
            episode_id=episode_id,
            watch_id=watch_id,
        ),
        next_state,
    )
