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
from typing import TYPE_CHECKING, Any, cast

from .abnormal_flow_input_audit import verified_input
from .abnormal_flow_screen import (
    LOOKBACK_MINUTES,
    AbnormalFlowContract,
    buy_pressure_ratio_60m,
    oi_growth_pct_60m,
    price_containment_max_bar_dev,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence
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


def no_oi_cell_fires(contract: AbnormalFlowContract, f: DecisionFeatures) -> bool:
    if f.buy_pressure is None or f.containment is None:
        return False
    if contract.min_buy_pressure_ratio is None or contract.max_price_containment is None:
        return False
    return (
        f.buy_pressure >= contract.min_buy_pressure_ratio
        and f.containment <= contract.max_price_containment
    )


def no_buy_cell_fires(contract: AbnormalFlowContract, f: DecisionFeatures) -> bool:
    if f.oi_growth_pct is None or f.containment is None:
        return False
    if contract.min_oi_growth_pct is None or contract.max_price_containment is None:
        return False
    return (
        f.oi_growth_pct >= contract.min_oi_growth_pct
        and f.containment <= contract.max_price_containment
    )


def no_containment_cell_fires(contract: AbnormalFlowContract, f: DecisionFeatures) -> bool:
    if f.oi_growth_pct is None or f.buy_pressure is None:
        return False
    if contract.min_oi_growth_pct is None or contract.min_buy_pressure_ratio is None:
        return False
    return (
        f.oi_growth_pct >= contract.min_oi_growth_pct
        and f.buy_pressure >= contract.min_buy_pressure_ratio
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
    contract: AbnormalFlowContract,
    exchange: str,
    entry_price: float | None,
    exit_price: float | None,
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
    for name in ("taker_fee_bps", "entry_slippage_bps", "exit_slippage_bps"):
        if not getattr(contract, name):
            raise ValueError(f"cost model incomplete: {name} is not set")

    if exchange == "binance":
        funding_bps = contract.funding_bps_720m_binance
    elif exchange == "bybit":
        funding_bps = contract.funding_bps_720m_bybit
    else:
        raise ValueError(f"funding model incomplete: unknown exchange {exchange}")

    if not _finite(funding_bps):
        raise ValueError(f"funding model incomplete for exchange {exchange}")

    gross = (exit_price - entry_price) / entry_price  # long
    entry_slippage = cast("float", contract.entry_slippage_bps)
    exit_slippage = cast("float", contract.exit_slippage_bps)
    fee = cast("float", contract.taker_fee_bps)

    # 10 bps on entry, 10 bps on exit = 2.0 * fee
    cost_bps = entry_slippage + exit_slippage + 2.0 * fee + cast("float", funding_bps)
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
    no_oi_fires: int = 0
    no_buy_fires: int = 0
    no_containment_fires: int = 0
    p99_fires: int = 0
    sub_p99_fires: int = 0
    primary_episodes: int = 0
    no_oi_episodes: int = 0
    no_buy_episodes: int = 0
    no_containment_episodes: int = 0
    p99_episodes: int = 0
    sub_p99_episodes: int = 0
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
    no_oi: list[DecisionFeatures] = []
    no_buy: list[DecisionFeatures] = []
    no_containment: list[DecisionFeatures] = []
    p99_strata: list[DecisionFeatures] = []
    sub_p99_strata: list[DecisionFeatures] = []
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
        if no_oi_cell_fires(contract, d):
            funnel.no_oi_fires += 1
            no_oi.append(d)
        if no_buy_cell_fires(contract, d):
            funnel.no_buy_fires += 1
            no_buy.append(d)
        if no_containment_cell_fires(contract, d):
            funnel.no_containment_fires += 1
            no_containment.append(d)
    cooldown = contract.cooldown_minutes
    funnel.primary_episodes = len(form_episodes(primary, cooldown))
    funnel.no_oi_episodes = len(form_episodes(no_oi, cooldown))
    funnel.no_buy_episodes = len(form_episodes(no_buy, cooldown))
    funnel.no_containment_episodes = len(form_episodes(no_containment, cooldown))
    funnel.p99_episodes = len(form_episodes(p99_strata, cooldown))
    funnel.sub_p99_episodes = len(form_episodes(sub_p99_strata, cooldown))
    return funnel


# --- Economics, portfolio, verdict (RETURNS-BEARING; pure over resolved episodes) ---


@dataclass(frozen=True)
class EpisodeRecord:
    """One resolved episode's returns-bearing summary, used for the aggregate report.
    Produced only inside the frozen-gated run."""

    route_key: RouteKey
    canonical_asset: str
    iso_week: str
    decision_at: datetime
    net_return: float
    excess: float | None  # None when the episode had no resolved control


@dataclass
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
    distinct_assets: int
    n_weeks: int
    max_episodes_per_asset_frac: float | None
    max_episodes_per_week_frac: float | None
    control_coverage_frac: float | None
    mean_net_return: float | None
    weekly_clustered_se: float | None
    lower_95ci_net_return: float | None
    mean_excess_over_control: float | None
    weekly_clustered_se_excess: float | None
    lower_95ci_excess_over_control: float | None
    leave_one_out_net_min: float | None
    leave_one_out_net_max: float | None
    portfolio: PortfolioResult
    verdict: str


def select_portfolio(
    contract: AbnormalFlowContract, decisions: Sequence[DecisionFeatures]
) -> tuple[list[DecisionFeatures], int]:
    slots = contract.portfolio_max_slots or 1
    horizon = timedelta(minutes=contract.outcome_horizon_minutes)
    active_ends: list[datetime] = []
    taken: list[DecisionFeatures] = []
    skipped = 0

    # Entry is at decision_at + 1m
    # Sort by (entry_at, full route_key)
    def sort_key(d: DecisionFeatures) -> tuple[datetime, RouteKey]:
        return (d.decision_at + timedelta(minutes=1), d.route_key())

    sorted_decisions = sorted(decisions, key=sort_key)
    for d in sorted_decisions:
        entry_at = d.decision_at + timedelta(minutes=1)
        active_ends = [t for t in active_ends if t > entry_at]
        if len(active_ends) >= slots:
            skipped += 1
            continue
        active_ends.append(entry_at + horizon)
        taken.append(d)
    return taken, skipped


def simulate_portfolio(
    contract: AbnormalFlowContract, selected: list[tuple[DecisionFeatures, float | None]]
) -> tuple[PortfolioResult, bool]:
    from datetime import timedelta

    bank = float(contract.portfolio_bank_usd or 0.0)
    position = float(contract.position_usd or 0.0)
    slots = int(contract.portfolio_max_slots or 1)

    current_capital = bank
    active_positions: list[datetime] = []

    taken = 0
    skipped = 0
    max_concurrency = 0
    cum_pnl = 0.0
    peak = bank
    max_dd = 0.0
    streak = 0
    longest = 0

    unresolved_in_portfolio = False

    for feat, pnl in sorted(selected, key=lambda x: x[0].decision_at):
        decision_time = feat.decision_at

        still_active = [ext for ext in active_positions if ext > decision_time]
        active_positions = still_active

        if len(active_positions) >= slots:
            skipped += 1
            continue

        actual_position = min(position, current_capital)
        if actual_position <= 0:
            skipped += 1
            continue

        taken += 1
        active_positions.append(decision_time + timedelta(minutes=contract.outcome_horizon_minutes))
        max_concurrency = max(max_concurrency, len(active_positions))

        if pnl is None:
            unresolved_in_portfolio = True
        else:
            # pnl is the net_return (percentage/fraction), we need actual USD
            actual_pnl_usd = actual_position * pnl
            cum_pnl += actual_pnl_usd
            current_capital += actual_pnl_usd

            peak = max(peak, current_capital)
            max_dd = max(max_dd, peak - current_capital)
            if pnl < 0:
                streak += 1
                longest = max(longest, streak)
            else:
                streak = 0

    return PortfolioResult(
        taken_trades=taken,
        skipped_capacity=skipped,
        total_pnl_usd=cum_pnl,
        max_drawdown_usd=max_dd,
        longest_losing_streak=longest,
        max_concurrency=max_concurrency,
    ), unresolved_in_portfolio


def _clustered_se(values: Sequence[tuple[str, float]]) -> tuple[int, float | None]:
    """Cluster-robust standard error for the pooled mean.
    Treats the string as the cluster ID (e.g., ISO week)."""
    import math
    from collections import defaultdict

    if not values:
        return 0, None

    total_obs = len(values)
    global_mean = sum(v for _, v in values) / total_obs

    by_cluster: dict[str, list[float]] = defaultdict(list)
    for c_id, v in values:
        by_cluster[c_id].append(v)

    num_clusters = len(by_cluster)
    if num_clusters < 2:
        return num_clusters, None

    # Variance of the mean = (1 / N^2) * (C / (C-1)) * sum_c (sum_i_in_c (y_i - global_mean))^2
    sum_sq_cluster_errors = 0.0
    for obs in by_cluster.values():
        cluster_error = sum(y - global_mean for y in obs)
        sum_sq_cluster_errors += cluster_error**2

    var = (num_clusters / (num_clusters - 1)) * sum_sq_cluster_errors / (total_obs**2)
    return num_clusters, math.sqrt(var)


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


def _leave_one_out_net(
    records: Sequence[EpisodeRecord],
) -> tuple[float | None, float | None]:
    """Min and max mean net when each canonical asset is dropped in turn, so a single
    asset cannot carry the net. ``None`` when fewer than two assets have a resolved
    net return."""
    with_net = [(r.canonical_asset, r.net_return) for r in records if r.net_return is not None]
    assets = {a for a, _ in with_net}
    if len(assets) < 2:
        return None, None
    means: list[float] = []
    for drop in assets:
        kept = [e for a, e in with_net if a != drop]
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
    distinct_assets: int,
    n_weeks: int,
    max_episodes_per_asset_frac: float | None,
    max_episodes_per_week_frac: float | None,
    control_coverage_frac: float | None,
    mean_net_return: float | None,
    lower_95ci_net_return: float | None,
    mean_excess_over_control: float | None,
    lower_95ci_excess_over_control: float | None,
    leave_one_out_net_min: float | None,
    portfolio_pnl: float | None,
    portfolio_ending_bank: float | None,
    unresolved_in_portfolio: bool = False,
) -> str:
    if unresolved_in_portfolio:
        return "INSUFFICIENT_EVIDENCE"
    # Gate 1: maturity
    if contract.min_resolved_episodes is None or resolved_episodes < contract.min_resolved_episodes:
        return "INSUFFICIENT_EVIDENCE"

    # Gate 2: mature negative economics (before missingness)
    if mean_net_return is None or mean_net_return <= 0:
        return "FAIL"

    # Gate 3: evidence quality
    total = resolved_episodes + unresolved_episodes
    missing = (unresolved_episodes / total) if total else None
    if missing is not None and missing > (contract.max_missing_fraction or 1.0):
        return "INSUFFICIENT_EVIDENCE"
    if contract.min_distinct_assets is not None and distinct_assets < contract.min_distinct_assets:
        return "INSUFFICIENT_EVIDENCE"
    if contract.min_utc_weeks is not None and n_weeks < contract.min_utc_weeks:
        return "INSUFFICIENT_EVIDENCE"
    if (
        contract.max_episodes_per_asset_frac is not None
        and max_episodes_per_asset_frac is not None
        and max_episodes_per_asset_frac > contract.max_episodes_per_asset_frac
    ):
        return "INSUFFICIENT_EVIDENCE"
    if (
        contract.max_episodes_per_week_frac is not None
        and max_episodes_per_week_frac is not None
        and max_episodes_per_week_frac > contract.max_episodes_per_week_frac
    ):
        return "INSUFFICIENT_EVIDENCE"
    if (
        contract.min_control_coverage_frac is not None
        and control_coverage_frac is not None
        and control_coverage_frac < contract.min_control_coverage_frac
    ):
        return "INSUFFICIENT_EVIDENCE"

    # Gate 4: statistical evidence
    if lower_95ci_net_return is None or lower_95ci_net_return <= 0:
        return "INSUFFICIENT_EVIDENCE"
    floor = (contract.min_excess_over_control_pct or 0.0) / 100.0
    if mean_excess_over_control is None or mean_excess_over_control < floor:
        return "FAIL"
    if lower_95ci_excess_over_control is None or lower_95ci_excess_over_control <= 0:
        return "INSUFFICIENT_EVIDENCE"
    if leave_one_out_net_min is None or leave_one_out_net_min <= 0:
        return "INSUFFICIENT_EVIDENCE"

    # Gate 5: portfolio
    if portfolio_pnl is None or portfolio_pnl <= 0:
        return "FAIL"
    if (
        contract.portfolio_bank_usd is not None
        and portfolio_ending_bank is not None
        and portfolio_ending_bank <= contract.portfolio_bank_usd
    ):
        return "FAIL"

    return "PASS_DISCOVERY"


def build_report(
    contract: AbnormalFlowContract,
    records: Sequence[EpisodeRecord],
    *,
    unresolved_episodes: int,
    unresolved_decision_times: Sequence[datetime] = (),
    resolved_controls: int = 0,
    requested_controls: int = 0,
    skipped_portfolio_capacity: int = 0,
    selected_episodes: list[DecisionFeatures] | None = None,
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

    n_weeks, se = _clustered_se([(r.iso_week, r.net_return) for r in records])
    n_excess_weeks, se_excess = _clustered_se(
        [(r.iso_week, r.excess) for r in records if r.excess is not None]
    )

    def _critical_value(rule: str | None, clusters: int) -> float:
        if not rule or rule == "normal_1_96_v1":
            return 1.96
        if rule == "student_t_df_weeks_minus_one_v1":
            if clusters < 2:
                # If we have less than 2 clusters, t is not well-defined, fall back very wide
                return 12.706
            df = clusters - 1
            # Standard t-distribution critical values for 95% two-sided (or 97.5% one-sided)
            t_table = {
                1: 12.706,
                2: 4.303,
                3: 3.182,
                4: 2.776,
                5: 2.571,
                6: 2.447,
                7: 2.365,
                8: 2.306,
                9: 2.262,
                10: 2.228,
                11: 2.201,
                12: 2.179,
                13: 2.160,
                14: 2.145,
                15: 2.131,
                16: 2.120,
                17: 2.110,
                18: 2.101,
                19: 2.093,
                20: 2.086,
                21: 2.080,
                22: 2.074,
                23: 2.069,
                24: 2.064,
                25: 2.060,
                26: 2.056,
                27: 2.052,
                28: 2.048,
                29: 2.045,
                30: 2.042,
            }
            if df in t_table:
                return t_table[df]
            return 1.96 + 2.4 / df
        return 1.96

    cv_net = _critical_value(contract.inference_rule, n_weeks)
    lower_95_net = None
    if mean_net is not None and se is not None:
        lower_95_net = mean_net - cv_net * se

    cv_excess = _critical_value(contract.inference_rule, n_excess_weeks)
    lower_95_excess = None
    if mean_excess is not None and se_excess is not None:
        lower_95_excess = mean_excess - cv_excess * se_excess

    loo_net_min, loo_net_max = _leave_one_out_net(records)

    assets = {r.canonical_asset for r in records}
    distinct_assets = len(assets)

    from collections import Counter

    asset_counts = Counter(r.canonical_asset for r in records)
    week_counts = Counter(r.iso_week for r in records)

    max_episodes_per_asset_frac = (max(asset_counts.values()) / resolved) if resolved else None
    max_episodes_per_week_frac = (max(week_counts.values()) / resolved) if resolved else None
    control_coverage_frac = (resolved_controls / requested_controls) if requested_controls else None

    selected_pnls: list[tuple[DecisionFeatures, float | None]] = []
    unresolved_in_portfolio = False
    if selected_episodes is not None:
        record_map = {r.route_key: r.net_return for r in records}
        for d in selected_episodes:
            r_net = record_map.get(d.route_key())
            if r_net is not None:
                selected_pnls.append((d, r_net))
            else:
                selected_pnls.append((d, None))

    portfolio, unresolved_in_portfolio = simulate_portfolio(contract, selected_pnls)
    portfolio = PortfolioResult(
        taken_trades=portfolio.taken_trades,
        skipped_capacity=skipped_portfolio_capacity,
        total_pnl_usd=portfolio.total_pnl_usd,
        max_drawdown_usd=portfolio.max_drawdown_usd,
        longest_losing_streak=portfolio.longest_losing_streak,
        max_concurrency=portfolio.max_concurrency,
    )

    portfolio_ending_bank = None
    if contract.portfolio_bank_usd is not None:
        portfolio_ending_bank = float(contract.portfolio_bank_usd) + portfolio.total_pnl_usd

    verdict = render_verdict(
        contract,
        resolved_episodes=resolved,
        unresolved_episodes=unresolved_episodes,
        distinct_assets=distinct_assets,
        n_weeks=n_weeks,
        max_episodes_per_asset_frac=max_episodes_per_asset_frac,
        max_episodes_per_week_frac=max_episodes_per_week_frac,
        control_coverage_frac=control_coverage_frac,
        mean_net_return=mean_net,
        lower_95ci_net_return=lower_95_net,
        mean_excess_over_control=mean_excess,
        lower_95ci_excess_over_control=lower_95_excess,
        leave_one_out_net_min=loo_net_min,
        portfolio_pnl=portfolio.total_pnl_usd,
        portfolio_ending_bank=portfolio_ending_bank,
        unresolved_in_portfolio=unresolved_in_portfolio,
    )
    return EconomicsReport(
        resolved_episodes=resolved,
        unresolved_episodes=unresolved_episodes,
        missing_fraction=missing,
        distinct_assets=distinct_assets,
        n_weeks=n_weeks,
        max_episodes_per_asset_frac=max_episodes_per_asset_frac,
        max_episodes_per_week_frac=max_episodes_per_week_frac,
        control_coverage_frac=control_coverage_frac,
        mean_net_return=mean_net,
        weekly_clustered_se=se,
        lower_95ci_net_return=lower_95_net,
        mean_excess_over_control=mean_excess,
        weekly_clustered_se_excess=se_excess,
        lower_95ci_excess_over_control=lower_95_excess,
        leave_one_out_net_min=loo_net_min,
        leave_one_out_net_max=loo_net_max,
        portfolio=portfolio,
        verdict=verdict,
    )


# --- Formal run (RETURNS-BEARING; require_frozen fail-closed) -----------------------


@dataclass(frozen=True)
class EvaluationManifest:
    input_audit_fingerprint: str
    identity_snapshot_hash: str
    candidate_table_version: str
    funding_snapshot_hash: str
    funding_settlements_hash: str

    def compute_fingerprint(self) -> str:
        import hashlib
        import json
        from dataclasses import asdict

        d = asdict(self)
        encoded = json.dumps(d, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


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
        evaluation_manifest: EvaluationManifest,
        registered_contract_path: str = "",
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
        if registered_contract_path:
            from pathlib import Path

            content = Path(registered_contract_path).read_bytes()

            import json

            from .abnormal_flow_screen import AbnormalFlowContract

            d = json.loads(content)
            d.pop("contract_hash", None)
            disk_contract = AbnormalFlowContract(**d)
            disk_hash = disk_contract.compute_hash()
            if self._contract.compute_hash() != disk_hash:
                raise ValueError(
                    f"Contract hash mismatch: registered file "
                    f"{registered_contract_path} hashes to {disk_hash}, "
                    f"but in-memory contract hashes to {self._contract.compute_hash()}"
                )
        else:
            raise ValueError("registered_contract_path must be provided")
        contract = self._contract

        # Bind the freeze to the data actually being scored: the loader's reproduced
        # fingerprint must match the pinned one, and no decision may fall outside the
        # registered window. Either mismatch refuses the run instead of reading returns
        # against a dataset the contract never pinned.
        observed_fingerprint = evaluation_manifest.compute_fingerprint()
        if observed_fingerprint != contract.input_fingerprint:
            raise FreezeMismatchError(
                "observed evaluation fingerprint does not match the frozen contract; "
                f"expected {contract.input_fingerprint!r}, got {observed_fingerprint!r}"
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

        # Portfolio selection on DecisionFeatures, BEFORE read_outcomes
        selected_episodes, skipped_portfolio_capacity = select_portfolio(contract, episodes)

        # Select controls BEFORE reading returns, then request outcomes for both groups
        # in one read keyed by the exact native route. A reader that returns only what
        # it was asked for is enough; the excess is no longer hidden by over-returning.
        controls_by_episode: dict[RouteKey, list[DecisionFeatures]] = {}
        controls_per_episode = (contract.controls_per_episode or 0) or 1
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
            return proxy_net_return(
                contract, outcome.exchange, outcome.entry_price, outcome.exit_price
            )

        net_returns: list[float] = []
        excesses: list[float] = []
        control_means: list[float] = []
        records: list[EpisodeRecord] = []
        unresolved_episodes = 0
        unresolved_decision_times: list[datetime] = []
        episodes_with_matched_control = 0
        resolved_controls = 0
        unresolved_controls = 0
        # Process controls completely independently from primary resolution
        requested_controls = len(episodes) * (contract.controls_per_episode or 0)
        resolved_controls_by_ep: dict[RouteKey, list[float]] = {}
        for ep in episodes:
            ctrls = controls_by_episode.get(ep.route_key(), [])
            c_returns = []
            for c in ctrls:
                cr = _return_for(c)
                if cr is None:
                    unresolved_controls += 1
                else:
                    resolved_controls += 1
                    c_returns.append(cr)
            resolved_controls_by_ep[ep.route_key()] = c_returns

        for ep in episodes:
            r = _return_for(ep)
            if r is None:
                unresolved_episodes += 1
                unresolved_decision_times.append(ep.decision_at)
                continue

            net_returns.append(r)
            control_rs = resolved_controls_by_ep.get(ep.route_key(), [])

            excess: float | None = None
            if control_rs:
                episodes_with_matched_control += 1
                control_mean = sum(control_rs) / len(control_rs)
                control_means.append(control_mean)
                excess = r - control_mean
                excesses.append(excess)

            records.append(
                EpisodeRecord(
                    route_key=ep.route_key(),
                    canonical_asset=ep.canonical_asset,
                    iso_week=ep.iso_week,
                    decision_at=ep.decision_at,
                    net_return=r,
                    excess=excess,
                )
            )
        report = build_report(
            contract,
            records,
            unresolved_episodes=unresolved_episodes,
            unresolved_decision_times=unresolved_decision_times,
            resolved_controls=resolved_controls,
            requested_controls=requested_controls,
            skipped_portfolio_capacity=skipped_portfolio_capacity,
            selected_episodes=selected_episodes,
        )
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
SELECT exchange, market_type, symbol, capture_version, \
       bucket_start, open_price, close_price, high_price, low_price, price_complete
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


def iter_instrument_bars(
    paths: Sequence[str], *, window_start: datetime, window_end: datetime
) -> Iterator[list[MinuteBar]]:
    """Stream outcome-blind minute bars grouped by native route + capture_version, one
    instrument at a time, from the given Parquet file(s). DuckDB does the ordered scan
    (out-of-core if needed) and Python holds only the current instrument's bars, so a
    full month never materializes as one list. Reads no forward price."""
    import duckdb

    connection = duckdb.connect()
    try:
        cursor = connection.execute(_INPUT_COLUMNS_SQL, [list(paths), window_start, window_end])
        current: tuple[str, str, str, str] | None = None
        buffer: list[MinuteBar] = []
        while True:
            rows = cursor.fetchmany(20_000)
            if not rows:
                break
            for row in rows:
                bar = _row_to_bar(row)
                key = (bar.exchange, bar.market_type, bar.native_market_id, bar.capture_version)
                if key != current:
                    if buffer:
                        yield buffer
                    buffer = []
                    current = key
                buffer.append(bar)
        if buffer:
            yield buffer
    finally:
        connection.close()


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
    path: str,
    *,
    outcome_horizon_minutes: int,
) -> Callable[[Sequence[DecisionFeatures]], dict[RouteKey, Outcome]]:
    """Build a returns-bearing reader over one Parquet: entry is the priced-proxy open
    of the decision minute's bar, exit the close of the bar ``outcome_horizon_minutes``
    later, on the exact native route. Missing legs stay unresolved (never filled in)."""
    from collections import defaultdict

    import duckdb

    def reader(requested: Sequence[DecisionFeatures]) -> dict[RouteKey, Outcome]:
        if not requested:
            return {}
        starts = [d.decision_at for d in requested]
        max_horizon = outcome_horizon_minutes
        max_horizon_td = timedelta(minutes=max_horizon)
        lo = min(starts)
        hi = max(starts) + max_horizon_td + timedelta(minutes=2)
        connection = duckdb.connect()
        try:
            rows = connection.execute(_OUTCOME_COLUMNS_SQL, [path, lo, hi]).fetchall()
        finally:
            connection.close()

        # Group by route
        bars: dict[
            tuple[str, str, str, str], dict[datetime, tuple[float, float, float, float, bool]]
        ] = defaultdict(dict)
        for row in rows:
            route = (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
            bars[route][row[4]] = (row[5], row[6], row[7], row[8], row[9])

        out: dict[RouteKey, Outcome] = {}
        for d in requested:
            route = (d.exchange, d.market_type, d.native_market_id, d.capture_version)
            route_bars = bars.get(route, {})

            entry_price = None
            exit_price = None

            # primary path continuity check
            # Entry is the next executable bar after decision
            entry_t = d.decision_at + timedelta(minutes=1)
            primary_bars = []
            continuous = True
            for i in range(outcome_horizon_minutes + 1):
                t = entry_t + timedelta(minutes=i)
                b = route_bars.get(t)
                # require b is not None and price_complete is True
                if b is None or not b[4]:
                    continuous = False
                    break
                primary_bars.append(b)

            if continuous and primary_bars:
                entry_price = primary_bars[0][0]  # open of first bar (entry)
                exit_price = primary_bars[-1][1]  # close of last bar (horizon)

            out[d.route_key()] = Outcome(
                exchange=d.exchange,
                market_type=d.market_type,
                native_market_id=d.native_market_id,
                capture_version=d.capture_version,
                symbol=d.symbol,
                decision_at=d.decision_at,
                entry_price=entry_price,
                exit_price=exit_price,
            )
        return out

    return reader
