"""Outcome-blind scanner for the abnormal-flow economic screen.

THIS RELEASE IS A COUNTS-ONLY SCANNER (calibration, not an economic result). The
returns-reading path exists but is HARD-DISABLED: ``FORMAL_RETURNS_RUN_ENABLED`` is
False and :class:`FormalReplay.run` raises :class:`ReturnsRunDisabledError`
unconditionally, even for a fully frozen contract, so no forward return can be read.
Enabling it, a full registered input fingerprint, the OI ablation, the portfolio
simulation, and the one-shot verdict are a later PR, still before any returns are read.

PRE-REGISTRATION INVARIANT (for when the run is later enabled). Reading forward returns
is gated behind a fully frozen contract AND the exact frozen dataset: the run would call
``contract.require_frozen()``, then refuse unless the loader's observed input fingerprint
equals the pinned one and every decision falls inside the registered UTC window, all
before any outcome reader is invoked. Scanning inputs, computing the pre-decision
features, applying eligibility and the primary/ablation cells, forming episodes, and
selecting matched controls are all outcome-blind and never touch a forward price. This
module therefore cannot tune a threshold to, score a window it did not pin, or peek at
the returns it will later score.

The decision logic (feature assembly, OI->USD conversion, participation, eligibility,
the primary and ablation cells, episode formation, control matching, the priced-proxy
economics) is expressed as small pure functions so it can be reviewed and unit-tested
with synthetic rows, before any real dataset or return is read.
:func:`assemble_decisions` turns per-instrument outcome-blind minute bars into
:class:`DecisionFeatures` using the frozen feature forms and the registered scan lag;
the loader that reads those bars from frozen, fidelity-verified cold-bar Parquet, and
the reader that later fetches the forward priced-proxy legs, are the only edges that
touch the dataset, and the forward prices are read solely by the frozen-gated run.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .abnormal_flow_input_audit import verified_input
from .abnormal_flow_screen import (
    LOOKBACK_MINUTES,
    AbnormalFlowContract,
    buy_pressure_ratio_60m,
    oi_growth_pct_60m,
    price_containment_max_bar_dev,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from pathlib import Path

    # A point-in-time canonical-identity resolver: given a route and a decision instant
    # it returns the canonical asset live at that instant, or ``None`` if it cannot be
    # resolved. The scanner never invents identity; the caller supplies the existing
    # resolver, and an unresolved route is counted in the funnel.
    CanonicalResolver = Callable[[str, str, str, str, datetime], "str | None"]

REPLAY_VERSION = "abnormal_flow_replay_v1"

# This release ships an OUTCOME-BLIND SCANNER only. Reading forward returns is disabled
# at the hardest level: FormalReplay.run refuses unconditionally, even for a fully
# frozen contract, so no returns can be read before the separate outcome-blind threshold
# and window freeze (a later PR). Flipping this flag is a deliberate, reviewed change.
FORMAL_RETURNS_RUN_ENABLED = False

# Conservative funding charge for the registered ``conservative_8h_v1`` model: a
# worst-case cost is applied for every 8h settlement the 720m hold can cross. It is a
# pre-registered pessimistic constant, not a measured per-instrument rate, so the
# historical economics never flatter themselves with a favourable funding assumption.
_CONSERVATIVE_FUNDING_BPS_PER_8H = 3.0
_FUNDING_SETTLEMENT_MINUTES = 480


def _finite(x: float | None) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)


# --- Outcome-blind decision record -------------------------------------------------


class FreezeMismatchError(RuntimeError):
    """The data handed to a formal run does not match the frozen contract: the input
    fingerprint differs, or a decision falls outside the registered UTC window. Reading
    returns is refused, so the scored dataset can never drift from the pinned one."""


class ReturnsRunDisabledError(RuntimeError):
    """A formal returns-reading run was attempted while this outcome-blind scanner
    release has returns reading disabled (``FORMAL_RETURNS_RUN_ENABLED`` is False). It
    is raised unconditionally, even for a fully frozen contract, before any freeze
    check or outcome read."""


# A candidate's exact native route + decision instant. Includes the exchange, market
# type, native market id, and capture_version so rows that share a symbol and minute
# across venues OR across capture regimes are never conflated into one outcome.
RouteKey = tuple[str, str, str, str, datetime]


@dataclass(frozen=True)
class DecisionFeatures:
    """Everything known at (and only at) the decision instant. Contains NO forward
    price: it is the outcome-blind half of a candidate. ``decision_price`` is the last
    pre-decision close (used for the Binance OI->USD conversion and nothing forward).
    ``canonical_asset`` is the point-in-time resolved identity, empty when unresolved."""

    exchange: str
    market_type: str
    native_market_id: str
    capture_version: str
    symbol: str
    canonical_asset: str
    decision_at: datetime
    oi_growth_pct: float | None
    buy_pressure: float | None
    containment: float | None
    oi_native_amount: float | None
    oi_native_value_usd: float | None  # populated on Bybit; None on Binance
    decision_price: float | None
    pre_decision_turnover_usd: float | None
    iso_week: str
    unavailable_reason: str | None = None

    def route_key(self) -> RouteKey:
        return (
            self.exchange,
            self.market_type,
            self.native_market_id,
            self.capture_version,
            self.decision_at,
        )


@dataclass(frozen=True)
class Outcome:
    """The returns-bearing half of a candidate. Read ONLY by the frozen-gated formal
    run: entry is the next-bar open priced proxy, exit the horizon-bar close proxy."""

    exchange: str
    market_type: str
    native_market_id: str
    capture_version: str
    symbol: str
    decision_at: datetime
    entry_price: float | None
    exit_price: float | None

    def route_key(self) -> RouteKey:
        return (
            self.exchange,
            self.market_type,
            self.native_market_id,
            self.capture_version,
            self.decision_at,
        )


# --- Outcome-blind minute bar + feature assembly -----------------------------------


@dataclass(frozen=True)
class MinuteBar:
    """One outcome-blind 1m bar for a single instrument. Only fields available at or
    before the bar's finalization are carried; no forward price appears here."""

    exchange: str
    market_type: str
    native_market_id: str
    capture_version: str
    symbol: str
    bucket_start: datetime
    created_at: datetime
    open_price: float | None
    high_price: float | None
    low_price: float | None
    close_price: float | None
    buy_notional_usd: float
    sell_notional_usd: float
    open_interest: float | None
    open_interest_value: float | None
    open_interest_observed_at: datetime | None
    last_trade_received_at: datetime | None
    price_complete: bool
    trades_complete: bool
    open_interest_complete: bool


