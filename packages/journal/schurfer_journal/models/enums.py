import enum


class Exchange(enum.StrEnum):
    BYBIT = "bybit"
    OKX = "okx"
    HYPERLIQUID = "hyperliquid"


class MarketType(enum.StrEnum):
    SPOT = "spot"
    PERP = "perp"
    FUTURES = "futures"


class Side(enum.StrEnum):
    LONG = "long"
    SHORT = "short"


class TradeStatus(enum.StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class SignalSource(enum.StrEnum):
    PUMP_SHORT = "pump_short"
    FUNDING_ARB = "funding_arb"
    MANUAL = "manual"


class OutcomeLabel(enum.StrEnum):
    WIN = "win"
    LOSS = "loss"
    BREAKEVEN = "breakeven"


class OutcomeQuality(enum.StrEnum):
    PLANNED = "planned"  # exit at intended target/stop
    LUCKY = "lucky"  # win but not due to the thesis
    MISTAKE = "mistake"  # avoidable loss
    FORCE_MAJEURE = "force_majeure"  # external event (liquidation, api error)


class AlertStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    SKIPPED = "skipped"
    EXPIRED = "expired"
