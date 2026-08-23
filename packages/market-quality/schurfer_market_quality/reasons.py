"""Rejection-reason vocabulary for window quality evaluation.

Each member names exactly one distinct incident, never a generic bucket.
Capture/universe-version rejection is deliberately split into three
separate reasons rather than one -- an unrecognized version, a seam inside
one window, and a mid-window universe change are three different
operational incidents with three different fixes, not one undifferentiated
"version mismatch".
"""

from __future__ import annotations

import enum


class WindowQualityReason(enum.StrEnum):
    INCOMPLETE_PRICE = "incomplete_price"
    INCOMPLETE_TRADES = "incomplete_trades"
    INCOMPLETE_OI = "incomplete_oi"
    GAP = "gap"
    STALE_BUCKET = "stale_bucket"
    STALE_OI = "stale_oi"
    # An unrecognized/new capture_version -- a schema change nobody has
    # blessed for this policy yet, distinct from a version seam (below).
    CAPTURE_VERSION_NOT_ALLOWED = "capture_version_not_allowed"
    # More than one capture_version present inside a single window -- a
    # seam, even if every individual version is otherwise allowed.
    MULTIPLE_CAPTURE_VERSIONS = "multiple_capture_versions"
    # The universe changed mid-window.
    MULTIPLE_UNIVERSE_VERSIONS = "multiple_universe_versions"
    DUPLICATE_BUCKET = "duplicate_bucket"
    WRONG_MARKET_TYPE = "wrong_market_type"
    FUTURE_TIMESTAMP = "future_timestamp"
    INVALID_PRICE = "invalid_price"
    INVALID_OPEN_INTEREST = "invalid_open_interest"
    INSUFFICIENT_ROWS = "insufficient_rows"


__all__ = ["WindowQualityReason"]
