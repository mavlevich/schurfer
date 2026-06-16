from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, Double, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .pump_event import PumpEvent


class FundingRateSnapshot(Base):
    __tablename__ = "funding_rate_snapshots"
    __table_args__ = ({"schema": "app"},)

    id: Mapped[int] = mapped_column(BigInteger(), primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger(), ForeignKey("app.pump_events.id", ondelete="CASCADE"), nullable=False
    )
    base: Mapped[str] = mapped_column(String(20), nullable=False)
    exchange: Mapped[str] = mapped_column(String(20), nullable=False)
    rate: Mapped[float] = mapped_column(Double(), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    event: Mapped["PumpEvent"] = relationship("PumpEvent", back_populates="funding_rate_snapshots")
