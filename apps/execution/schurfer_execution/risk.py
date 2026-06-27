from dataclasses import dataclass
from typing import Any

TRADING_ENABLED_KEY = "trading:enabled"
DAILY_PNL_KEY = "trading:daily_pnl"


@dataclass
class RiskCheck:
    allowed: bool
    reason: str


def check_trading_enabled(flag: str | None) -> RiskCheck:
    if flag in ("0", "false"):
        return RiskCheck(allowed=False, reason="trading disabled (emergency stop)")
    return RiskCheck(allowed=True, reason="ok")


def check_positions_available(exchange: str, failed_exchanges: set[str]) -> RiskCheck:
    """Fail-closed: reject order if positions could not be fetched for target exchange."""
    if exchange in failed_exchanges:
        return RiskCheck(
            allowed=False,
            reason=f"positions unavailable for {exchange}: fetch failed, order rejected",
        )
    return RiskCheck(allowed=True, reason="ok")


def check_max_positions(open_count: int, max_positions: int) -> RiskCheck:
    if open_count >= max_positions:
        return RiskCheck(
            allowed=False,
            reason=f"max positions reached ({open_count}/{max_positions})",
        )
    return RiskCheck(allowed=True, reason="ok")


def check_duplicate_position(base: str, open_positions: list[dict[str, Any]]) -> RiskCheck:
    for p in open_positions:
        if p.get("base", "").upper() == base.upper():
            return RiskCheck(
                allowed=False,
                reason=f"already have position in {base} on {p['exchange']}",
            )
    return RiskCheck(allowed=True, reason="ok")


def check_sufficient_margin(
    size_usd: float, balances: list[dict[str, Any]], exchange: str
) -> RiskCheck:
    for b in balances:
        if (
            b["exchange"] == exchange
            and b.get("tradeable", True)
            and b.get("asset", "USDT") == "USDT"
        ):
            if b["free"] < size_usd:
                return RiskCheck(
                    allowed=False,
                    reason=(
                        f"insufficient margin on {exchange}: "
                        f"need ${size_usd:.0f}, have ${b['free']:.0f}"
                    ),
                )
            return RiskCheck(allowed=True, reason="ok")
    return RiskCheck(allowed=False, reason=f"no balance data for {exchange}")


def check_max_position_size(size_usd: float, max_position_usd: float) -> RiskCheck:
    if size_usd > max_position_usd:
        return RiskCheck(
            allowed=False,
            reason=f"position size ${size_usd:.0f} exceeds limit ${max_position_usd:.0f}",
        )
    return RiskCheck(allowed=True, reason="ok")


def check_daily_loss(daily_pnl: float, limit: float) -> RiskCheck:
    if daily_pnl <= -abs(limit):
        return RiskCheck(
            allowed=False,
            reason=f"daily loss limit reached (${daily_pnl:.0f} / -${limit:.0f})",
        )
    return RiskCheck(allowed=True, reason="ok")


_MAINTENANCE_MARGIN_PCT = 0.5  # conservative cross-exchange estimate


def check_liquidation_distance(
    initial_sl_pct: float,
    leverage: int,
    buffer_pct: float = 20.0,
) -> RiskCheck:
    """Ensure initial SL is not too close to the estimated liquidation price.

    For a short at leverage L, liquidation occurs when price rises roughly
    (100/L - maintenance_margin) % from entry. We require the SL to consume
    at most (1 - buffer_pct/100) of that distance, leaving a safety gap.
    """
    if leverage <= 0:
        return RiskCheck(allowed=False, reason=f"leverage must be > 0, got {leverage}")
    liq_distance_pct = max(0.0, 100.0 / leverage - _MAINTENANCE_MARGIN_PCT)
    max_sl_pct = liq_distance_pct * (1 - buffer_pct / 100)
    if initial_sl_pct > max_sl_pct:
        return RiskCheck(
            allowed=False,
            reason=(
                f"initial_sl={initial_sl_pct:.1f}% too close to liquidation "
                f"at {leverage}x leverage (max safe={max_sl_pct:.1f}%)"
            ),
        )
    return RiskCheck(allowed=True, reason="ok")


def check_funding_rate(funding_rate_pct: float, min_funding_rate_pct: float) -> RiskCheck:
    """Block entry if the current funding rate is below the configured minimum.

    funding_rate_pct is the rate expressed as a percentage per 8h period
    (e.g. -0.05 means shorts pay 0.05%/8h to longs).
    Positive rates are income for shorts; negative rates are a cost.
    """
    if funding_rate_pct < min_funding_rate_pct:
        return RiskCheck(
            allowed=False,
            reason=(
                f"funding_rate={funding_rate_pct:.4f}%/8h below min "
                f"{min_funding_rate_pct:.4f}%/8h (shorts paying too much)"
            ),
        )
    return RiskCheck(allowed=True, reason="ok")


def run_all_checks(
    *,
    base: str,
    exchange: str,
    size_usd: float,
    trading_flag: str | None,
    open_positions: list[dict[str, Any]],
    balances: list[dict[str, Any]],
    daily_pnl: float,
    max_positions: int,
    max_position_usd: float,
    daily_loss_limit_usd: float,
    failed_exchanges: set[str],
) -> RiskCheck:
    checks = [
        check_trading_enabled(trading_flag),
        check_positions_available(exchange, failed_exchanges),
        check_daily_loss(daily_pnl, daily_loss_limit_usd),
        check_max_positions(len(open_positions), max_positions),
        check_duplicate_position(base, open_positions),
        check_max_position_size(size_usd, max_position_usd),
        check_sufficient_margin(size_usd, balances, exchange),
    ]
    for check in checks:
        if not check.allowed:
            return check
    return RiskCheck(allowed=True, reason="ok")