def _iso_week(moment: datetime) -> str:
    year, week, _ = moment.isocalendar()
    return f"{year:04d}-W{week:02d}"


def _oi_fresh(bar: MinuteBar, freshness: timedelta, decision_at: datetime) -> bool:
    """The OI observation backing ``bar`` is usable: present, not observed after the
    decision, and not older than ``freshness`` before the bar it belongs to."""
    observed = bar.open_interest_observed_at
    if observed is None or observed > decision_at:
        return False
    return observed >= bar.bucket_start - freshness


def _unavailable_decision(
    end: MinuteBar, decision_at: datetime, reason: str, *, canonical: str = ""
) -> DecisionFeatures:
    """A decision whose feature window could not be used, with the rejection reason
    recorded (never a fabricated feature). Carries only outcome-blind identity."""
    return DecisionFeatures(
        exchange=end.exchange,
        market_type=end.market_type,
        native_market_id=end.native_market_id,
        capture_version=end.capture_version,
        symbol=end.symbol,
        canonical_asset=canonical,
        decision_at=decision_at,
        oi_growth_pct=None,
        buy_pressure=None,
        containment=None,
        oi_native_amount=end.open_interest,
        oi_native_value_usd=end.open_interest_value,
        decision_price=end.close_price,
        pre_decision_turnover_usd=None,
        iso_week=_iso_week(decision_at),
        unavailable_reason=reason,
    )


def oi_freshness_limit_for(contract: AbnormalFlowContract, exchange: str) -> int | None:
    """The registered per-venue OI freshness ceiling (seconds), or ``None`` for an
    unknown venue (which is then never accepted)."""
    ex = exchange.lower()
    if ex == "bybit":
        return contract.oi_freshness_limit_seconds_bybit
    if ex == "binance":
        return contract.oi_freshness_limit_seconds_binance
    return None


def assemble_decisions(
    bars: Sequence[MinuteBar],
    *,
    scan_lag_minutes: int,
    entry_execution_window_minutes: int,
    oi_freshness_limit_seconds: int,
    resolve_canonical: Callable[[datetime], str | None],
) -> list[DecisionFeatures]:
    """Turn one instrument's ordered, outcome-blind minute bars into per-minute
    :class:`DecisionFeatures` using the frozen feature forms. All bars must share one
    capture_version (see :func:`assemble_all`), so a feature window never spans a
    capture regime change.

    The decision at bar ``i`` is timed AFTER that bar finalizes plus the registered
    scan lag; the 60m feature window is ``bars[i-60 .. i]``, which must be contiguous,
    fully complete, and finalized/observed at or before the decision instant. Both the
    window-start and window-end OI observations must be no older than
    ``oi_freshness_limit_seconds`` before their bar (not just "not in the future").
    Trade timing is checked by feed health: a bar whose last trade was RECEIVED after
    the decision is late; a healthy no-trade minute (``last_trade_received_at`` is NULL
    but the bar is trade-complete and finalized) stays available. Canonical identity is
    resolved point-in-time via ``resolve_canonical(decision_at)``; an unresolved one is
    counted (``unresolved_identity``), never treated as its own ticker. A window that is
    short, gapped, incomplete, not finalized, resting on stale OI, holding a late trade,
    or of unresolved identity yields a decision marked ``unavailable_reason`` rather than
    a fabricated feature. No forward bar is read."""
    ordered = sorted(bars, key=lambda b: b.bucket_start)
    out: list[DecisionFeatures] = []
    window = LOOKBACK_MINUTES
    freshness = timedelta(seconds=oi_freshness_limit_seconds)
    for i in range(window, len(ordered)):
        end = ordered[i]
        start = ordered[i - window]
        decision_at = end.bucket_start + timedelta(minutes=1 + scan_lag_minutes)
        span = ordered[i - window : i + 1]

        canonical = resolve_canonical(decision_at)
        if not canonical:
            # Identity could not be resolved point-in-time: count it in the funnel,
            # never let it pass as its own distinct ticker (it would break dedup and
            # clustering).
            out.append(_unavailable_decision(end, decision_at, "unresolved_identity"))
            continue

        contiguous = all(
            (span[j].bucket_start - span[j - 1].bucket_start) == timedelta(minutes=1)
            for j in range(1, len(span))
        )
        if not contiguous:
            out.append(_unavailable_decision(end, decision_at, "lookback_gap", canonical=canonical))
            continue
        if any(
            not (b.price_complete and b.trades_complete and b.open_interest_complete) for b in span
        ):
            out.append(
                _unavailable_decision(end, decision_at, "incomplete_lookback", canonical=canonical)
            )
            continue
        if any(b.created_at > decision_at for b in span):
            out.append(
                _unavailable_decision(
                    end, decision_at, "not_finalized_by_decision", canonical=canonical
                )
            )
            continue
        # A NULL last_trade_received_at is a healthy no-trade minute (the bar is
        # trade-complete and finalized), not a missing trade; only a trade RECEIVED
        # after the decision is late.
        if any(
            b.last_trade_received_at is not None and b.last_trade_received_at > decision_at
            for b in span
        ):
            out.append(_unavailable_decision(end, decision_at, "late_trades", canonical=canonical))
            continue
        if not _oi_fresh(start, freshness, decision_at) or not _oi_fresh(
            end, freshness, decision_at
        ):
            out.append(_unavailable_decision(end, decision_at, "stale_oi", canonical=canonical))
            continue

        flow_bars = span[1:]  # the 60 bars within the hour
        buy_sum = sum(b.buy_notional_usd for b in flow_bars)
        sell_sum = sum(b.sell_notional_usd for b in flow_bars)
        exec_bars = span[max(0, len(span) - entry_execution_window_minutes) :]
        pre_decision_turnover = sum(b.buy_notional_usd + b.sell_notional_usd for b in exec_bars)
        containment_open = flow_bars[0].open_price
        highs_lows = [
            (b.high_price, b.low_price)
            for b in flow_bars
            if b.high_price is not None and b.low_price is not None
        ]
        containment = (
            price_containment_max_bar_dev(containment_open, highs_lows)
            if containment_open is not None and len(highs_lows) == len(flow_bars)
            else None
        )

        out.append(
            DecisionFeatures(
                exchange=end.exchange,
                market_type=end.market_type,
                native_market_id=end.native_market_id,
                capture_version=end.capture_version,
                symbol=end.symbol,
                canonical_asset=canonical,
                decision_at=decision_at,
                oi_growth_pct=oi_growth_pct_60m(start.open_interest, end.open_interest),  # type: ignore[arg-type]
                buy_pressure=buy_pressure_ratio_60m(buy_sum, sell_sum),
                containment=containment,
                oi_native_amount=end.open_interest,
                oi_native_value_usd=end.open_interest_value,
                decision_price=end.close_price,
                pre_decision_turnover_usd=pre_decision_turnover
                if pre_decision_turnover > 0
                else None,
                iso_week=_iso_week(decision_at),
                unavailable_reason=None,
            )
        )
    return out


