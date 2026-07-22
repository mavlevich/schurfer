"""Domain types shared by the outcome resolver and its persistence adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True)
class Decision:
    decision_id: str
    ts: datetime
    base: str
    exchange: str
    price: float | None
    features: dict[str, Any] | None
    horizons: tuple[int, ...]


@dataclass(frozen=True)
class Outcome:
    decision_id: str
    horizon_minutes: int
    anchor_exchange: str | None
    source_exchange: str | None
    entry_price: float | None
    forward_price: float | None
    mfe_pct: float | None
    mae_pct: float | None
    short_return_pct: float | None
    bars_count: int
    expected_bars: int
    coverage_ratio: float | None
    status: str
    error: str | None = None

    @classmethod
    def unavailable(
        cls,
        decision: Decision,
        horizon_minutes: int,
        *,
        anchor_exchange: str | None,
        status: str,
        expected_bars: int,
        source_exchange: str | None = None,
        bars_count: int = 0,
        coverage_ratio: float | None = None,
        error: str | None = None,
    ) -> Outcome:
        """Build a non-measurable outcome without a fragile positional constructor."""
        return cls(
            decision_id=decision.decision_id,
            horizon_minutes=horizon_minutes,
            anchor_exchange=anchor_exchange,
            source_exchange=source_exchange,
            entry_price=decision.price,
            forward_price=None,
            mfe_pct=None,
            mae_pct=None,
            short_return_pct=None,
            bars_count=bars_count,
            expected_bars=expected_bars,
            coverage_ratio=coverage_ratio,
            status=status,
            error=error,
        )
