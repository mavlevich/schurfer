from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Double, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class PumpEvent(Base):
    __tablename__ = "pump_events"
    __table_args__ = ({"schema": "app"},)

    id: Mapped[int] = mapped_column(BigInteger(), primary_key=True, autoincrement=True)
    base: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    peak_pct: Mapped[float] = mapped_column(Double(), nullable=False)
    last_pct: Mapped[float] = mapped_column(Double(), nullable=False)
    exchanges: Mapped[list[Any]] = mapped_column(JSONB(), nullable=False, default=list)
