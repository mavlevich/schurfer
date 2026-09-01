"""Pure forward-outcome resolution for ROADMAP item 8
(`research/serial-pump-regimes-v1`): what to do after a pump on a given
asset -- hold or sell -- using every independent pump regime, not only
ones that went on to "win".

Discovery-only, no verdict (explicit user decision, 2026-08-31): this
module answers "what happened" across the full historical population,
never "is this an edge" -- there is no formal checkpoint, evidence floor,
or promotion rule here, unlike this codebase's registered prospective
contracts (e.g. `source_lead_forward_cohort.py`). Any future confirmation
step runs on a new, untouched forward cutoff, never on the historical
window this module reads.

Reuses `pump_recurrence_integrity_report.py`'s own `Episode`/`Regime`/
`merge_episodes_into_regimes` for recurrence identification rather than
reimplementing episode-fragmentation merging -- that logic already went
through colleague review (overlapping/nested-episode handling) and is the
exact "recurrence count and inter-episode intervals" mechanism this item
needs. Nothing here duplicates it; `recurrence_summary` below only
consumes its output.

Decision anchor: `regime.first_seen_at`, not `last_seen_at` (colleague
review, 2026-09-01). Item 8 asks "what to do after a FIRST pump" -- a
regime's own `last_seen_at` is a running maximum that a future episode can
still extend forward (merge_episodes_into_regimes merges any episode
starting within the cooldown of the regime's own running-max last_seen_at
into the SAME regime), so it is only knowable in hindsight, once the
cooldown has fully elapsed with no further episode -- exactly the
look-ahead item 8's own "never on the window already viewed here" line
warns against. `first_seen_at` is set once, from the regime's own first
episode, and never revised by a later merge (see
`merge_episodes_into_regimes`'s own loop: `first` is only reassigned when
a NEW regime starts) -- it is knowable the instant the first episode is
detected, with zero dependency on what happens afterward. The cooldown
itself is still used, unchanged, purely for the deduplication
`merge_episodes_into_regimes` already provides (collapsing detector-
flapping reopens into one regime identity for recurrence counting) --
never as part of the decision instant itself. A still-cooling-down regime
(its own `last_seen_at` within one cooldown of `evaluation_at`) is
reported as `regime_mature=False` by the I/O layer
(`serial_pump_regimes_report.py`'s own `RegimeRow`) -- its own
`episode_ids`/`last_seen_at`/`max_peak_pct`/recurrence stats could still
change on a later run if one more episode merges in, but its
`decision_at`/forward-outcome numbers below never do, since neither
depends on `last_seen_at`.

Everything in this module is pure (no DB, no network, no CCXT) given
already-fetched candle series -- the I/O half
(`serial_pump_regimes_report.py`) fetches `app.pump_events`/
`app.pump_event_sources` and forward OHLCV via the shared, cached
`ohlcv.fetch_symbol_candles`, and is a thin wiring layer around these
functions.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

from .ohlcv import ceil_to_timeframe, closed_candles, covers_window_without_gaps

if TYPE_CHECKING:
    from datetime import datetime

    from .ohlcv import Candle
    from .pump_recurrence_integrity_report import Regime

SERIAL_PUMP_REGIMES_VERSION = "serial_pump_regimes_v1"

# Same cooldown pump_recurrence_integrity_report.py already uses as its own
# primary metric ("24h" label in REGIME_COOLDOWNS) -- reused here, not a
# second, independently-chosen threshold that could quietly drift from it.
# See the module docstring: only used for regime dedup/maturity, never for
# the decision instant itself.
REGIME_COOLDOWN_MINUTES = 24 * 60

# label -> forward horizon in minutes, exactly the six item 8 names.
HORIZONS_MINUTES: tuple[tuple[str, int], ...] = (
    ("15m", 15),
    ("1h", 60),
    ("4h", 240),
    ("1d", 1_440),
    ("7d", 10_080),
    ("30d", 43_200),
)

# The candle at (or first after) the decision boundary contributes its own
# OPEN as the entry price, never its close: the close of that candle is
# only known once the candle itself finishes, i.e. one full timeframe AFTER
# the decision boundary -- using it as "the price we entered at" would be
# entering on a price not yet known at entry time (colleague review,
# 2026-09-01: this previously used the close, silently trimming e.g. a 15m
# horizon down to 10 minutes of genuinely forward-looking coverage). The
# open of a bar is set at the bar's own start instant, so it carries no
# such delay.
ENTRY_PRICE_SOURCE_VERSION = "ohlcv_open_proxy_v1"

UNRESOLVED_REASONS = frozenset(
    {
        "missing_decision_candle",
        "missing_btc_decision_candle",
        "insufficient_candle_history",
        "insufficient_btc_candle_history",
        "leading_candle_gap",
        "internal_candle_gap",
        "leading_btc_candle_gap",
        "internal_btc_candle_gap",
    }
)


@dataclass(frozen=True)
class HorizonOutcome:
    """One regime's own forward read at one horizon. resolved=False means
    exactly one of UNRESOLVED_REASONS -- never a fabricated number standing
    in for missing OHLCV history (e.g. the exchange stopped listing the
    instrument, or candle history simply does not reach that far, or a
    leading/internal gap makes the path itself unreliable even though the
    tail happens to reach far enough)."""

    horizon_label: str
    resolved: bool
    unresolved_reason: str | None
    forward_return_pct: float | None
    btc_adjusted_return_pct: float | None
    mfe_pct: float | None
    mae_pct: float | None
    time_to_peak_minutes: float | None
    retrace_magnitude_pct: float | None


@dataclass(frozen=True)
class RegimeOutcome:
    """One independent pump regime's own decision point (its own
    `first_seen_at`, ceil-aligned to the next candle boundary -- see
    `decision_boundary_ms`) plus its forward read at every horizon in
    HORIZONS_MINUTES."""

    base: str
    regime: Regime
    decision_at: datetime
    horizons: tuple[HorizonOutcome, ...]


@dataclass(frozen=True)
class RecurrenceSummary:
    """One regime's own place in its base's full recurrence history --
    computed purely from the already-merged Regime sequence for that base,
    ordered by first_seen_at. next_regime_gap_minutes is None exactly when
    this is the most recent regime seen so far for this base (not yet
    known to recur again, not a zero)."""

    base: str
    regime_index: int  # 0-based position among this base's own regimes
    regime_count_so_far: int  # regimes for this base up to and including this one
    next_regime_gap_minutes: float | None


def decision_boundary_ms(regime: Regime, *, timeframe_ms: int) -> int:
    """The instant a regime's own forward read starts from: ceil-aligned
    to the next full candle boundary at or after the regime's own
    `first_seen_at` -- never `last_seen_at` (see the module docstring: a
    regime's own last_seen_at is a running maximum a future episode can
    still extend, so anchoring the decision there is only knowable in
    hindsight and answers a different question than item 8's own "after a
    FIRST pump"). Ceil, never floor, so the forward read never inspects a
    bar the detector's own first observation fell inside of before it
    fully closed -- the same "never inspect a bar before it closes"
    discipline this codebase's other registered contracts use (e.g.
    source_lead_forward_cohort.py's own _expected_exit_boundary_ms).
    Deterministic given only the regime's own first episode: unaffected by
    whether, or how many, further episodes later merge into the same
    regime."""
    decision_ms = int(regime.first_seen_at.timestamp() * 1000)
    return ceil_to_timeframe(decision_ms, timeframe_ms)


def _entry_price(candles: tuple[Candle, ...], boundary_ms: int) -> float | None:
    """The OPEN of the first fully-closed candle at or after boundary_ms --
    the executable-proxy convention this module uses for entry
    (ENTRY_PRICE_SOURCE_VERSION), not a guess at an intra-bar price, and
    not that candle's own close (see ENTRY_PRICE_SOURCE_VERSION's own
    comment for why the close would be a look-ahead here). None if no
    candle at or after boundary_ms exists in the already-fetched series."""
    for candle in candles:
        if candle.ts_ms >= boundary_ms:
            return candle.open
    return None


def _return_pct(entry_price: float, exit_price: float) -> float:
    return (exit_price - entry_price) / entry_price * 100.0


def resolve_horizon_outcome(
    *,
    horizon_label: str,
    horizon_minutes: int,
    boundary_ms: int,
    timeframe_ms: int,
    candles: tuple[Candle, ...],
    btc_candles: tuple[Candle, ...],
) -> HorizonOutcome:
    """Pure: given already-fetched candle series (both must already cover
    from boundary_ms through at least boundary_ms + horizon_minutes, or
    this correctly resolves unresolved -- it never re-fetches or extends
    what it is given), compute one horizon's own forward read.

    Entry is the OPEN of the first candle at/after boundary_ms (see
    `_entry_price`'s own docstring) -- that same candle's own high/low are
    legitimately part of the trade's exposure from that instant forward,
    so it stays included in the MFE/MAE scan below, unlike when a close
    was used as entry (a look-ahead this module used to have).

    MFE/MAE use each closed candle's own high/low relative to
    entry_price (the standard max-favorable/max-adverse-excursion
    convention), not close-to-close -- a close-only read would understate
    both by missing intra-bar extremes a real exit could have captured or
    suffered. time_to_peak_minutes is measured from boundary_ms to the
    candle that produced the MFE high specifically (not the MAE low).
    retrace_magnitude_pct is the percentage-POINT gap between mfe_pct and
    forward_return_pct -- a positive MAGNITUDE (0 means the close IS the
    peak, never negative by construction), deliberately NOT signed the
    same way `app.pump_events.retrace_pct` (`last_pct - peak_pct`, always
    <= 0) is; the two measure the same underlying gap but with opposite
    sign conventions, so this field is named to make that explicit rather
    than claim a match that isn't literally true.

    resolved requires more than the horizon-end candle simply reaching far
    enough, and more than SOME candle existing at or after boundary_ms:
    the ENTIRE path from boundary_ms through horizon_end_ms must be the
    exact, gapless bar sequence -- `fetch_symbol_candles` can return a
    partial result (a leading gap -- the series starts later than
    boundary_ms -- or a bar silently missing from the middle) without
    raising, so a return/MFE/MAE built on an incomplete path would
    otherwise silently mix real price action with a hole in the data, or
    (a leading gap specifically) use a candle after boundary_ms as if it
    were the entry price AT boundary_ms. The two cases get distinct
    reasons (`leading_candle_gap` vs. `internal_candle_gap`) rather than
    one merged one, so a reader can tell "the series started late" apart
    from "a bar is missing somewhere in the middle of an otherwise-present
    series"."""
    horizon_end_ms = boundary_ms + horizon_minutes * 60_000

    def _unresolved(reason: str) -> HorizonOutcome:
        return HorizonOutcome(horizon_label, False, reason, None, None, None, None, None, None)

    entry_price = _entry_price(candles, boundary_ms)
    if entry_price is None:
        return _unresolved("missing_decision_candle")
    btc_entry_price = _entry_price(btc_candles, boundary_ms)
    if btc_entry_price is None:
        return _unresolved("missing_btc_decision_candle")

    window = closed_candles(candles, boundary_ms, horizon_end_ms, timeframe_ms=timeframe_ms)
    btc_window = closed_candles(btc_candles, boundary_ms, horizon_end_ms, timeframe_ms=timeframe_ms)
    if not window or window[-1].ts_ms + timeframe_ms < horizon_end_ms:
        return _unresolved("insufficient_candle_history")
    if not btc_window or btc_window[-1].ts_ms + timeframe_ms < horizon_end_ms:
        return _unresolved("insufficient_btc_candle_history")
    # boundary_ms is already timeframe-aligned by construction (see
    # decision_boundary_ms's own ceil), so the window's first candle must
    # sit exactly at boundary_ms -- checked separately from the general
    # gaplessness scan below so a leading gap gets its own distinct reason.
    if window[0].ts_ms != boundary_ms:
        return _unresolved("leading_candle_gap")
    if btc_window[0].ts_ms != boundary_ms:
        return _unresolved("leading_btc_candle_gap")
    if not covers_window_without_gaps(window, boundary_ms, horizon_end_ms, timeframe_ms):
        return _unresolved("internal_candle_gap")
    if not covers_window_without_gaps(btc_window, boundary_ms, horizon_end_ms, timeframe_ms):
        return _unresolved("internal_btc_candle_gap")

    horizon_close = window[-1].close
    btc_horizon_close = btc_window[-1].close
    forward_return_pct = _return_pct(entry_price, horizon_close)
    btc_return_pct = _return_pct(btc_entry_price, btc_horizon_close)

    peak_price = entry_price
    peak_ts_ms = boundary_ms
    trough_price = entry_price
    for candle in window:
        if candle.high > peak_price:
            peak_price = candle.high
            peak_ts_ms = candle.ts_ms
        if candle.low < trough_price:
            trough_price = candle.low

    mfe_pct = _return_pct(entry_price, peak_price)
    mae_pct = _return_pct(entry_price, trough_price)
    time_to_peak_minutes = (peak_ts_ms - boundary_ms) / 60_000
    # Positive magnitude -- see this function's own docstring for why this
    # is deliberately not signed the way app.pump_events.retrace_pct is.
    retrace_magnitude_pct = mfe_pct - forward_return_pct

    return HorizonOutcome(
        horizon_label=horizon_label,
        resolved=True,
        unresolved_reason=None,
        forward_return_pct=round(forward_return_pct, 6),
        btc_adjusted_return_pct=round(forward_return_pct - btc_return_pct, 6),
        mfe_pct=round(mfe_pct, 6),
        mae_pct=round(mae_pct, 6),
        time_to_peak_minutes=round(time_to_peak_minutes, 3),
        retrace_magnitude_pct=round(retrace_magnitude_pct, 6),
    )


def recurrence_summary(base_regimes: tuple[Regime, ...]) -> tuple[RecurrenceSummary, ...]:
    """base_regimes must already be one base's own regimes (from
    merge_episodes_into_regimes), ordered by first_seen_at ascending --
    the caller's job, checked and raised loudly rather than silently
    misordered. One RecurrenceSummary per regime, in the same order."""
    if not base_regimes:
        return ()
    base = base_regimes[0].base
    for regime in base_regimes:
        if regime.base != base:
            raise ValueError("recurrence_summary requires a single base")
    for prev, curr in pairwise(base_regimes):
        if curr.first_seen_at < prev.first_seen_at:
            raise ValueError("recurrence_summary requires first_seen_at-sorted regimes")

    summaries = []
    for index, regime in enumerate(base_regimes):
        next_gap: float | None = None
        if index + 1 < len(base_regimes):
            gap = base_regimes[index + 1].first_seen_at - regime.last_seen_at
            next_gap = gap.total_seconds() / 60.0
        summaries.append(
            RecurrenceSummary(
                base=base,
                regime_index=index,
                regime_count_so_far=index + 1,
                next_regime_gap_minutes=next_gap,
            )
        )
    return tuple(summaries)
