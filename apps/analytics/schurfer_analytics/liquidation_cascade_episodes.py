"""Cascade episode declustering for analysis/liquidation-cascade-validation-v2.

Groups consecutive 1-minute price/OI-drop observations for one (exchange,
symbol) into cascade episodes, so a single multi-minute cascade counts as one
independent observation instead of one per triggering minute. This is the
same class of bug momentum_flow_bidirectional_burst_study.py's own
`decluster_episodes` already fixed for volume bursts (see that module's own
doc comment: "a single 14-minute burst run for one symbol counted as 14
independent observations"). The recovered `feature/alpha-research` grid
search (`583213f`, never merged) had exactly this bug for liquidation
cascades: every qualifying minute was scored as its own independent trade.

Unlike the burst declustering (a flat refractory period only), this adds the
recovery-or-cooldown rule: an episode also ends the moment price AND OI both
recover back above their own recovery thresholds, even if that happens
before `cooldown_minutes` has elapsed -- otherwise a full recovery followed
by a genuinely new, unrelated cascade within the cooldown window would
wrongly merge into one episode. Recovery can only be confirmed from a minute
with resolved price/OI drop values; an incomplete or missing bar never
counts as evidence of recovery, only elapsed cooldown time does.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class MinuteState:
    """One minute's causal price/OI-drop state for one (exchange, symbol).

    `price_drop_pct`/`oi_drop_pct` are the same 15-minute-lookback ratios
    `liquidation_cascade_repository.py` computes (negative == a drop; must
    track apps/execution/schurfer_execution/liquidation_cascade.py's own
    `_SQL_SCANNER` thresholds -- see that repository module's own comment).
    Either is None when the 15-minute-ago lag value or the current bar
    itself is unresolved -- never coerced to 0.0, since a missing lag is not
    the same claim as "no drop happened".
    """

    exchange: str
    symbol: str
    bucket_start: datetime
    price_drop_pct: float | None
    oi_drop_pct: float | None
    is_qualifying: bool
    price_complete: bool
    open_interest_complete: bool

    def __post_init__(self) -> None:
        if self.is_qualifying and (self.price_drop_pct is None or self.oi_drop_pct is None):
            raise ValueError("a qualifying minute must have resolved price/OI drops")


@dataclass(frozen=True)
class CascadeEpisode:
    episode_id: int
    exchange: str
    symbol: str
    trigger_at: datetime
    last_trigger_at: datetime
    peak_price_drop_pct: float
    peak_oi_drop_pct: float
    trigger_minutes: int
    # True when any qualifying minute in this episode had an incomplete
    # price/OI bar -- the episode is kept (never dropped), but callers must
    # exclude it from "fillable" denominators and count it as unresolved
    # instead, per this PR's own "missing OI stays unresolved" rule.
    data_quality_unresolved: bool

    def __post_init__(self) -> None:
        if self.trigger_minutes < 1:
            raise ValueError("trigger_minutes must be at least 1")
        if self.last_trigger_at < self.trigger_at:
            raise ValueError("last_trigger_at must not precede trigger_at")
        if self.peak_price_drop_pct > 0 or self.peak_oi_drop_pct > 0:
            raise ValueError("peak drops must be non-positive")

    @property
    def cluster_key(self) -> str:
        return self.symbol

    @property
    def week_key(self) -> str:
        iso = self.trigger_at.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"


def _recovered(minute: MinuteState, *, recovery_price_pct: float, recovery_oi_pct: float) -> bool:
    if minute.price_drop_pct is None or minute.oi_drop_pct is None:
        return False
    return minute.price_drop_pct > recovery_price_pct and minute.oi_drop_pct > recovery_oi_pct


def decluster_cascade_episodes(
    minutes: Sequence[MinuteState],
    *,
    recovery_price_pct: float,
    recovery_oi_pct: float,
    cooldown_minutes: int,
    start_id: int = 1,
) -> tuple[CascadeEpisode, ...]:
    """`minutes` must cover every minute in the analysis window for each
    (exchange, symbol) that has a resolvable price/OI-drop state -- not only
    the qualifying ones -- so recovery can be evaluated from the minutes in
    between two qualifying rows. A minute simply absent from `minutes`
    (missing bar) contributes no recovery evidence but still lets cooldown
    time elapse, since cooldown is measured on `bucket_start`, not on row
    position.

    `recovery_price_pct`/`recovery_oi_pct` must be shallower (closer to
    zero) than the causal trigger thresholds -- they describe how far price/
    OI must climb back before a cascade is considered over, not a second
    copy of the trigger rule.

    `start_id` lets a caller combining episodes from more than one call keep
    `episode_id` globally unique, the same reason
    `momentum_flow_bidirectional_burst_study.decluster_episodes` exposes it.
    """
    if recovery_price_pct <= 0 or recovery_oi_pct <= 0:
        raise ValueError("recovery thresholds must be positive (shallower than the trigger drop)")
    if cooldown_minutes <= 0:
        raise ValueError("cooldown_minutes must be positive")
    if start_id <= 0:
        raise ValueError("start_id must be positive")

    by_instrument: dict[tuple[str, str], list[MinuteState]] = defaultdict(list)
    for minute in minutes:
        by_instrument[(minute.exchange, minute.symbol)].append(minute)

    cooldown = timedelta(minutes=cooldown_minutes)
    episodes: list[CascadeEpisode] = []
    next_id = start_id

    for (exchange, symbol), instrument_minutes in by_instrument.items():
        ordered = sorted(instrument_minutes, key=lambda m: m.bucket_start)
        trigger_at: datetime | None = None
        last_trigger_at: datetime | None = None
        peak_price = 0.0
        peak_oi = 0.0
        trigger_count = 0
        unresolved = False

        for minute in ordered:
            if trigger_at is not None and last_trigger_at is not None:
                closed_by_cooldown = minute.bucket_start - last_trigger_at >= cooldown
                closed_by_recovery = not minute.is_qualifying and _recovered(
                    minute,
                    recovery_price_pct=recovery_price_pct,
                    recovery_oi_pct=recovery_oi_pct,
                )
                if closed_by_cooldown or closed_by_recovery:
                    episodes.append(
                        CascadeEpisode(
                            episode_id=next_id,
                            exchange=exchange,
                            symbol=symbol,
                            trigger_at=trigger_at,
                            last_trigger_at=last_trigger_at,
                            peak_price_drop_pct=peak_price,
                            peak_oi_drop_pct=peak_oi,
                            trigger_minutes=trigger_count,
                            data_quality_unresolved=unresolved,
                        )
                    )
                    next_id += 1
                    trigger_at = None

            if minute.is_qualifying:
                assert minute.price_drop_pct is not None
                assert minute.oi_drop_pct is not None
                if trigger_at is None:
                    trigger_at = minute.bucket_start
                    peak_price = minute.price_drop_pct
                    peak_oi = minute.oi_drop_pct
                    trigger_count = 0
                    unresolved = False
                peak_price = min(peak_price, minute.price_drop_pct)
                peak_oi = min(peak_oi, minute.oi_drop_pct)
                trigger_count += 1
                last_trigger_at = minute.bucket_start
                if not (minute.price_complete and minute.open_interest_complete):
                    unresolved = True

        if trigger_at is not None:
            assert last_trigger_at is not None
            episodes.append(
                CascadeEpisode(
                    episode_id=next_id,
                    exchange=exchange,
                    symbol=symbol,
                    trigger_at=trigger_at,
                    last_trigger_at=last_trigger_at,
                    peak_price_drop_pct=peak_price,
                    peak_oi_drop_pct=peak_oi,
                    trigger_minutes=trigger_count,
                    data_quality_unresolved=unresolved,
                )
            )
            next_id += 1

    return tuple(sorted(episodes, key=lambda e: (e.exchange, e.symbol, e.trigger_at)))
