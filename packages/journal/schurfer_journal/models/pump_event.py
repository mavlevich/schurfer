from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, DateTime, Double, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .funding_rate_snapshot import FundingRateSnapshot
    from .oi_snapshot import OiSnapshot
    from .pump_event_snapshot import PumpEventSnapshot
    from .pump_event_source import PumpEventSource


class PumpEvent(Base):
    __tablename__ = "pump_events"
    __table_args__ = ({"schema": "app"},)

    id: Mapped[int] = mapped_column(BigInteger(), primary_key=True, autoincrement=True)
    base: Mapped[str] = mapped_column(String(20), nullable=False)
    episode: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)
    miss_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    peak_pct: Mapped[float] = mapped_column(Double(), nullable=False)
    last_pct: Mapped[float] = mapped_column(Double(), nullable=False)
    retrace_pct: Mapped[float | None] = mapped_column(Double(), nullable=True)
    exchanges: Mapped[list[Any]] = mapped_column(JSONB(), nullable=False, default=list)

    snapshots: Mapped[list["PumpEventSnapshot"]] = relationship(
        "PumpEventSnapshot", back_populates="event", cascade="all, delete-orphan"
    )
    sources: Mapped[list["PumpEventSource"]] = relationship(
        "PumpEventSource", back_populates="event", cascade="all, delete-orphan"
    )
    oi_snapshots: Mapped[list["OiSnapshot"]] = relationship(
        "OiSnapshot", back_populates="event", cascade="all, delete-orphan"
    )
    funding_rate_snapshots: Mapped[list["FundingRateSnapshot"]] = relationship(
        "FundingRateSnapshot", back_populates="event", cascade="all, delete-orphan"
    )
