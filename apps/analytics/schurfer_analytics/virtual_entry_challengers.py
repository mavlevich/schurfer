"""Point-in-time entry-confirmation challengers for the pump-short replay."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from .ohlcv import TIMEFRAME_MS, Candle, ceil_to_timeframe
from .virtual_strategy import exit_parameters

if TYPE_CHECKING:
    from .replay import ReplayDecision

ENTRY_CHALLENGER_FAMILY_VERSION = "entry_confirmation_family_v1"
ENTRY_CONFIRMATION_MODEL_VERSION = "closed_5m_six_bar_wait_v1"
ENTRY_LOOKBACK_BARS = 6
ENTRY_MAX_WAIT_MINUTES = 60
ENTRY_EXECUTION_GAP_BARS = 1


@dataclass(frozen=True)
class EntryVariant:
    key: str
    version: str
    require_red_candle: bool
    min_retrace_pct: float
    lookback_bars: int = ENTRY_LOOKBACK_BARS
    max_wait_minutes: int = ENTRY_MAX_WAIT_MINUTES
    execution_gap_bars: int = ENTRY_EXECUTION_GAP_BARS

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.version.strip():
            raise ValueError("entry variant key and version must not be empty")
        if self.lookback_bars < 2:
            raise ValueError("entry lookback must contain at least two bars")
        if self.max_wait_minutes < 0 or self.max_wait_minutes % 5 != 0:
            raise ValueError("entry max wait must be a non-negative multiple of five")
        if self.execution_gap_bars < 1:
            raise ValueError("entry execution gap must contain at least one bar")
        if not math.isfinite(self.min_retrace_pct) or self.min_retrace_pct < 0:
            raise ValueError("minimum retrace must be finite and non-negative")
        if not self.require_red_candle and self.min_retrace_pct == 0:
            raise ValueError("challenger must enable at least one confirmation rule")


ENTRY_VARIANTS = (
    EntryVariant(
        key="red_candle",
        version="entry_red_candle_v1",
        require_red_candle=True,
        min_retrace_pct=0.0,
    ),
    EntryVariant(
        key="retrace_1_5",
        version="entry_retrace_1_5_v1",
        require_red_candle=False,
        min_retrace_pct=1.5,
    ),
    EntryVariant(
        key="red_candle_retrace_1_5",
        version="entry_red_candle_retrace_1_5_v1",
        require_red_candle=True,
        min_retrace_pct=1.5,
    ),
)


@dataclass(frozen=True)
class EntryConfirmation:
    status: Literal["confirmed", "not_confirmed", "unresolved"]
    entry_at_ms: int | None
    signal_at: datetime | None
    wait_minutes: float | None
    closed_red: bool | None
    retrace_pct: float | None
    error: str | None = None


def challenger_path_bounds(decision: ReplayDecision) -> tuple[int, int]:
    """Return the exact path needed by every registered entry challenger."""
    baseline_entry_ms = ceil_to_timeframe(int(decision.ts.timestamp() * 1000))
    max_lookback = max(variant.lookback_bars for variant in ENTRY_VARIANTS)
    max_execution_gap = max(variant.execution_gap_bars for variant in ENTRY_VARIANTS)
    max_wait_minutes = max(variant.max_wait_minutes for variant in ENTRY_VARIANTS)
    start_ms = baseline_entry_ms - (max_lookback + max_execution_gap) * TIMEFRAME_MS
    latest_entry_ms = baseline_entry_ms + max_wait_minutes * 60 * 1000
    end_ms = latest_entry_ms + exit_parameters(decision.pump_pct).max_hold_min * 60 * 1000
    return start_ms, end_ms


def _valid_candle(candle: Candle) -> bool:
    prices = (candle.open, candle.high, candle.low, candle.close)
    return (
        all(math.isfinite(value) and value > 0 for value in prices)
        and candle.high >= max(candle.open, candle.close, candle.low)
        and candle.low <= min(candle.open, candle.close, candle.high)
    )


def evaluate_entry_confirmation(
    decision: ReplayDecision,
    candles: tuple[Candle, ...],
    variant: EntryVariant,
) -> EntryConfirmation:
    """Find the first confirmed entry without consulting an unfinished candle.

    At each candidate entry open, only the preceding fully closed lookback window is
    examined. A missing bar makes the result unresolved because it could hide the
    first qualifying confirmation.
    """
    baseline_entry_ms = ceil_to_timeframe(int(decision.ts.timestamp() * 1000))
    last_entry_ms = baseline_entry_ms + variant.max_wait_minutes * 60 * 1000
    by_timestamp: dict[int, Candle] = {}
    for candle in candles:
        if candle.ts_ms in by_timestamp:
            return EntryConfirmation(
                status="unresolved",
                entry_at_ms=None,
                signal_at=None,
                wait_minutes=None,
                closed_red=None,
                retrace_pct=None,
                error=f"duplicate candle at {candle.ts_ms}",
            )
        by_timestamp[candle.ts_ms] = candle

    last_red: bool | None = None
    last_retrace: float | None = None
    for entry_at_ms in range(baseline_entry_ms, last_entry_ms + 1, TIMEFRAME_MS):
        # Keep one full-bar execution gap. A candle closing at entry_at_ms cannot
        # also be used to obtain that bar's open without assuming zero latency.
        window_end = entry_at_ms - variant.execution_gap_bars * TIMEFRAME_MS
        window_start = window_end - variant.lookback_bars * TIMEFRAME_MS
        window = tuple(
            by_timestamp.get(timestamp)
            for timestamp in range(window_start, window_end, TIMEFRAME_MS)
        )
        if len(window) != variant.lookback_bars or any(candle is None for candle in window):
            return EntryConfirmation(
                status="unresolved",
                entry_at_ms=None,
                signal_at=None,
                wait_minutes=None,
                closed_red=None,
                retrace_pct=None,
                error=f"missing closed entry candle before {entry_at_ms}",
            )
        complete = tuple(candle for candle in window if candle is not None)
        if any(not _valid_candle(candle) for candle in complete):
            return EntryConfirmation(
                status="unresolved",
                entry_at_ms=None,
                signal_at=None,
                wait_minutes=None,
                closed_red=None,
                retrace_pct=None,
                error=f"invalid closed entry candle before {entry_at_ms}",
            )
        last_closed = complete[-1]
        last_red = last_closed.close < last_closed.open
        window_high = max(candle.high for candle in complete)
        last_retrace = (window_high - last_closed.close) / window_high * 100
        red_allowed = not variant.require_red_candle or last_red
        retrace_allowed = last_retrace >= variant.min_retrace_pct
        if red_allowed and retrace_allowed:
            return EntryConfirmation(
                status="confirmed",
                entry_at_ms=entry_at_ms,
                signal_at=datetime.fromtimestamp(window_end / 1000, tz=UTC),
                wait_minutes=(entry_at_ms - baseline_entry_ms) / 60_000,
                closed_red=last_red,
                retrace_pct=last_retrace,
            )

    return EntryConfirmation(
        status="not_confirmed",
        entry_at_ms=None,
        signal_at=None,
        wait_minutes=float(variant.max_wait_minutes),
        closed_red=last_red,
        retrace_pct=last_retrace,
    )
