from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin

if TYPE_CHECKING:
    from .trade import Trade


class LiveOrderAttempt(Base, TimestampMixin):
    """Durable pre-flight record for a live order, written BEFORE
    orders.place_order ever calls the exchange -- client_order_id is
    generated here first, then passed to the exchange as clientOrderId
    (bybit: orderLinkId), so a crash at any point after this row exists is
    reconcilable by that id even if the local process never captures the
    exchange's own order_id. If this row itself can't be written, place_order
    fails closed and never calls the exchange -- see orders.py."""

    __tablename__ = "live_order_attempts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    base: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    size_usd: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    leverage: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    contract_size: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    exit_params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    setup_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    filled_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    trade_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("app.trades.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    trade: Mapped["Trade | None"] = relationship("Trade")

    __table_args__ = (
        Index(
            "ux_live_order_attempts_client_order_id",
            "client_order_id",
            unique=True,
        ),
        Index("ix_live_order_attempts_status", "status"),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'completed', 'failed')",
            name="ck_live_order_attempts_status",
        ),
        {"schema": "app"},
    )
