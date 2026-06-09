import enum


class Exchange(str, enum.Enum):
    BYBIT = "bybit"
    OKX = "okx"
    HYPERLIQUID = "hyperliquid"


class MarketType(str, enum.Enum):
    SPOT = "spot"
    PERP = "perp"
    FUTURES = "futures"


class Side(str, enum.Enum):
    LONG = "long"
    SHORT = "short"


class TradeStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class SignalSource(str, enum.Enum):
    PUMP_SHORT = "pump_short"
    FUNDING_ARB = "funding_arb"
    MANUAL = "manual"


class OutcomeLabel(str, enum.Enum):
    WIN = "win"
    LOSS = "loss"
    BREAKEVEN = "breakeven"


class OutcomeQuality(str, enum.Enum):
    PLANNED = "planned"  # exit at intended target/stop
    LUCKY = "lucky"  # win but not due to the thesis
    MISTAKE = "mistake"  # avoidable loss
    FORCE_MAJEURE = "force_majeure"  # external event (liquidation, api error)


class AlertStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    SKIPPED = "skipped"
    EXPIRED = "expired"