# --- Pure decision logic (outcome-blind) -------------------------------------------


def oi_notional_usd(
    exchange: str,
    native_amount: float | None,
    native_value_usd: float | None,
    decision_price: float | None,
) -> float | None:
    """Registered rule ``bybit_native_value_binance_amount_x_decision_price_v1``.

    Verified per venue against the collector: Bybit publishes a native USD
    open-interest value (use it directly); Binance publishes only the base-asset OI
    amount, so its USD OI is that amount times the decision-time price. An unknown
    venue, or a venue missing its required inputs, is ``None`` (ineligible) -- never a
    zero and never a cross-venue substitute."""
    ex = exchange.lower()
    if ex == "bybit":
        if _finite(native_value_usd) and native_value_usd is not None and native_value_usd >= 0:
            return native_value_usd
        return None
    if ex == "binance":
        if (
            _finite(native_amount)
            and native_amount is not None
            and native_amount >= 0
            and _finite(decision_price)
            and decision_price is not None
            and decision_price > 0
        ):
            return native_amount * decision_price
        return None
    return None


def participation_frac(
    position_usd: float | None, pre_decision_turnover_usd: float | None
) -> float | None:
    """The fixed position as a fraction of turnover accumulated over the pre-decision
    execution window (see ``entry_execution_window_minutes``). ``None`` when the
    window had no usable turnover, so a no-liquidity window is ineligible rather than
    silently tradeable."""
    if not _finite(position_usd) or position_usd is None or position_usd <= 0:
        return None
    if (
        not _finite(pre_decision_turnover_usd)
        or pre_decision_turnover_usd is None
        or pre_decision_turnover_usd <= 0
    ):
        return None
    return position_usd / pre_decision_turnover_usd


def is_eligible(contract: AbnormalFlowContract, oi_usd: float | None, part: float | None) -> bool:
    """Liquidity + proven-OI floor. Requires a frozen contract's floor fields; a row
    below the OI-notional floor or above the participation cap is out of scope."""
    if oi_usd is None or part is None:
        return False
    if contract.min_oi_notional_usd is None or contract.max_participation_frac is None:
        return False
    return oi_usd >= contract.min_oi_notional_usd and part <= contract.max_participation_frac


def primary_cell_fires(contract: AbnormalFlowContract, f: DecisionFeatures) -> bool:
    """The ONE primary cell: OI growth, buy dominance, and price containment all pass.
    Any unavailable feature is a non-fire (never silently treated as passing)."""
    if f.oi_growth_pct is None or f.buy_pressure is None or f.containment is None:
        return False
    if (
        contract.min_oi_growth_pct is None
        or contract.min_buy_pressure_ratio is None
        or contract.max_price_containment is None
    ):
        return False
    return (
        f.oi_growth_pct >= contract.min_oi_growth_pct
        and f.buy_pressure >= contract.min_buy_pressure_ratio
        and f.containment <= contract.max_price_containment
    )


def ablation_cell_fires(contract: AbnormalFlowContract, f: DecisionFeatures) -> bool:
    """The registered OI ablation: the SAME cell with ONLY the OI-growth threshold
    removed. Buy dominance and containment still apply, and the eligible set is
    unchanged, so any excess of the primary over this isolates the OI effect."""
    if f.buy_pressure is None or f.containment is None:
        return False
    if contract.min_buy_pressure_ratio is None or contract.max_price_containment is None:
        return False
    return (
        f.buy_pressure >= contract.min_buy_pressure_ratio
        and f.containment <= contract.max_price_containment
    )


