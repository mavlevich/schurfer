from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, DateTime, Double, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .pump_event import PumpEvent


class PumpEventSnapshot(Base):
    __tablename__ = "pump_event_snapshots"
    __table_args__ = ({"schema": "app"},)

    id: Mapped[int] = mapped_column(BigInteger(), primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger(), ForeignKey("app.pump_events.id", ondelete="CASCADE"), nullable=False
    )
    offset_label: Mapped[str] = mapped_column(String(8), nullable=False)  # '+1h', '+4h', '+24h'
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    price: Mapped[float | None] = mapped_column(Double(), nullable=True)
    change_pct: Mapped[float | None] = mapped_column(Double(), nullable=True)
    exchanges: Mapped[list[Any]] = mapped_column(JSONB(), nullable=False, default=list)

    event: Mapped["PumpEvent"] = relationship("PumpEvent", back_populates="snapshots")
