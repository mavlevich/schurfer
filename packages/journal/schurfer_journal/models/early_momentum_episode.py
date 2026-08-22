from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class EarlyMomentumEpisode(Base, TimestampMixin):
    """Durable candidate->armed->claimed->opened lifecycle for early_momentum_v3.

    Postgres is the source of truth for this lifecycle -- Redis WATCH/position
    keys are a repairable cache of it, never the only place a live episode can
    be found. See migration 0032 for the partial unique index that enforces
    at most one live (armed/claimed) episode per instrument, and the atomic
    claim UPDATE that makes a crashed worker's stale claim safely reclaimable.
    """

    __tablename__ = "early_momentum_episodes"

    episode_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    strategy_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.strategies.id"), nullable=False
    )
    contract_sha256: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)

    source_exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    source_native_id: Mapped[str] = mapped_column(String(64), nullable=False)

    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    native_market_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Nullable: the scanner resolves only the route (native ids + identity
    # keys) at ARM time -- no live exchange client yet to derive the
    # CCXT-unified symbol. Filled in once the trigger loop resolves it.
    execution_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_identity_key: Mapped[str] = mapped_column(Text, nullable=False)
    source_identity_key: Mapped[str] = mapped_column(Text, nullable=False)
    cluster_key: Mapped[str] = mapped_column(Text, nullable=False)

    ceiling: Mapped[Decimal] = mapped_column(Numeric(30, 14), nullable=False)
    features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    armed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Lifecycle stage -- see episodes.STATUS_ARMED/CLAIMED/OPENED/CLOSED/
    # EXPIRED/REJECTED/SUPPRESSED in apps/execution for the full vocabulary.
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    terminal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    claim_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index(
            "ux_early_momentum_episodes_live_instrument",
            "exchange",
            "native_market_id",
            unique=True,
            postgresql_where=text("status IN ('armed', 'claimed')"),
        ),
        Index(
            "ix_early_momentum_episodes_armed_expiry",
            "expires_at",
            postgresql_where=text("status = 'armed'"),
        ),
        Index(
            "ix_early_momentum_episodes_claim_expiry",
            "claim_expires_at",
            postgresql_where=text("status = 'claimed'"),
        ),
        Index(
            "ix_early_momentum_episodes_instrument_recent",
            "exchange",
            "native_market_id",
            "armed_at",
        ),
        {"schema": "app"},
    )
