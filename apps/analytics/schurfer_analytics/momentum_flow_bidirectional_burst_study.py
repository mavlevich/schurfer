"""Bidirectional buy/sell volume-burst discovery study (analysis/momentum-
flow-bidirectional-burst-study-v1).

Rebuild of the 2026-08-17 volume-burst screen (docs/analysis/
momentum_flow_volume_burst_screen.sql) after a colleague review found real
methodological holes in that first pass:

- ROWS BETWEEN N PRECEDING / LEAD(N) do not equal N actual minutes once
  incomplete bars are filtered out (VERIFIED same night: 0.37% of rows in
  the real dataset immediately follow a gap, one as large as 1h57m).
  Fixed here by computing burst percentages with Postgres RANGE-based
  window frames (`RANGE BETWEEN INTERVAL '5 minutes' PRECEDING`), which
  are keyed on the actual timestamp, not row position -- and by looking up
  forward/precursor prices via exact-timestamp equality, never LEAD/LAG.
- "13 independent episodes" only declustered by symbol, not by time --
  fixed here with a real refractory-window episode segmentation
  (`decluster_episodes`): a new episode only starts after
  `refractory_minutes` with no extreme-burst minute for that symbol.
- Selection bias (only symbols that had pumped) and no fixed dataset
  window -- fixed here by scanning the FULL captured universe over an
  explicit, pinned [since, until) range, not a `now() - 7 days` moving
  target.
- Buy-only -- fixed here by tracking buy and sell bursts as two distinct,
  separately-declustered populations (buy burst -> candidate long entry,
  sell burst -> candidate short entry), per the user's own observation
  that a sell burst is informative on its own, not just an absence of a
  buy signal.
- No matched control, no after-cost economics, no real cluster-bootstrap
  inference -- fixed here: baseline is each symbol's OWN mean forward
  return across every bar in the study window (same asset, not a
  cherry-picked comparison), after-cost economics reuse
  schurfer_performance.accounting.calculate_performance (the same engine
  every other paper/replay path in this repo uses), and statistical
  inference reuses challenger_inference.build_challenger_inference (the
  same cluster-bootstrap + Holm-correction engine the pump-short reports
  use) rather than a hand-rolled mean/CI.

Known, disclosed limitation this pass does NOT fix: no real bid/ask
slippage/impact model exists for these synthetic entries (unlike the
pump-short paper contracts, which fetch a real order-book VWAP). Net
economics here are fees + funding only (entry/exit slippage passed as
0.0 bps explicitly, not omitted silently) -- labeled `costs_partial` in
the report, not presented as a full paper-trade cost model.

Discovery-level only: reuses the same 2026-08-10..2026-08-18 Bybit window
several other screens this week already looked at (not a genuinely
untouched forward cohort), and does not authorize any change to the live
WATCH/paper contracts regardless of its own verdict.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from schurfer_performance.accounting import DEFAULT_COSTS, calculate_performance

from .challenger_inference import (
    DEFAULT_INFERENCE_SETTINGS,
    ChallengerEpisode,
    ChallengerInference,
    InferenceSettings,
    build_challenger_inference,
)

DIRECTIONS = ("buy", "sell")
OUTCOME_HORIZONS_MINUTES = (15, 60, 240, 720)
PRECURSOR_LOOKBACK_MINUTES = 60
# Position notional matches momentum_flow_paper_contract's own $50 default --
# not a claim this study shares that contract, just a consistent, comparable
# unit for the after-cost dollar figures.
POSITION_USD = 50.0


@dataclass(frozen=True)
class BurstMinute:
    exchange: str
    symbol: str
    bucket_start: datetime
    close_price: float
    buy_burst_pct_5m: float
    sell_burst_pct_5m: float

    def __post_init__(self) -> None:
        if self.close_price <= 0:
            raise ValueError("close_price must be positive")


@dataclass(frozen=True)
class BurstEpisode:
    episode_id: int
    exchange: str
    symbol: str
    direction: str  # "buy" or "sell"
    trigger_at: datetime
    peak_burst_pct: float
    extreme_minutes: int

    def __post_init__(self) -> None:
        if self.direction not in DIRECTIONS:
            raise ValueError(f"direction must be one of {DIRECTIONS}")
        if self.extreme_minutes < 1:
            raise ValueError("extreme_minutes must be at least 1")

    @property
    def cluster_key(self) -> str:
        return self.symbol

    @property
    def week_key(self) -> str:
        iso = self.trigger_at.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"


def decluster_episodes(
    minutes: tuple[BurstMinute, ...],
    *,
    direction: str,
    threshold_pct: float,
    refractory_minutes: int,
    start_id: int = 1,
) -> tuple[BurstEpisode, ...]:
    """Group consecutive-in-time extreme-burst minutes for the same symbol
    into one episode, keyed by the FIRST such minute (the trigger). A new
    episode starts only once `refractory_minutes` has passed since the
    LAST extreme-burst minute for that symbol -- not merely a different
    calendar minute, the actual bug the first-pass screen's own
    per-symbol-only declustering had (a single 14-minute burst run for one
    symbol counted as 14 "independent" observations).

    start_id (default 1) lets a caller combining episodes from more than
    one call -- buy and sell are always two separate calls, see
    momentum_flow_bidirectional_burst_report.py -- keep episode_id unique
    across the combined set. Both direction calls otherwise restart at 1,
    which build_challenger_inference's own uniqueness check on
    pump_event_id caught the very first time this ran against real data
    (a buy episode and a sell episode sharing episode_id=1)."""
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}")
    if threshold_pct <= 0:
        raise ValueError("threshold_pct must be positive")
    if refractory_minutes <= 0:
        raise ValueError("refractory_minutes must be positive")
    if start_id <= 0:
        raise ValueError("start_id must be positive")

    by_symbol: dict[tuple[str, str], list[BurstMinute]] = defaultdict(list)
    for minute in minutes:
        value = minute.buy_burst_pct_5m if direction == "buy" else minute.sell_burst_pct_5m
        if value >= threshold_pct:
            by_symbol[(minute.exchange, minute.symbol)].append(minute)

    refractory = timedelta(minutes=refractory_minutes)
    episodes: list[BurstEpisode] = []
    next_id = start_id
    for (exchange, symbol), symbol_minutes in by_symbol.items():
        ordered = sorted(symbol_minutes, key=lambda m: m.bucket_start)
        run_start: BurstMinute | None = None
        run_last_at: datetime | None = None
        run_peak = 0.0
        run_count = 0

        for minute in ordered:
            value = minute.buy_burst_pct_5m if direction == "buy" else minute.sell_burst_pct_5m
            if (
                run_start is not None
                and run_last_at is not None
                and minute.bucket_start - run_last_at >= refractory
            ):
                episodes.append(
                    BurstEpisode(
                        episode_id=next_id,
                        exchange=exchange,
                        symbol=symbol,
                        direction=direction,
                        trigger_at=run_start.bucket_start,
                        peak_burst_pct=run_peak,
                        extreme_minutes=run_count,
                    )
                )
                next_id += 1
                run_start = None
            if run_start is None:
                run_start = minute
                run_peak = value
                run_count = 0
            run_peak = max(run_peak, value)
            run_count += 1
            run_last_at = minute.bucket_start
        if run_start is not None:
            episodes.append(
                BurstEpisode(
                    episode_id=next_id,
                    exchange=exchange,
                    symbol=symbol,
                    direction=direction,
                    trigger_at=run_start.bucket_start,
                    peak_burst_pct=run_peak,
                    extreme_minutes=run_count,
                )
            )
            next_id += 1

    return tuple(sorted(episodes, key=lambda e: (e.exchange, e.symbol, e.trigger_at)))


@dataclass(frozen=True)
class EpisodeOutcome:
    episode: BurstEpisode
    trigger_price: float
    precursor_return_pct: (
        float | None
    )  # price move in the PRECURSOR_LOOKBACK_MINUTES before trigger
    # {horizon_minutes: (long_gross_pct, short_gross_pct)} -- None when the
    # exact-timestamp price at that horizon was never captured.
    horizon_returns_pct: dict[int, tuple[float, float]]


def compute_episode_outcomes(
    episodes: tuple[BurstEpisode, ...],
    price_at: dict[tuple[str, datetime], float],
    *,
    horizons_minutes: tuple[int, ...] = OUTCOME_HORIZONS_MINUTES,
    precursor_lookback_minutes: int = PRECURSOR_LOOKBACK_MINUTES,
) -> tuple[EpisodeOutcome, ...]:
    """price_at must be an EXACT-timestamp lookup (symbol, bucket_start) ->
    close_price, never a LEAD/LAG row-position approximation -- see the
    module's own doc comment for why that distinction is the whole point
    of this rebuild. A horizon with no exact match in price_at is left
    None (genuinely unresolved), not silently dropped or approximated
    from a neighboring minute."""
    outcomes: list[EpisodeOutcome] = []
    for episode in episodes:
        trigger_price = price_at.get((episode.symbol, episode.trigger_at))
        if trigger_price is None:
            continue
        precursor_at = episode.trigger_at - timedelta(minutes=precursor_lookback_minutes)
        precursor_price = price_at.get((episode.symbol, precursor_at))
        precursor_return_pct = (
            (trigger_price / precursor_price - 1) * 100 if precursor_price else None
        )
        horizon_returns: dict[int, tuple[float, float]] = {}
        for horizon in horizons_minutes:
            future_at = episode.trigger_at + timedelta(minutes=horizon)
            future_price = price_at.get((episode.symbol, future_at))
            if future_price is None:
                continue
            long_pct = (future_price / trigger_price - 1) * 100
            short_pct = -long_pct
            horizon_returns[horizon] = (long_pct, short_pct)
        outcomes.append(
            EpisodeOutcome(
                episode=episode,
                trigger_price=trigger_price,
                precursor_return_pct=precursor_return_pct,
                horizon_returns_pct=horizon_returns,
            )
        )
    return tuple(outcomes)


@dataclass(frozen=True)
class HorizonEconomics:
    horizon_minutes: int
    side: str  # "long" or "short"
    n: int
    mean_gross_pct: float
    mean_net_pct: float  # fees + funding only, no slippage/impact model -- see module doc
    win_rate_pct: float
    baseline_mean_gross_pct: float | None  # same-symbol unconditional mean, matched control
    inference: ChallengerInference | None


def _after_cost_return_pct(gross_pct: float, *, side: str, duration_minutes: float) -> float:
    entry_price = 100.0
    exit_price = (
        entry_price * (1 + gross_pct / 100)
        if side == "long"
        else entry_price * (1 - gross_pct / 100)
    )
    result = calculate_performance(
        position_usd=POSITION_USD,
        entry_price=entry_price,
        exit_price=exit_price,
        side=side,
        duration_minutes=duration_minutes,
        # Explicit 0.0, not None: no real book/impact model exists for these
        # synthetic entries (see module doc). Passing 0.0 keeps fees+funding
        # accounted for and status="complete" instead of "incomplete", while
        # staying honest that slippage itself is not modeled here.
        entry_slippage_bps=0.0,
        exit_slippage_bps=0.0,
        costs=DEFAULT_COSTS,
    )
    assert result.net_return_pct is not None  # slippage supplied above, never None
    return result.net_return_pct


def build_horizon_economics(
    outcomes: tuple[EpisodeOutcome, ...],
    baseline_forward_returns: dict[str, dict[int, float]],
    *,
    horizons_minutes: tuple[int, ...] = OUTCOME_HORIZONS_MINUTES,
    settings: InferenceSettings = DEFAULT_INFERENCE_SETTINGS,
) -> tuple[HorizonEconomics, ...]:
    results: list[HorizonEconomics] = []
    for horizon in horizons_minutes:
        for side_index, side in enumerate(("long", "short")):
            resolved = [
                (outcome, outcome.horizon_returns_pct[horizon][side_index])
                for outcome in outcomes
                if horizon in outcome.horizon_returns_pct
            ]
            if not resolved:
                continue
            net_returns = [
                _after_cost_return_pct(gross, side=side, duration_minutes=float(horizon))
                for _, gross in resolved
            ]
            wins = sum(1 for value in net_returns if value > 0)
            # The short side trades the mirror image of the raw (long-framed)
            # forward return, so its matched control must be sign-flipped the
            # same way -- both the display-only baseline_mean_gross_pct below
            # AND the baseline fed into the paired cluster-bootstrap inference
            # (a challenger vs. baseline delta computed against the un-flipped
            # long baseline would silently score every short-side horizon
            # against the wrong control).
            sign = 1 if side == "long" else -1

            def _signed_baseline(
                symbol: str, *, sign: float = sign, horizon: int = horizon
            ) -> float | None:
                per_horizon = baseline_forward_returns.get(symbol)
                if per_horizon is None or horizon not in per_horizon:
                    return None
                return sign * per_horizon[horizon]

            baseline_values = [
                value
                for outcome, _ in resolved
                if (value := _signed_baseline(outcome.episode.symbol)) is not None
            ]
            challenger_episodes = tuple(
                ChallengerEpisode(
                    pump_event_id=outcome.episode.episode_id,
                    cluster_key=outcome.episode.cluster_key,
                    baseline_return_pct=_signed_baseline(outcome.episode.symbol),
                    challenger_returns_pct=(("burst_entry", net_return),),
                )
                for (outcome, _gross), net_return in zip(resolved, net_returns, strict=True)
            )
            inference = (
                build_challenger_inference(challenger_episodes, ("burst_entry",), settings=settings)
                if len(challenger_episodes) >= 2
                else None
            )
            results.append(
                HorizonEconomics(
                    horizon_minutes=horizon,
                    side=side,
                    n=len(resolved),
                    mean_gross_pct=sum(gross for _, gross in resolved) / len(resolved),
                    mean_net_pct=sum(net_returns) / len(net_returns),
                    win_rate_pct=wins / len(net_returns) * 100,
                    baseline_mean_gross_pct=(
                        sum(baseline_values) / len(baseline_values) if baseline_values else None
                    ),
                    inference=inference,
                )
            )
    return tuple(results)
