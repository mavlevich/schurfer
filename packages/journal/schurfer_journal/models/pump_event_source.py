from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    Double,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .pump_event import PumpEvent


class PumpEventSource(Base):
    __tablename__ = "pump_event_sources"
    __table_args__ = (
        UniqueConstraint("event_id", "exchange", name="uq_pump_event_source_venue"),
        {"schema": "app"},
    )

    id: Mapped[int] = mapped_column(BigInteger(), primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger(), ForeignKey("app.pump_events.id", ondelete="CASCADE"), nullable=False
    )
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(128), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    first_change_pct: Mapped[float] = mapped_column(Double(), nullable=False)
    last_change_pct: Mapped[float] = mapped_column(Double(), nullable=False)
    peak_change_pct: Mapped[float] = mapped_column(Double(), nullable=False)
    first_price: Mapped[float | None] = mapped_column(Double(), nullable=True)
    last_price: Mapped[float | None] = mapped_column(Double(), nullable=True)
    first_volume_24h_usd: Mapped[float | None] = mapped_column(Double(), nullable=True)
    last_volume_24h_usd: Mapped[float | None] = mapped_column(Double(), nullable=True)
    observation_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)

    event: Mapped["PumpEvent"] = relationship("PumpEvent", back_populates="sources")
