"""Frozen prospective paper contract for ``momentum_flow_paper_v1``.

The paper probe measures the already-frozen WATCH signal. It must never rewrite a
missed entry from later market data or silently change its exit policy after outcomes
are visible. Exact executable VWAPs include spread and book impact in the simulated
fill price; conservative fees and funding are applied separately.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from hashlib import sha256

from schurfer_performance import COST_MODEL_VERSION

from .momentum_flow_watch_contract import (
    BINANCE_WATCH_CONTRACT,
    BINANCE_WATCH_CONTRACT_SHA256,
    MARKET_TYPE,
    SOURCE_EXCHANGE,
    WATCH_CONTRACT_SHA256,
    WATCH_VERSION,
)

PAPER_VERSION = "momentum_flow_paper_v1"
SIDE = "long"
POSITION_NOTIONAL_USD = 50.0
LEVERAGE = 1

STOP_LOSS_PCT = 5.0
MAX_HOLD_MINUTES = 240
OUTCOME_HORIZONS_MINUTES = (5, 15, 30, 60, 120, 240)

MAX_WATCH_TO_QUOTE_SECONDS = 30
MAX_QUOTE_LATENCY_SECONDS = 5
MAX_OUTCOME_QUOTE_LATENESS_SECONDS = 60
POLL_INTERVAL_SECONDS = 5.0
BOOK_LIMIT = 50


@dataclass(frozen=True)
class PaperContract:
    paper_version: str = PAPER_VERSION
    watch_version: str = WATCH_VERSION
    watch_contract_sha256: str = WATCH_CONTRACT_SHA256
    source_exchange: str = SOURCE_EXCHANGE
    market_type: str = MARKET_TYPE
    side: str = SIDE
    position_notional_usd: float = POSITION_NOTIONAL_USD
    leverage: int = LEVERAGE
    stop_loss_pct: float = STOP_LOSS_PCT
    max_hold_minutes: int = MAX_HOLD_MINUTES
    outcome_horizons_minutes: tuple[int, ...] = OUTCOME_HORIZONS_MINUTES
    max_watch_to_quote_seconds: int = MAX_WATCH_TO_QUOTE_SECONDS
    max_quote_latency_seconds: int = MAX_QUOTE_LATENCY_SECONDS
    max_outcome_quote_lateness_seconds: int = MAX_OUTCOME_QUOTE_LATENESS_SECONDS
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS
    book_limit: int = BOOK_LIMIT
    cost_model_version: str = COST_MODEL_VERSION
    entry_quote_side: str = "ask"
    exit_quote_side: str = "bid"

    def __post_init__(self) -> None:
        if self.side != "long":
            raise ValueError("momentum paper v1 must remain a long probe")
        if self.position_notional_usd <= 0:
            raise ValueError("paper notional must be positive")
        if self.leverage != 1:
            raise ValueError("momentum paper v1 must remain unlevered")
        if not 0 < self.stop_loss_pct < 100:
            raise ValueError("stop loss must be in (0, 100)")
        if self.max_hold_minutes <= 0:
            raise ValueError("max hold must be positive")
        if not self.outcome_horizons_minutes:
            raise ValueError("at least one outcome horizon is required")
        if tuple(sorted(set(self.outcome_horizons_minutes))) != self.outcome_horizons_minutes:
            raise ValueError("outcome horizons must be unique and sorted")
        if any(
            value <= 0 or value > self.max_hold_minutes for value in self.outcome_horizons_minutes
        ):
            raise ValueError("outcome horizons must be inside the bounded hold window")
        if self.outcome_horizons_minutes[-1] != self.max_hold_minutes:
            raise ValueError("the final outcome horizon must equal max hold")
        if (
            min(
                self.max_watch_to_quote_seconds,
                self.max_quote_latency_seconds,
                self.max_outcome_quote_lateness_seconds,
                self.poll_interval_seconds,
                self.book_limit,
            )
            <= 0
        ):
            raise ValueError("paper timing and book limits must be positive")

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def sha256_hex(self) -> str:
        return sha256(self.canonical_json().encode()).hexdigest()


FROZEN_PAPER_CONTRACT = PaperContract()
# Checked literal filled after the contract is reviewed. Never recompute this value as
# part of normal runtime or a formatting pass.
PAPER_CONTRACT_SHA256 = "d2e1c4cb81c9ac7e4100bfbef677f1e3d14b9f6e5fcb04644f6bb6ccca07c945"
if FROZEN_PAPER_CONTRACT.sha256_hex() != PAPER_CONTRACT_SHA256:
    raise RuntimeError("momentum paper v1 contract changed without an explicit version decision")

# Binance's own paper instance -- same Foundation-then-Resolution venue-
# expansion pattern as momentum_flow_watch_contract.BINANCE_WATCH_CONTRACT.
# Every threshold below (position size, stop, hold window, outcome
# horizons, timing/latency bounds, cost model) is reused verbatim from
# FROZEN_PAPER_CONTRACT; only the identity fields differ: paper_version
# (its own distinct string, so acquire_worker_lock/register_run/the Redis
# health key never collide with the live Bybit worker's -- see
# momentum_flow_paper_worker.health_key's own doc comment), and
# watch_version/watch_contract_sha256, which point at
# BINANCE_WATCH_CONTRACT instead of the live Bybit WatchContract so this
# worker only ever claims WATCH decisions its own Binance WATCH shadow
# actually produced.
BINANCE_PAPER_CONTRACT = PaperContract(
    paper_version="momentum_flow_paper_v1_binance",
    watch_version=BINANCE_WATCH_CONTRACT.watch_version,
    watch_contract_sha256=BINANCE_WATCH_CONTRACT_SHA256,
    source_exchange="binance",
)
BINANCE_PAPER_CONTRACT_SHA256 = "c862fdfa6d2748a899cd813b0202960eb745a94da5881e7597361c07b0e4767a"
if BINANCE_PAPER_CONTRACT.sha256_hex() != BINANCE_PAPER_CONTRACT_SHA256:
    raise RuntimeError(
        "momentum paper v1 binance contract changed without an explicit version decision"
    )
