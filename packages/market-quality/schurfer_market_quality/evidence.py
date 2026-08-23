"""Per-window quality evidence and the pure validator that turns it into a
verdict against a WindowQualityPolicy.

Deliberately quality-only: no trading-signal fields (no OI growth, price
range, or buy-dominance) live here or anywhere in this package -- a
strategy computes its own signal features from a quality-passed window
using its own types, kept entirely separate from WindowQualityResult.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .contracts import Capability, SeriesIdentity, WindowQualityPolicy
from .reasons import WindowQualityReason

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True)
class WindowQualityEvidence:
    """Everything needed to judge one series' lookback window, computed
    once (typically by SQL) and handed to `validate` -- never recomputed
    inside the validator itself, so evidence gathering and the pure
    pass/fail decision stay fully decoupled and independently testable."""

    identity: SeriesIdentity
    window_start: datetime
    window_end: datetime
    raw_row_count: int
    distinct_bucket_count: int
    max_gap_seconds: float
    latest_bucket_start: datetime
    # Distinct values seen across the window, sorted -- a single-element
    # tuple means the window is internally consistent on that dimension.
    capture_versions: tuple[str, ...]
    universe_versions: tuple[str, ...]
    price_complete_count: int
    trades_complete_count: int
    oi_complete_count: int
    first_oi_event_at: datetime | None
    latest_oi_event_at: datetime | None
    unbackfilled_gap_minutes_sum: int
    has_future_timestamp: bool
    has_invalid_price: bool
    has_invalid_open_interest: bool
    has_duplicate_bucket: bool

    def __post_init__(self) -> None:
        if self.raw_row_count < 0 or self.distinct_bucket_count < 0:
            raise ValueError("row/bucket counts must be non-negative")
        if self.distinct_bucket_count > self.raw_row_count:
            raise ValueError("distinct_bucket_count cannot exceed raw_row_count")


@dataclass(frozen=True)
class WindowQualityResult:
    evidence: WindowQualityEvidence
    reasons: tuple[WindowQualityReason, ...]

    @property
    def qualified(self) -> bool:
        return len(self.reasons) == 0


_CAPABILITY_COMPLETE_REASON: dict[Capability, tuple[WindowQualityReason, str]] = {
    Capability.PRICE: (WindowQualityReason.INCOMPLETE_PRICE, "price_complete_count"),
    Capability.TRADES: (WindowQualityReason.INCOMPLETE_TRADES, "trades_complete_count"),
    Capability.OPEN_INTEREST: (WindowQualityReason.INCOMPLETE_OI, "oi_complete_count"),
}


def validate(
    evidence: WindowQualityEvidence,
    policy: WindowQualityPolicy,
    *,
    evaluated_at: datetime,
) -> WindowQualityResult:
    """Pure: no clock reads, no I/O -- `evaluated_at` is always passed in,
    never sampled internally, so this is fully deterministic and testable
    with synthetic evidence alone."""
    reasons: list[WindowQualityReason] = []

    if evidence.identity.market_type not in policy.allowed_market_types:
        reasons.append(WindowQualityReason.WRONG_MARKET_TYPE)

    if (
        evidence.raw_row_count < policy.required_bucket_count
        or evidence.distinct_bucket_count < policy.required_bucket_count
    ):
        reasons.append(WindowQualityReason.INSUFFICIENT_ROWS)

    if evidence.has_duplicate_bucket:
        reasons.append(WindowQualityReason.DUPLICATE_BUCKET)

    # A perfectly contiguous window has max_gap_seconds == cadence_seconds
    # (the gap between two consecutive buckets); anything larger means a
    # bucket is missing somewhere inside the window.
    if evidence.max_gap_seconds > policy.cadence_seconds:
        reasons.append(WindowQualityReason.GAP)

    if evidence.unbackfilled_gap_minutes_sum > 0:
        reasons.append(WindowQualityReason.GAP)

    for capability in policy.required_capabilities:
        reason, field_name = _CAPABILITY_COMPLETE_REASON[capability]
        complete_count = getattr(evidence, field_name)
        if complete_count < policy.required_bucket_count:
            reasons.append(reason)

    bucket_lag_seconds = (evaluated_at - evidence.latest_bucket_start).total_seconds()
    if bucket_lag_seconds > policy.max_bucket_lag_seconds:
        reasons.append(WindowQualityReason.STALE_BUCKET)

    if Capability.OPEN_INTEREST in policy.required_capabilities:
        limit = policy.oi_age_limit_seconds(evidence.identity.exchange)
        if limit is None or evidence.latest_oi_event_at is None:
            reasons.append(WindowQualityReason.STALE_OI)
        else:
            oi_age_seconds = (evaluated_at - evidence.latest_oi_event_at).total_seconds()
            if oi_age_seconds > limit:
                reasons.append(WindowQualityReason.STALE_OI)

    disallowed_versions = set(evidence.capture_versions) - policy.allowed_capture_versions
    if disallowed_versions:
        reasons.append(WindowQualityReason.CAPTURE_VERSION_NOT_ALLOWED)
    elif policy.require_single_capture_version and len(evidence.capture_versions) > 1:
        reasons.append(WindowQualityReason.MULTIPLE_CAPTURE_VERSIONS)

    if policy.require_single_universe_version and len(evidence.universe_versions) > 1:
        reasons.append(WindowQualityReason.MULTIPLE_UNIVERSE_VERSIONS)

    if evidence.has_future_timestamp:
        reasons.append(WindowQualityReason.FUTURE_TIMESTAMP)

    if evidence.has_invalid_price:
        reasons.append(WindowQualityReason.INVALID_PRICE)
    if evidence.has_invalid_open_interest:
        reasons.append(WindowQualityReason.INVALID_OPEN_INTEREST)

    # dict.fromkeys dedupes while preserving first-seen order -- max_gap and
    # unbackfilled_gap_minutes_sum can both independently signal GAP for
    # the same underlying incident; a caller counting "how many windows
    # were rejected for a gap" must see that reason once, not twice.
    return WindowQualityResult(evidence=evidence, reasons=tuple(dict.fromkeys(reasons)))


__all__ = ["WindowQualityEvidence", "WindowQualityResult", "validate"]
