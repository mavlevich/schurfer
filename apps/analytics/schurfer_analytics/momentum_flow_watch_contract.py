"""Frozen prospective contract for ``momentum_flow_watch_v1``.

This module contains only input-side rules. The thresholds were fixed before the
first prospective WATCH cohort started and do not use pump labels, returns, fills,
or any other future outcome. Cross-sectional percentiles are computed from the
same closed UTC minute across the complete quality-ready Bybit universe.

The contract emits market-state WATCH observations, not trading advice and not a
profitability claim. Paper execution and outcome resolution are separate versions.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from hashlib import sha256

from .momentum_flow_capture_contract import (
    BYBIT_MOMENTUM_CAPTURE_VERSION,
    BYBIT_MOMENTUM_EXCHANGE,
    BYBIT_MOMENTUM_MARKET_TYPE,
)

WATCH_VERSION = "momentum_flow_watch_v1"
CAPTURE_VERSION = BYBIT_MOMENTUM_CAPTURE_VERSION
SOURCE_EXCHANGE = BYBIT_MOMENTUM_EXCHANGE
MARKET_TYPE = BYBIT_MOMENTUM_MARKET_TYPE

LOOKBACK_MINUTES = 60
FLOW_WINDOW_MINUTES = 15
FLOW_BASELINE_MINUTES = 45
MIN_CROSS_SECTION_SIZE = 100

OI_GROWTH_PERCENTILE = 0.90
BUY_IMBALANCE_PERCENTILE = 0.90
FLOW_ACCELERATION_PERCENTILE = 0.75

MIN_OI_GROWTH_PCT = 0.0
MIN_BUY_IMBALANCE = 0.10
MIN_FLOW_ACCELERATION = 1.50
MIN_FLOW_NOTIONAL_USD_15M = 10_000.0
MIN_PRICE_RETURN_60M_PCT = -5.0
MAX_PRICE_RETURN_60M_PCT = 12.0
MAX_PRICE_RETURN_15M_PCT = 6.0

MAX_BUCKET_DECISION_DELAY_SECONDS = 120
REARM_CLEAR_MINUTES = 5
WATCH_COOLDOWN_MINUTES = 360


@dataclass(frozen=True)
class WatchContract:
    watch_version: str = WATCH_VERSION
    capture_version: str = CAPTURE_VERSION
    source_exchange: str = SOURCE_EXCHANGE
    market_type: str = MARKET_TYPE
    lookback_minutes: int = LOOKBACK_MINUTES
    flow_window_minutes: int = FLOW_WINDOW_MINUTES
    flow_baseline_minutes: int = FLOW_BASELINE_MINUTES
    min_cross_section_size: int = MIN_CROSS_SECTION_SIZE
    oi_growth_percentile: float = OI_GROWTH_PERCENTILE
    buy_imbalance_percentile: float = BUY_IMBALANCE_PERCENTILE
    flow_acceleration_percentile: float = FLOW_ACCELERATION_PERCENTILE
    min_oi_growth_pct: float = MIN_OI_GROWTH_PCT
    min_buy_imbalance: float = MIN_BUY_IMBALANCE
    min_flow_acceleration: float = MIN_FLOW_ACCELERATION
    min_flow_notional_usd_15m: float = MIN_FLOW_NOTIONAL_USD_15M
    min_price_return_60m_pct: float = MIN_PRICE_RETURN_60M_PCT
    max_price_return_60m_pct: float = MAX_PRICE_RETURN_60M_PCT
    max_price_return_15m_pct: float = MAX_PRICE_RETURN_15M_PCT
    max_bucket_decision_delay_seconds: int = MAX_BUCKET_DECISION_DELAY_SECONDS
    rearm_clear_minutes: int = REARM_CLEAR_MINUTES
    watch_cooldown_minutes: int = WATCH_COOLDOWN_MINUTES

    def __post_init__(self) -> None:
        if self.lookback_minutes != self.flow_window_minutes + self.flow_baseline_minutes:
            raise ValueError("flow windows must exactly cover lookback_minutes")
        if (
            min(
                self.lookback_minutes,
                self.flow_window_minutes,
                self.flow_baseline_minutes,
                self.min_cross_section_size,
                self.max_bucket_decision_delay_seconds,
                self.rearm_clear_minutes,
                self.watch_cooldown_minutes,
            )
            <= 0
        ):
            raise ValueError("WATCH durations and sample sizes must be positive")
        percentiles = (
            self.oi_growth_percentile,
            self.buy_imbalance_percentile,
            self.flow_acceleration_percentile,
        )
        if any(not 0.0 < value <= 1.0 for value in percentiles):
            raise ValueError("WATCH percentiles must be in (0, 1]")
        if self.min_flow_notional_usd_15m < 0:
            raise ValueError("minimum flow notional cannot be negative")
        if self.min_price_return_60m_pct > self.max_price_return_60m_pct:
            raise ValueError("minimum price return cannot exceed maximum")

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def sha256_hex(self) -> str:
        return sha256(self.canonical_json().encode()).hexdigest()


FROZEN_WATCH_CONTRACT = WatchContract()
WATCH_CONTRACT_SHA256 = "f112c05005e8eb5c81670df09beedb741351bce55c307019aa347552c5dd6f97"
if FROZEN_WATCH_CONTRACT.sha256_hex() != WATCH_CONTRACT_SHA256:
    raise RuntimeError("momentum WATCH v1 contract changed without an explicit version decision")

# Binance's own WATCH instance (ROADMAP phase 3: "Binance WATCH shadow,
# frozen v1 logic, own version hash"). Every threshold below, including
# min_cross_section_size, is reused verbatim from FROZEN_WATCH_CONTRACT;
# only the two identity fields differ. Full rationale for why
# watch_version specifically (not just source_exchange) has to be a
# distinct string, and why min_cross_section_size stays untouched despite
# Binance's smaller universe, is in
# docs/research/binance-momentum-watch-v1.md.
BINANCE_WATCH_CONTRACT = WatchContract(
    watch_version="momentum_flow_watch_v1_binance",
    source_exchange="binance",
)
BINANCE_WATCH_CONTRACT_SHA256 = "f47199562827b58c7aa31ad032d80c2eaf7d43f7aef3979ca723fa3b8d5f1aa1"
if BINANCE_WATCH_CONTRACT.sha256_hex() != BINANCE_WATCH_CONTRACT_SHA256:
    raise RuntimeError(
        "momentum WATCH v1 binance contract changed without an explicit version decision"
    )