def form_episodes(
    fires: Iterable[DecisionFeatures], cooldown_minutes: int
) -> list[DecisionFeatures]:
    """One episode per ``(exchange, canonical_asset)``: the first qualifying decision,
    then a cooldown at least as long as the outcome horizon before the next is counted,
    so overlapping fires on one instrument are not independent evidence. Deterministic:
    ordered by decision time, ties broken by symbol."""
    ordered = sorted(fires, key=lambda f: (f.exchange, f.canonical_asset, f.decision_at, f.symbol))
    kept: list[DecisionFeatures] = []
    last_kept_at: dict[tuple[str, str], datetime] = {}
    cooldown = timedelta(minutes=cooldown_minutes)
    for f in ordered:
        key = (f.exchange, f.canonical_asset)
        previous = last_kept_at.get(key)
        if previous is not None and f.decision_at - previous < cooldown:
            continue
        kept.append(f)
        last_kept_at[key] = f.decision_at
    return kept


# --- Control matching (outcome-blind, deterministic) -------------------------------


def _log_liquidity_band(turnover_usd: float | None) -> int:
    """Half-decade (log10 * 2) liquidity bucket, so controls share an order-of-magnitude
    of pre-decision turnover. ``None`` turnover has no band and cannot be matched."""
    if not _finite(turnover_usd) or turnover_usd is None or turnover_usd <= 0:
        return -(10**9)
    return math.floor(math.log10(turnover_usd) * 2)


def _containment_band(containment: float | None) -> int:
    """1%-wide pre-decision price-movement bucket, so a control experienced comparable
    contemporaneous movement and the excess is not a movement artefact."""
    if not _finite(containment) or containment is None or containment < 0:
        return -(10**9)
    return math.floor(containment * 100)


def control_band_key(f: DecisionFeatures) -> tuple[str, str, int, int] | None:
    """Deterministic key for ``same_venue_regime_liquidity_pricemove_band_v1``: same
    venue, same ISO week (calendar regime), same log-liquidity band, same
    price-movement band. ``None`` when any banded feature is unavailable, so an
    unbandable row is never matched by accident."""
    liq = _log_liquidity_band(f.pre_decision_turnover_usd)
    move = _containment_band(f.containment)
    if liq == -(10**9) or move == -(10**9):
        return None
    return (f.exchange, f.iso_week, liq, move)


def match_controls(
    fired: DecisionFeatures,
    pool: Sequence[DecisionFeatures],
    *,
    max_controls: int,
) -> list[DecisionFeatures]:
    """Point-in-time matched controls: eligible, non-firing decisions sharing the
    fired row's band key, nearest in decision time first, capped at ``max_controls``.
    Deterministic and outcome-blind (no forward price is consulted)."""
    key = control_band_key(fired)
    if key is None:
        return []
    same_band = [
        c
        for c in pool
        if c.decision_at != fired.decision_at or c.symbol != fired.symbol
        if control_band_key(c) == key
    ]
    same_band.sort(
        key=lambda c: (abs((c.decision_at - fired.decision_at).total_seconds()), c.symbol)
    )
    return same_band[:max_controls]


# --- Priced-proxy economics (RETURNS-BEARING; frozen-gated callers only) ------------


def proxy_net_return(
    contract: AbnormalFlowContract, entry_price: float | None, exit_price: float | None
) -> float | None:
    """Net fractional return of the long priced proxy over the horizon, after the
    pre-registered conservative entry cost, slippage, round-trip fee, and worst-case
    funding. ``None`` when either priced-proxy leg is unavailable (an unresolved
    outcome, never a filled-in one).

    Returns-bearing: callers must already hold a frozen contract. This function does
    not itself read the dataset; it scores prices the frozen-gated run supplied."""
    if (
        not _finite(entry_price)
        or entry_price is None
        or entry_price <= 0
        or not _finite(exit_price)
        or exit_price is None
        or exit_price <= 0
    ):
        return None
    for name in ("entry_cost_bps", "slippage_bps", "fee_bps"):
        if not _finite(getattr(contract, name)):
            raise ValueError(f"cost model incomplete: {name} is not set")
    gross = (exit_price - entry_price) / entry_price  # long
    entry_cost = float(contract.entry_cost_bps)  # type: ignore[arg-type]
    slippage = float(contract.slippage_bps)  # type: ignore[arg-type]
    fee = float(contract.fee_bps)  # type: ignore[arg-type]
    settlements = math.ceil(contract.outcome_horizon_minutes / _FUNDING_SETTLEMENT_MINUTES)
    funding_bps = _CONSERVATIVE_FUNDING_BPS_PER_8H * settlements
    cost_bps = entry_cost + slippage + 2.0 * fee + funding_bps  # round-trip fees
    return gross - cost_bps / 10_000.0


# --- Funnel accounting -------------------------------------------------------------


