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

# Real capital committed per probe, independent of leverage. FROZEN_PAPER_CONTRACT
# (leverage=1) commits its whole notional as margin; LEVERAGED_PAPER_CONTRACT below
# (leverage=3) commits the SAME real capital but sizes the simulated position at
# 3x that -- position_notional_usd = MARGIN_USD * leverage is enforced below so a
# future sibling contract can't drift the real-capital-at-risk figure by accident.
MARGIN_USD = 50.0

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
        if self.leverage < 1:
            raise ValueError("leverage must be a positive integer")
        if self.position_notional_usd != MARGIN_USD * self.leverage:
            raise ValueError(
                f"position_notional_usd ({self.position_notional_usd}) must equal "
                f"MARGIN_USD ({MARGIN_USD}) x leverage ({self.leverage}) = "
                f"{MARGIN_USD * self.leverage}: the real capital committed per probe "
                "must stay exactly MARGIN_USD regardless of which sibling contract "
                "sizes the simulated position, or a leveraged variant could silently "
                "commit more real capital than intended"
            )
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

# A leveraged sizing variant of the SAME live Bybit WATCH signal FROZEN_PAPER_CONTRACT
# already probes -- not a venue expansion (watch_version/source_exchange are identical
# to FROZEN_PAPER_CONTRACT's own), a sizing expansion. Real capital at risk per probe
# stays MARGIN_USD ($50, enforced by __post_init__ above); the simulated position is
# sized at 3x that ($150 notional) via leverage=3, so this deliberately does NOT just
# replay FROZEN_PAPER_CONTRACT's own results scaled by 3 after the fact -- a $150
# simulated fill walks further into the real order book than a $50 one
# (momentum_flow_paper_market.py's own target_usd=contract.position_notional_usd feeds
# straight into the executable-VWAP/impact simulation), so entry_impact_bps and
# entry_spread_bps genuinely differ, not just the dollar P&L. Its own distinct
# paper_version keeps acquire_worker_lock/register_run/the Redis health key and its
# own uq_momentum_flow_paper_watch(paper_version, watch_id) row fully isolated from
# FROZEN_PAPER_CONTRACT's worker, so both independently claim and probe every WATCH
# decision the live Bybit WATCH worker produces.
LEVERAGED_PAPER_CONTRACT = PaperContract(
    paper_version="momentum_flow_paper_v1_lev3",
    position_notional_usd=MARGIN_USD * 3,
    leverage=3,
)
LEVERAGED_PAPER_CONTRACT_SHA256 = "5e3df53461ce4ac83a4fb44a9dc585fe9545602e37089bbdd916e5955d280dbd"
if LEVERAGED_PAPER_CONTRACT.sha256_hex() != LEVERAGED_PAPER_CONTRACT_SHA256:
    raise RuntimeError(
        "momentum paper v1 lev3 contract changed without an explicit version decision"
    )

# A longer-hold sizing-unchanged variant of the SAME live Bybit WATCH signal
# FROZEN_PAPER_CONTRACT already probes -- not a venue or sizing expansion
# (watch_version/source_exchange/position_notional_usd/leverage are all
# identical to FROZEN_PAPER_CONTRACT's own), a hold-duration expansion.
# Prompted by HYP-015 (docs/research/discovery-ledger.md): an informal,
# non-pre-registered bar-by-bar sweep of FROZEN_PAPER_CONTRACT's own
# already-collected probes found the live 240-minute/5%-stop defaults
# underperforming most other cells in a 60-1440 minute grid, but that sweep
# had no cost model (notably no funding) and reused the same probes across
# every cell -- a hypothesis, not evidence. This contract exists to test it
# honestly: a genuinely new forward cohort, with the production pipeline's
# own real fee/funding accounting applied automatically, not another replay
# of the same already-viewed window. 720 minutes (12h) is a deliberate
# middle-of-range pick from HYP-015's own tested grid, not the single
# best-looking cell -- funding accrues every 8h on a perp, so the raw
# sweep's own apparent improvement from 4h up through 12-24h is likely
# overstated before costs are applied, and 12h keeps this comfortably
# short of the 36h/48h cells that already turned net negative even before
# costs. outcome_horizons_minutes extends FROZEN_PAPER_CONTRACT's own
# horizon list with two more points (480, 720) rather than replacing it,
# so results stay comparable at every horizon the baseline already reports.
# Its own distinct paper_version keeps acquire_worker_lock/register_run/the
# Redis health key and its own uq_momentum_flow_paper_watch(paper_version,
# watch_id) row fully isolated from FROZEN_PAPER_CONTRACT's worker, so both
# independently claim and probe every WATCH decision the live Bybit WATCH
# worker produces.
HOLD12H_PAPER_CONTRACT = PaperContract(
    paper_version="momentum_flow_paper_v1_hold12h",
    max_hold_minutes=720,
    outcome_horizons_minutes=(5, 15, 30, 60, 120, 240, 480, 720),
)
HOLD12H_PAPER_CONTRACT_SHA256 = "f280bd1430c17fb1530b438a17c171e100e16e0f813806cc941c0383ae88e22b"
if HOLD12H_PAPER_CONTRACT.sha256_hex() != HOLD12H_PAPER_CONTRACT_SHA256:
    raise RuntimeError(
        "momentum paper v1 hold12h contract changed without an explicit version decision"
    )
if HOLD12H_PAPER_CONTRACT.sha256_hex() != HOLD12H_PAPER_CONTRACT_SHA256:
    raise RuntimeError(
        "momentum paper v1 hold12h contract changed without an explicit version decision"
    )
