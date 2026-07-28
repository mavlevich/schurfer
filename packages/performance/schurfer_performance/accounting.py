"""Pure, versioned performance accounting shared by replay, paper, and shadow."""

from __future__ import annotations

import math
from dataclasses import dataclass

COST_MODEL_VERSION = "conservative_costs_v1"
LEGACY_ACCOUNTING_VERSION = "legacy_price_only_v1"
PAPER_ACCOUNTING_VERSION = "paper_conservative_costs_v1"


def _finite_non_negative(name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True)
class CostParameters:
    """Conservative costs expressed against position notional."""

    taker_fee_bps_per_side: float = 10.0
    funding_cost_bps_per_8h: float = 5.0

    def __post_init__(self) -> None:
        _finite_non_negative("taker fee", self.taker_fee_bps_per_side)
        _finite_non_negative("funding cost", self.funding_cost_bps_per_8h)


DEFAULT_COSTS = CostParameters()


@dataclass(frozen=True)
class AccountingResult:
    gross_return_pct: float
    net_return_pct: float | None
    gross_pnl_usd: float
    net_pnl_usd: float | None
    fees_usd: float
    funding_usd: float
    slippage_usd: float | None
    fee_cost_bps: float
    funding_cost_bps: float
    slippage_cost_bps: float | None
    status: str
    error: str | None = None


def calculate_performance(
    *,
    position_usd: float,
    entry_price: float,
    exit_price: float,
    side: str,
    duration_minutes: float,
    entry_slippage_bps: float | None,
    exit_slippage_bps: float | None,
    costs: CostParameters = DEFAULT_COSTS,
) -> AccountingResult:
    """Calculate gross movement and conservative net performance.

    The notional stays fixed for fee, funding, and slippage estimates so the
    contract exactly matches the replay engine. Missing slippage fails net
    accounting closed while preserving the observable gross result.
    """

    for name, value in (
        ("position_usd", position_usd),
        ("entry_price", entry_price),
        ("exit_price", exit_price),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    _finite_non_negative("duration_minutes", duration_minutes)
    if side not in {"long", "short"}:
        raise ValueError("side must be long or short")

    gross_return_pct = (
        (entry_price - exit_price) / entry_price * 100
        if side == "short"
        else (exit_price - entry_price) / entry_price * 100
    )
    gross_pnl_usd = position_usd * gross_return_pct / 100
    fee_cost_bps = costs.taker_fee_bps_per_side * 2
    funding_cost_bps = costs.funding_cost_bps_per_8h * duration_minutes / 480
    fees_usd = position_usd * fee_cost_bps / 10_000
    funding_usd = position_usd * funding_cost_bps / 10_000

    if entry_slippage_bps is None or exit_slippage_bps is None:
        missing = []
        if entry_slippage_bps is None:
            missing.append("entry_slippage_bps")
        if exit_slippage_bps is None:
            missing.append("exit_slippage_bps")
        return AccountingResult(
            gross_return_pct=gross_return_pct,
            net_return_pct=None,
            gross_pnl_usd=gross_pnl_usd,
            net_pnl_usd=None,
            fees_usd=fees_usd,
            funding_usd=funding_usd,
            slippage_usd=None,
            fee_cost_bps=fee_cost_bps,
            funding_cost_bps=funding_cost_bps,
            slippage_cost_bps=None,
            status="incomplete",
            error=f"missing {', '.join(missing)}",
        )

    _finite_non_negative("entry_slippage_bps", entry_slippage_bps)
    _finite_non_negative("exit_slippage_bps", exit_slippage_bps)
    slippage_cost_bps = entry_slippage_bps + exit_slippage_bps
    slippage_usd = position_usd * slippage_cost_bps / 10_000
    net_pnl_usd = gross_pnl_usd - fees_usd - funding_usd - slippage_usd
    net_return_pct = net_pnl_usd / position_usd * 100
    return AccountingResult(
        gross_return_pct=gross_return_pct,
        net_return_pct=net_return_pct,
        gross_pnl_usd=gross_pnl_usd,
        net_pnl_usd=net_pnl_usd,
        fees_usd=fees_usd,
        funding_usd=funding_usd,
        slippage_usd=slippage_usd,
        fee_cost_bps=fee_cost_bps,
        funding_cost_bps=funding_cost_bps,
        slippage_cost_bps=slippage_cost_bps,
        status="complete",
    )
