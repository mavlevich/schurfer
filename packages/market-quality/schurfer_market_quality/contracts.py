"""Pure contracts for describing a market-data window's required quality.

No I/O, no psycopg, no Redis, no strategy-specific concepts (no OI growth,
price range, or buy-dominance -- those are trading-signal concerns that
live with the strategy that uses this package, never here). Everything in
this module is a frozen dataclass or enum with deterministic serialization,
so a policy's identity is exactly its canonical dict/hash -- see
`WindowQualityPolicy.to_canonical_dict`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any


class Capability(enum.StrEnum):
    PRICE = "price"
    TRADES = "trades"
    OPEN_INTEREST = "open_interest"


@dataclass(frozen=True)
class SeriesIdentity:
    """Identifies one evaluated series -- one row of evidence maps to
    exactly one SeriesIdentity."""

    exchange: str
    market_type: str
    symbol: str

    def __post_init__(self) -> None:
        if not self.exchange.strip():
            raise ValueError("exchange must not be empty")
        if not self.market_type.strip():
            raise ValueError("market_type must not be empty")
        if not self.symbol.strip():
            raise ValueError("symbol must not be empty")


@dataclass(frozen=True)
class WindowQualityPolicy:
    """The frozen contract a window must satisfy before it's trustworthy
    input. `frozen=True` alone does not make a `dict`-typed field immutable
    -- it only blocks reassigning the field itself, not mutating whatever
    object it points to -- so `max_oi_age_seconds_by_exchange` is a sorted
    tuple of pairs, not a dict, and callers use `oi_age_limit_seconds`
    below rather than dict-indexing it directly.

    `to_canonical_dict()` is the single source of truth for this policy's
    identity: it feeds both a strategy's contract-hash computation and the
    SQL parameters used to evaluate windows against it, so the hash and the
    actual enforced behavior can never drift apart.
    """

    cadence_seconds: int
    required_bucket_count: int
    max_bucket_lag_seconds: int
    # Sorted tuple of (exchange, max_age_seconds) pairs -- see class
    # docstring for why not a plain dict.
    max_oi_age_seconds_by_exchange: tuple[tuple[str, int], ...]
    required_capabilities: tuple[Capability, ...]
    allowed_market_types: tuple[str, ...]
    allowed_capture_versions: frozenset[str]
    require_single_capture_version: bool = True
    require_single_universe_version: bool = True
    future_timestamp_tolerance_seconds: int = 5

    def __post_init__(self) -> None:
        if self.cadence_seconds <= 0:
            raise ValueError("cadence_seconds must be positive")
        if self.required_bucket_count <= 0:
            raise ValueError("required_bucket_count must be positive")
        if self.max_bucket_lag_seconds <= 0:
            raise ValueError("max_bucket_lag_seconds must be positive")
        if self.future_timestamp_tolerance_seconds < 0:
            raise ValueError("future_timestamp_tolerance_seconds must be >= 0")
        if not self.required_capabilities:
            raise ValueError("required_capabilities must not be empty")
        if not self.allowed_market_types:
            raise ValueError("allowed_market_types must not be empty")
        if not self.allowed_capture_versions:
            raise ValueError("allowed_capture_versions must not be empty")
        exchanges_seen = [exchange for exchange, _ in self.max_oi_age_seconds_by_exchange]
        if exchanges_seen != sorted(exchanges_seen):
            raise ValueError("max_oi_age_seconds_by_exchange must be pre-sorted by exchange")
        if len(set(exchanges_seen)) != len(exchanges_seen):
            raise ValueError("max_oi_age_seconds_by_exchange must not repeat an exchange")
        for exchange, max_age in self.max_oi_age_seconds_by_exchange:
            if max_age <= 0:
                raise ValueError(f"max_oi_age_seconds for {exchange!r} must be positive")

    def oi_age_limit_seconds(self, exchange: str) -> int | None:
        """None means this policy has no per-exchange OI-freshness limit
        configured for this exchange. `evidence.validate` treats that as
        fail-closed (rejects with STALE_OI) rather than silently passing
        an exchange nobody has calibrated a threshold for yet."""
        for exchange_name, max_age in self.max_oi_age_seconds_by_exchange:
            if exchange_name == exchange:
                return max_age
        return None

    def to_canonical_dict(self) -> dict[str, Any]:
        """Deterministic, sorted-keys serialization -- the same dict feeds
        both CONTRACT_SHA256 and SQL parameter binding, so what's hashed
        and what's enforced can never silently diverge."""
        return {
            "cadence_seconds": self.cadence_seconds,
            "required_bucket_count": self.required_bucket_count,
            "max_bucket_lag_seconds": self.max_bucket_lag_seconds,
            "max_oi_age_seconds_by_exchange": [
                {"exchange": exchange, "max_age_seconds": max_age}
                for exchange, max_age in self.max_oi_age_seconds_by_exchange
            ],
            "required_capabilities": sorted(c.value for c in self.required_capabilities),
            "allowed_market_types": sorted(self.allowed_market_types),
            "allowed_capture_versions": sorted(self.allowed_capture_versions),
            "require_single_capture_version": self.require_single_capture_version,
            "require_single_universe_version": self.require_single_universe_version,
            "future_timestamp_tolerance_seconds": self.future_timestamp_tolerance_seconds,
        }


__all__ = ["Capability", "SeriesIdentity", "WindowQualityPolicy"]