@dataclass
class Funnel:
    """Outcome-blind accounting of what happened to every scanned decision, by
    rejection reason, so the sample is auditable before any economics are read."""

    scanned: int = 0
    unavailable_feature: int = 0
    ineligible: int = 0
    eligible: int = 0
    primary_fires: int = 0
    ablation_fires: int = 0
    primary_episodes: int = 0
    ablation_episodes: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def note(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


def _decision_eligible(contract: AbnormalFlowContract, d: DecisionFeatures) -> bool:
    """Outcome-blind eligibility of one decision under the contract's floor."""
    if d.unavailable_reason is not None:
        return False
    oi_usd = oi_notional_usd(
        d.exchange, d.oi_native_amount, d.oi_native_value_usd, d.decision_price
    )
    part = participation_frac(contract.position_usd, d.pre_decision_turnover_usd)
    return is_eligible(contract, oi_usd, part)


def build_funnel(contract: AbnormalFlowContract, decisions: Iterable[DecisionFeatures]) -> Funnel:
    """Outcome-blind: classify each decision into the funnel and form episodes. Reads
    no forward price, so it is safe to run during discovery. It does NOT require a
    frozen contract, but a fire count is only meaningful once thresholds are frozen."""
    funnel = Funnel()
    primary: list[DecisionFeatures] = []
    ablation: list[DecisionFeatures] = []
    for d in decisions:
        funnel.scanned += 1
        if d.unavailable_reason is not None:
            funnel.unavailable_feature += 1
            funnel.note(d.unavailable_reason)
            continue
        if not _decision_eligible(contract, d):
            funnel.ineligible += 1
            funnel.note("below_eligibility_floor")
            continue
        funnel.eligible += 1
        if primary_cell_fires(contract, d):
            funnel.primary_fires += 1
            primary.append(d)
        if ablation_cell_fires(contract, d):
            funnel.ablation_fires += 1
            ablation.append(d)
    cooldown = contract.cooldown_minutes
    funnel.primary_episodes = len(form_episodes(primary, cooldown))
    funnel.ablation_episodes = len(form_episodes(ablation, cooldown))
    return funnel


# --- Economics, portfolio, verdict (RETURNS-BEARING; pure over resolved episodes) ---


@dataclass(frozen=True)
class EpisodeRecord:
    """One resolved episode's returns-bearing summary, used for the aggregate report.
    Produced only inside the frozen-gated run."""

    canonical_asset: str
    iso_week: str
    decision_at: datetime
    net_return: float
    excess: float | None  # None when the episode had no resolved control


@dataclass(frozen=True)
class PortfolioResult:
    """Fixed-bank, slot-limited portfolio outcome over the resolved episodes. A signal
    arriving while every slot is occupied for the horizon is dropped (capacity), not
    silently stacked, so the dollar path reflects real concurrency limits."""

    taken_trades: int
    skipped_capacity: int
    total_pnl_usd: float
    max_drawdown_usd: float
    longest_losing_streak: int
    max_concurrency: int


@dataclass(frozen=True)
class EconomicsReport:
    """The one-shot pre-registered report. Every figure is after-cost; nothing here is
    read or computed until the freeze gates in :meth:`FormalReplay.run` have passed."""

    resolved_episodes: int
    unresolved_episodes: int
    missing_fraction: float | None
    n_weeks: int
    mean_net_return: float | None
    weekly_clustered_se: float | None
    mean_excess_over_control: float | None
    leave_one_out_excess_min: float | None
    leave_one_out_excess_max: float | None
    break_even_extra_cost_bps: float | None
    portfolio: PortfolioResult
    verdict: str


def simulate_portfolio(
    contract: AbnormalFlowContract, episodes: Sequence[tuple[datetime, float]]
) -> PortfolioResult:
    """Slot-limited fixed-bank simulation. ``episodes`` are ``(decision_at, net_return)``
    for resolved episodes; each taken trade holds one slot for the outcome horizon and
    earns ``position_usd * net_return``. Drawdown and losing streak are measured on the
    realized dollar path in entry order."""
    slots = contract.portfolio_max_slots or 1
    position_usd = float(contract.position_usd)  # type: ignore[arg-type]
    horizon = timedelta(minutes=contract.outcome_horizon_minutes)
    active_ends: list[datetime] = []
    taken = 0
    skipped = 0
    pnls: list[float] = []
    max_concurrency = 0
    for decision_at, ret in sorted(episodes, key=lambda e: e[0]):
        active_ends = [t for t in active_ends if t > decision_at]
        if len(active_ends) >= slots:
            skipped += 1
            continue
        active_ends.append(decision_at + horizon)
        max_concurrency = max(max_concurrency, len(active_ends))
        taken += 1
        pnls.append(position_usd * ret)
    peak = 0.0
    cum = 0.0
    max_dd = 0.0
    streak = 0
    longest = 0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        if p < 0:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0
    return PortfolioResult(
        taken_trades=taken,
        skipped_capacity=skipped,
        total_pnl_usd=sum(pnls),
        max_drawdown_usd=max_dd,
        longest_losing_streak=longest,
        max_concurrency=max_concurrency,
    )


def _weekly_clustered_se(records: Sequence[EpisodeRecord]) -> tuple[int, float | None]:
    """Week count and the standard error of the equal-weighted weekly mean net return,
    treating each ISO week as a cluster. ``None`` SE with fewer than two weeks (a single
    cluster carries no cross-week uncertainty)."""
    by_week: dict[str, list[float]] = defaultdict(list)
    for r in records:
        by_week[r.iso_week].append(r.net_return)
    weekly_means = [sum(v) / len(v) for v in by_week.values()]
    n = len(weekly_means)
    if n < 2:
        return n, None
    mean = sum(weekly_means) / n
    variance = sum((m - mean) ** 2 for m in weekly_means) / (n - 1)
    return n, math.sqrt(variance / n)


def _leave_one_out_excess(
    records: Sequence[EpisodeRecord],
) -> tuple[float | None, float | None]:
    """Min and max mean excess when each canonical asset is dropped in turn, so a single
    asset cannot carry the excess. ``None`` when fewer than two assets have a resolved
    control excess."""
    with_excess = [(r.canonical_asset, r.excess) for r in records if r.excess is not None]
    assets = {a for a, _ in with_excess}
    if len(assets) < 2:
        return None, None
    means: list[float] = []
    for drop in assets:
        kept = [e for a, e in with_excess if a != drop]
        if kept:
            means.append(sum(kept) / len(kept))
    if not means:
        return None, None
    return min(means), max(means)


def render_verdict(
    contract: AbnormalFlowContract,
    *,
    resolved_episodes: int,
    unresolved_episodes: int,
    mean_net_return: float | None,
    mean_excess_over_control: float | None,
) -> str:
    """The pre-registered one-shot verdict. Underpowered or too-incomplete evidence is
    INSUFFICIENT_EVIDENCE (not a pass); a non-positive net or an excess below the
    registered floor is FAIL; only positive after-cost net AND sufficient matched excess
    is PASS_DISCOVERY (which authorizes at most a separate forward cohort, never live)."""
    total = resolved_episodes + unresolved_episodes
    missing = (unresolved_episodes / total) if total else None
    if contract.min_resolved_episodes is None or resolved_episodes < contract.min_resolved_episodes:
        return "INSUFFICIENT_EVIDENCE"
    if (
        contract.max_missing_fraction is not None
        and missing is not None
        and missing > contract.max_missing_fraction
    ):
        return "INSUFFICIENT_EVIDENCE"
    if mean_net_return is None or mean_net_return <= 0:
        return "FAIL"
    floor = (contract.min_excess_over_control_pct or 0.0) / 100.0
    if mean_excess_over_control is None or mean_excess_over_control < floor:
        return "FAIL"
    return "PASS_DISCOVERY"


def build_report(
    contract: AbnormalFlowContract,
    records: Sequence[EpisodeRecord],
    *,
    unresolved_episodes: int,
) -> EconomicsReport:
    """Assemble the one-shot economics report from resolved episode records. Pure and
    deterministic; every input is already after-cost."""
    resolved = len(records)
    total = resolved + unresolved_episodes
    missing = (unresolved_episodes / total) if total else None
    net_returns = [r.net_return for r in records]
    excesses = [r.excess for r in records if r.excess is not None]
    mean_net = (sum(net_returns) / len(net_returns)) if net_returns else None
    mean_excess = (sum(excesses) / len(excesses)) if excesses else None
    n_weeks, se = _weekly_clustered_se(records)
    loo_min, loo_max = _leave_one_out_excess(records)
    break_even = (mean_net * 10_000.0) if (mean_net is not None and mean_net > 0) else 0.0
    portfolio = simulate_portfolio(contract, [(r.decision_at, r.net_return) for r in records])
    verdict = render_verdict(
        contract,
        resolved_episodes=resolved,
        unresolved_episodes=unresolved_episodes,
        mean_net_return=mean_net,
        mean_excess_over_control=mean_excess,
    )
    return EconomicsReport(
        resolved_episodes=resolved,
        unresolved_episodes=unresolved_episodes,
        missing_fraction=missing,
        n_weeks=n_weeks,
        mean_net_return=mean_net,
        weekly_clustered_se=se,
        mean_excess_over_control=mean_excess,
        leave_one_out_excess_min=loo_min,
        leave_one_out_excess_max=loo_max,
        break_even_extra_cost_bps=break_even,
        portfolio=portfolio,
        verdict=verdict,
    )


# --- Formal run (RETURNS-BEARING; require_frozen fail-closed) -----------------------


@dataclass(frozen=True)
class ReplayResult:
    replay_version: str
    funnel: Funnel
    resolved_episodes: int
    unresolved_episodes: int
    episodes_with_matched_control: int
    resolved_controls: int
    unresolved_controls: int
    mean_net_return: float | None
    mean_control_return: float | None
    mean_excess_over_control: float | None
    report: EconomicsReport
    episode_records: tuple[EpisodeRecord, ...]


def _window_bounds(contract: AbnormalFlowContract) -> tuple[datetime, datetime]:
    assert contract.window_start_utc is not None and contract.window_end_utc is not None
    start = datetime.fromisoformat(contract.window_start_utc.replace("Z", "+00:00"))
    end = datetime.fromisoformat(contract.window_end_utc.replace("Z", "+00:00"))
    return start, end


class FormalReplay:
    """Runs the returns-reading replay. Construction is harmless; ``run`` is the only
    entry that reads outcomes, and it fail-closes BEFORE any outcome reader is invoked:
    the contract must be frozen, the loader's observed input fingerprint must equal the
    pinned one, and every decision must fall inside the registered UTC window."""

    def __init__(self, contract: AbnormalFlowContract) -> None:
        self._contract = contract

    def run(
        self,
        decisions: Sequence[DecisionFeatures],
        read_outcomes: Callable[[Sequence[DecisionFeatures]], dict[RouteKey, Outcome]],
        *,
        observed_input_fingerprint: str,
    ) -> ReplayResult:
        # Hardest gate FIRST: this scanner release does not read returns at all. Refuse
        # unconditionally, even for a fully frozen contract, before touching the reader.
        if not FORMAL_RETURNS_RUN_ENABLED:
            raise ReturnsRunDisabledError(
                "formal returns-reading run is disabled in this outcome-blind scanner "
                "release; it lands in a later PR after the separate threshold/window freeze"
            )
        # Fail-closed BEFORE any outcome is read.
        self._contract.require_frozen()
        contract = self._contract

        # Bind the freeze to the data actually being scored: the loader's reproduced
        # fingerprint must match the pinned one, and no decision may fall outside the
        # registered window. Either mismatch refuses the run instead of reading returns
        # against a dataset the contract never pinned.
        if observed_input_fingerprint != contract.input_fingerprint:
            raise FreezeMismatchError(
                "observed input fingerprint does not match the frozen contract; "
                f"expected {contract.input_fingerprint!r}, got {observed_input_fingerprint!r}"
            )
        window_start, window_end = _window_bounds(contract)
        for d in decisions:
            if not (window_start <= d.decision_at < window_end):
                raise FreezeMismatchError(
                    f"decision at {d.decision_at.isoformat()} is outside the registered "
                    f"window [{contract.window_start_utc}, {contract.window_end_utc})"
                )

        funnel = build_funnel(contract, decisions)
        primary = [
            d
            for d in decisions
            if _decision_eligible(contract, d) and primary_cell_fires(contract, d)
        ]
        episodes = form_episodes(primary, contract.cooldown_minutes)

        eligible_pool = [
            d
            for d in decisions
            if _decision_eligible(contract, d) and not primary_cell_fires(contract, d)
        ]

        # Select controls BEFORE reading returns, then request outcomes for both groups
        # in one read keyed by the exact native route. A reader that returns only what
        # it was asked for is enough; the excess is no longer hidden by over-returning.
        controls_by_episode: dict[RouteKey, list[DecisionFeatures]] = {}
        controls_per_episode = contract.controls_per_episode or 1
        for ep in episodes:
            controls_by_episode[ep.route_key()] = match_controls(
                ep, eligible_pool, max_controls=controls_per_episode
            )

        requested: dict[RouteKey, DecisionFeatures] = {}
        for ep in episodes:
            requested[ep.route_key()] = ep
        for controls in controls_by_episode.values():
            for c in controls:
                requested[c.route_key()] = c

        # The single returns read, only now that every freeze gate has passed.
        outcomes = read_outcomes(list(requested.values()))

        def _return_for(d: DecisionFeatures) -> float | None:
            outcome = outcomes.get(d.route_key())
            if outcome is None:
                return None
            return proxy_net_return(contract, outcome.entry_price, outcome.exit_price)

        net_returns: list[float] = []
        excesses: list[float] = []
        control_means: list[float] = []
        records: list[EpisodeRecord] = []
        unresolved_episodes = 0
        episodes_with_matched_control = 0
        resolved_controls = 0
        unresolved_controls = 0
        for ep in episodes:
            r = _return_for(ep)
            if r is None:
                unresolved_episodes += 1
                continue
            net_returns.append(r)
            control_rs: list[float] = []
            for c in controls_by_episode[ep.route_key()]:
                cr = _return_for(c)
                if cr is None:
                    unresolved_controls += 1
                else:
                    resolved_controls += 1
                    control_rs.append(cr)
            excess: float | None = None
            if control_rs:
                episodes_with_matched_control += 1
                control_mean = sum(control_rs) / len(control_rs)
                control_means.append(control_mean)
                excess = r - control_mean
                excesses.append(excess)
            records.append(
                EpisodeRecord(
                    canonical_asset=ep.canonical_asset,
                    iso_week=ep.iso_week,
                    decision_at=ep.decision_at,
                    net_return=r,
                    excess=excess,
                )
            )

        report = build_report(contract, records, unresolved_episodes=unresolved_episodes)
        return ReplayResult(
            replay_version=REPLAY_VERSION,
            funnel=funnel,
            resolved_episodes=len(net_returns),
            unresolved_episodes=unresolved_episodes,
            episodes_with_matched_control=episodes_with_matched_control,
            resolved_controls=resolved_controls,
            unresolved_controls=unresolved_controls,
            mean_net_return=(sum(net_returns) / len(net_returns)) if net_returns else None,
            mean_control_return=(
                (sum(control_means) / len(control_means)) if control_means else None
            ),
            mean_excess_over_control=(sum(excesses) / len(excesses)) if excesses else None,
            report=report,
            episode_records=tuple(records),
        )


# --- Dataset edges: Parquet loader + priced-proxy outcome reader --------------------
#
# These are the only functions that touch the frozen cold-bar Parquet. The loader and
# the fingerprint are outcome-blind (they read no forward price). The outcome reader is
# returns-bearing and is only ever invoked by the frozen-gated run.

_INPUT_COLUMNS_SQL = """
SELECT exchange, market_type, symbol, capture_version, bucket_start, created_at,
       open_price, high_price, low_price, close_price,
       buy_total_notional_usd, sell_total_notional_usd,
       open_interest, open_interest_value, open_interest_observed_at,
       last_trade_received_at, price_complete, trades_complete, open_interest_complete
FROM read_parquet(?)
WHERE bucket_start >= ? AND bucket_start < ?
ORDER BY exchange, market_type, symbol, capture_version, bucket_start
"""

_OUTCOME_COLUMNS_SQL = """
SELECT exchange, market_type, symbol, capture_version, bucket_start, open_price, close_price
FROM read_parquet(?)
WHERE bucket_start >= ? AND bucket_start < ?
"""


def quote_suffix_canonical_resolver(
    exchange: str, market_type: str, native_market_id: str, capture_version: str, at: datetime
) -> str | None:
    """A heuristic FALLBACK resolver (strip a recognized quote suffix). It is NOT the
    production identity source: the real run passes the existing point-in-time resolver.
    Returns ``None`` when the symbol has no known quote suffix, so it never invents an
    identity. Exposed for tests and offline exploration only."""
    upper = native_market_id.upper()
    for quote in ("USDT", "USDC", "USD"):
        if upper.endswith(quote) and len(upper) > len(quote):
            return upper[: -len(quote)]
    return None


def input_fingerprint_for(bars: Sequence[MinuteBar]) -> str:
    """Deterministic SHA-256 over the outcome-blind input rows the loader read, so a
    run can prove it is scoring exactly the frozen dataset. Namespaced to match the
    contract's ``input_fingerprint`` format. (A fuller, registered fingerprint is a
    next-PR item; this pins the exact rows the scanner assembled.)"""
    hasher = hashlib.sha256()
    for b in sorted(
        bars, key=lambda b: (b.exchange, b.market_type, b.symbol, b.capture_version, b.bucket_start)
    ):
        hasher.update(
            "|".join(
                str(x)
                for x in (
                    b.exchange,
                    b.market_type,
                    b.symbol,
                    b.capture_version,
                    b.bucket_start.isoformat(),
                    b.open_price,
                    b.high_price,
                    b.low_price,
                    b.close_price,
                    b.buy_notional_usd,
                    b.sell_notional_usd,
                    b.open_interest,
                    b.open_interest_value,
                    b.price_complete,
                    b.trades_complete,
                    b.open_interest_complete,
                )
            ).encode()
        )
        hasher.update(b"\n")
    return "abnormal_flow_input_v1:" + hasher.hexdigest()


def _row_to_bar(row: tuple[Any, ...]) -> MinuteBar:
    symbol = str(row[2])
    return MinuteBar(
        exchange=str(row[0]),
        market_type=str(row[1]),
        native_market_id=symbol,
        capture_version=str(row[3]),
        symbol=symbol,
        bucket_start=row[4],
        created_at=row[5],
        open_price=row[6],
        high_price=row[7],
        low_price=row[8],
        close_price=row[9],
        buy_notional_usd=float(row[10] or 0.0),
        sell_notional_usd=float(row[11] or 0.0),
        open_interest=row[12],
        open_interest_value=row[13],
        open_interest_observed_at=row[14],
        last_trade_received_at=row[15],
        price_complete=bool(row[16]),
        trades_complete=bool(row[17]),
        open_interest_complete=bool(row[18]),
    )


def load_minute_bars_from_parquet(
    path: str, *, window_start: datetime, window_end: datetime
) -> list[MinuteBar]:
    """Read outcome-blind minute bars for the window from one cold-bar Parquet. Reads no
    forward price. (Fidelity/manifest verification is the caller's responsibility; see
    ``abnormal_flow_input_audit``.)"""
    import duckdb

    connection = duckdb.connect()
    try:
        rows = connection.execute(_INPUT_COLUMNS_SQL, [path, window_start, window_end]).fetchall()
    finally:
        connection.close()
    return [_row_to_bar(row) for row in rows]


def load_verified_minute_bars(cold_bars_dir: Path, *, start: date, end: date) -> list[MinuteBar]:
    """The production loader: for every UTC day in ``[start, end)`` it verifies the
    cold-bar manifest (file bytes + sha256, identity/bounds, and proven source
    fidelity) via ``verified_input`` BEFORE reading a single row, then reads that day's
    outcome-blind bars. A day whose manifest is missing, mismatched, or whose fidelity
    was never proven raises instead of silently shrinking the sample."""
    if end <= start:
        raise ValueError("end day must be after start day")
    bars: list[MinuteBar] = []
    day = start
    while day < end:
        path, _manifest = verified_input(cold_bars_dir, day)
        day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        bars.extend(
            load_minute_bars_from_parquet(
                str(path), window_start=day_start, window_end=day_start + timedelta(days=1)
            )
        )
        day += timedelta(days=1)
    return bars


def assemble_all(
    bars: Sequence[MinuteBar],
    contract: AbnormalFlowContract,
    *,
    resolve_canonical: CanonicalResolver,
) -> list[DecisionFeatures]:
    """Group bars by exact native route AND capture_version, then assemble decisions per
    instrument using the contract's registered scan lag, execution window, and per-venue
    OI freshness. Grouping by capture_version means a feature window never spans a
    capture regime change. Canonical identity is resolved point-in-time per decision via
    ``resolve_canonical``; a venue without a registered freshness ceiling is skipped
    (never silently accepted)."""
    by_route: dict[tuple[str, str, str, str], list[MinuteBar]] = defaultdict(list)
    for b in bars:
        by_route[(b.exchange, b.market_type, b.native_market_id, b.capture_version)].append(b)
    out: list[DecisionFeatures] = []
    scan_lag = contract.scan_lag_minutes
    exec_window = contract.entry_execution_window_minutes
    assert scan_lag is not None and exec_window is not None
    for (exchange, market_type, native_market_id, capture_version), group in by_route.items():
        freshness = oi_freshness_limit_for(contract, exchange)
        if freshness is None:
            continue

        def _resolve(
            at: datetime,
            _ex: str = exchange,
            _mt: str = market_type,
            _mid: str = native_market_id,
            _cv: str = capture_version,
        ) -> str | None:
            return resolve_canonical(_ex, _mt, _mid, _cv, at)

        out.extend(
            assemble_decisions(
                group,
                scan_lag_minutes=scan_lag,
                entry_execution_window_minutes=exec_window,
                oi_freshness_limit_seconds=freshness,
                resolve_canonical=_resolve,
            )
        )
    return out


def parquet_outcome_reader(
    path: str, *, outcome_horizon_minutes: int
) -> Callable[[Sequence[DecisionFeatures]], dict[RouteKey, Outcome]]:
    """Build a returns-bearing reader over one Parquet: entry is the priced-proxy open
    of the decision minute's bar, exit the close of the bar ``outcome_horizon_minutes``
    later, on the exact native route. Missing legs stay unresolved (never filled in)."""
    import duckdb

    def reader(requested: Sequence[DecisionFeatures]) -> dict[RouteKey, Outcome]:
        if not requested:
            return {}
        starts = [d.decision_at for d in requested]
        horizon = timedelta(minutes=outcome_horizon_minutes)
        lo = min(starts)
        hi = max(starts) + horizon + timedelta(minutes=1)
        connection = duckdb.connect()
        try:
            rows = connection.execute(_OUTCOME_COLUMNS_SQL, [path, lo, hi]).fetchall()
        finally:
            connection.close()
        opens: dict[tuple[str, str, str, str, datetime], float | None] = {}
        closes: dict[tuple[str, str, str, str, datetime], float | None] = {}
        for row in rows:
            key = (str(row[0]), str(row[1]), str(row[2]), str(row[3]), row[4])
            opens[key] = row[5]
            closes[key] = row[6]
        out: dict[RouteKey, Outcome] = {}
        for d in requested:
            entry_key = (
                d.exchange,
                d.market_type,
                d.native_market_id,
                d.capture_version,
                d.decision_at,
            )
            exit_key = (
                d.exchange,
                d.market_type,
                d.native_market_id,
                d.capture_version,
                d.decision_at + horizon,
            )
            out[d.route_key()] = Outcome(
                exchange=d.exchange,
                market_type=d.market_type,
                native_market_id=d.native_market_id,
                capture_version=d.capture_version,
                symbol=d.symbol,
                decision_at=d.decision_at,
                entry_price=opens.get(entry_key),
                exit_price=closes.get(exit_key),
            )
        return out

    return reader
