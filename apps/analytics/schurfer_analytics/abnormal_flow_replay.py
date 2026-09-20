"""Outcome-blind replay engine for the abnormal-flow economic screen.

PRE-REGISTRATION INVARIANT. Reading forward returns is gated behind a fully frozen
contract AND the exact frozen dataset: :class:`FormalReplay.run` calls
``contract.require_frozen()``, then refuses unless the loader's observed input
fingerprint equals the pinned one and every decision falls inside the registered UTC
window, all before any outcome reader is invoked. Scanning inputs, computing the
pre-decision features, applying eligibility and the primary/ablation cells, forming
episodes, and selecting matched controls are all outcome-blind and never touch a
forward price. This module therefore cannot tune a threshold to, score a window it
did not pin, or peek at the returns it will later score.

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

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .abnormal_flow_screen import (
    LOOKBACK_MINUTES,
    AbnormalFlowContract,
    buy_pressure_ratio_60m,
    oi_growth_pct_60m,
    price_containment_max_bar_dev,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

REPLAY_VERSION = "abnormal_flow_replay_v1"

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


# A candidate's exact native route + decision instant. Includes the exchange, market
# type and native market id so a Bybit and a Binance row that happen to share a symbol
# and minute are never conflated into one outcome.
RouteKey = tuple[str, str, str, datetime]


@dataclass(frozen=True)
class DecisionFeatures:
    """Everything known at (and only at) the decision instant. Contains NO forward
    price: it is the outcome-blind half of a candidate. ``decision_price`` is the last
    pre-decision close (used for the Binance OI->USD conversion and nothing forward)."""

    exchange: str
    market_type: str
    native_market_id: str
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
        return (self.exchange, self.market_type, self.native_market_id, self.decision_at)


@dataclass(frozen=True)
class Outcome:
    """The returns-bearing half of a candidate. Read ONLY by the frozen-gated formal
    run: entry is the next-bar open priced proxy, exit the horizon-bar close proxy."""

    exchange: str
    market_type: str
    native_market_id: str
    symbol: str
    decision_at: datetime
    entry_price: float | None
    exit_price: float | None

    def route_key(self) -> RouteKey:
        return (self.exchange, self.market_type, self.native_market_id, self.decision_at)


# --- Outcome-blind minute bar + feature assembly -----------------------------------


@dataclass(frozen=True)
class MinuteBar:
    """One outcome-blind 1m bar for a single instrument. Only fields available at or
    before the bar's finalization are carried; no forward price appears here."""

    exchange: str
    market_type: str
    native_market_id: str
    symbol: str
    canonical_asset: str
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


def _unavailable_decision(end: MinuteBar, decision_at: datetime, reason: str) -> DecisionFeatures:
    """A decision whose feature window could not be used, with the rejection reason
    recorded (never a fabricated feature). Carries only outcome-blind identity."""
    return DecisionFeatures(
        exchange=end.exchange,
        market_type=end.market_type,
        native_market_id=end.native_market_id,
        symbol=end.symbol,
        canonical_asset=end.canonical_asset,
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
) -> list[DecisionFeatures]:
    """Turn one instrument's ordered, outcome-blind minute bars into per-minute
    :class:`DecisionFeatures` using the frozen feature forms.

    The decision at bar ``i`` is timed AFTER that bar finalizes plus the registered
    scan lag; the 60m feature window is ``bars[i-60 .. i]``, which must be contiguous,
    fully complete, and finalized/observed at or before the decision instant. Both the
    window-start and window-end OI observations must be no older than
    ``oi_freshness_limit_seconds`` before their bar (not just "not in the future"), and
    every bar's last trade must have been received by the decision instant. A window
    that is short, gapped, incomplete, not finalized, resting on stale OI, or missing a
    timely trade yields a decision marked ``unavailable_reason`` rather than a
    fabricated feature. No forward bar is read."""
    ordered = sorted(bars, key=lambda b: b.bucket_start)
    out: list[DecisionFeatures] = []
    window = LOOKBACK_MINUTES
    freshness = timedelta(seconds=oi_freshness_limit_seconds)
    for i in range(window, len(ordered)):
        end = ordered[i]
        start = ordered[i - window]
        decision_at = end.bucket_start + timedelta(minutes=1 + scan_lag_minutes)
        span = ordered[i - window : i + 1]

        contiguous = all(
            (span[j].bucket_start - span[j - 1].bucket_start) == timedelta(minutes=1)
            for j in range(1, len(span))
        )
        if not contiguous:
            out.append(_unavailable_decision(end, decision_at, "lookback_gap"))
            continue
        if any(
            not (b.price_complete and b.trades_complete and b.open_interest_complete) for b in span
        ):
            out.append(_unavailable_decision(end, decision_at, "incomplete_lookback"))
            continue
        if any(b.created_at > decision_at for b in span):
            out.append(_unavailable_decision(end, decision_at, "not_finalized_by_decision"))
            continue
        if any(
            b.last_trade_received_at is None or b.last_trade_received_at > decision_at for b in span
        ):
            out.append(_unavailable_decision(end, decision_at, "late_or_missing_trades"))
            continue
        if not _oi_fresh(start, freshness, decision_at) or not _oi_fresh(
            end, freshness, decision_at
        ):
            out.append(_unavailable_decision(end, decision_at, "stale_oi"))
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
                symbol=end.symbol,
                canonical_asset=end.canonical_asset,
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
            if control_rs:
                episodes_with_matched_control += 1
                control_mean = sum(control_rs) / len(control_rs)
                control_means.append(control_mean)
                excesses.append(r - control_mean)

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
        )
