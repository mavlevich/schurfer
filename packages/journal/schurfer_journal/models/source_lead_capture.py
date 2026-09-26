from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin

if TYPE_CHECKING:
    from .pump_event import PumpEvent


class SourceLeadCapture(Base, TimestampMixin):
    """Immutable point-in-time classification of one observed source lead."""

    __tablename__ = "source_lead_captures"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("app.pump_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    capture_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    base: Mapped[str] = mapped_column(String(64), nullable=False)
    source_symbol: Mapped[str] = mapped_column(String(128), nullable=False)
    source_identity_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_market_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_first_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    collector_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    capture_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    capture_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    eligibility_reason: Mapped[str] = mapped_column(String(64), nullable=False)
    source_change_pct: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    source_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    source_volume_24h_usd: Mapped[Decimal | None] = mapped_column(Numeric(24, 4), nullable=True)
    first_sources: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    source_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    event: Mapped["PumpEvent"] = relationship("PumpEvent")
    targets: Mapped[list["SourceLeadTargetObservation"]] = relationship(
        "SourceLeadTargetObservation",
        back_populates="capture",
        cascade="all, delete-orphan",
    )
    qualifications: Mapped[list["SourceLeadQualification"]] = relationship(
        "SourceLeadQualification",
        back_populates="capture",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index(
            "ux_source_lead_captures_event_version",
            "event_id",
            "capture_version",
            unique=True,
        ),
        Index("ix_source_lead_captures_observed", "source_first_observed_at"),
        Index("ix_source_lead_captures_status", "status"),
        CheckConstraint(
            "status IN ('collecting', 'complete', 'excluded', 'abandoned')",
            name="ck_source_lead_captures_status",
        ),
        CheckConstraint(
            "source_change_pct >= -5000 AND source_change_pct <= 5000",
            name="ck_source_lead_captures_change",
        ),
        CheckConstraint(
            "(status = 'collecting' AND capture_completed_at IS NULL) OR "
            "(status <> 'collecting' AND capture_completed_at IS NOT NULL)",
            name="ck_source_lead_captures_completion",
        ),
        {"schema": "app"},
    )


class SourceLeadTargetObservation(Base, TimestampMixin):
    """One bounded target-venue quote attempt made after a source lead."""

    __tablename__ = "source_lead_target_observations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    capture_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("app.source_lead_captures.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    eligibility_reason: Mapped[str] = mapped_column(String(64), nullable=False)
    identity_match_method: Mapped[str] = mapped_column(String(32), nullable=False)
    identity_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[int] = mapped_column(nullable=False)
    requested_notional_usd: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    instrument: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    ticker: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    liquidity: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    capture: Mapped["SourceLeadCapture"] = relationship(
        "SourceLeadCapture", back_populates="targets"
    )

    __table_args__ = (
        Index(
            "ux_source_lead_target_capture_exchange",
            "capture_id",
            "target_exchange",
            unique=True,
        ),
        Index("ix_source_lead_target_observed", "observed_at"),
        Index("ix_source_lead_target_status", "status"),
        CheckConstraint("latency_ms >= 0", name="ck_source_lead_target_latency"),
        CheckConstraint(
            "requested_notional_usd > 0",
            name="ck_source_lead_target_notional",
        ),
        CheckConstraint(
            "status IN ('sampled', 'excluded', 'fetch_failed')",
            name="ck_source_lead_target_status",
        ),
        CheckConstraint(
            "NOT (identity_match_method = 'base_symbol_v1' AND identity_verified)",
            name="ck_source_lead_target_provisional_identity",
        ),
        # registry_exact_v2 means _resolve_registered_target_market matched
        # the exact registered market -- identity_verified must be true even
        # when a later eligibility check or the network fetch itself then
        # failed. registry_lookup_v2 means no market was ever resolved --
        # identity_verified must be false. Added research/gate-source-lead-
        # registry-activation-v2 (colleague review, 2026-08-28): previously
        # every failure was tagged registry_exact_v2 with identity_verified
        # always false, which claimed a route was confirmed for captures
        # that never resolved one at all.
        CheckConstraint(
            "identity_match_method NOT IN ('registry_exact_v2', 'registry_lookup_v2') OR "
            "identity_verified = (identity_match_method = 'registry_exact_v2')",
            name="ck_source_lead_target_v2_identity_pairing",
        ),
        {"schema": "app"},
    )


class SourceLeadQualification(Base, TimestampMixin):
    """Append-only reviewed identity and deterministic venue-selection result."""

    __tablename__ = "source_lead_qualifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    capture_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("app.source_lead_captures.id", ondelete="CASCADE"),
        nullable=False,
    )
    qualification_version: Mapped[str] = mapped_column(String(64), nullable=False)
    identity_registry_version: Mapped[str] = mapped_column(String(64), nullable=False)
    identity_registry_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    venue_selector_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_asset_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    selected_target_exchange: Mapped[str | None] = mapped_column(String(32), nullable=True)
    selected_round_trip_impact_bps: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    requested_notional_usd: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    qualified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    capture: Mapped["SourceLeadCapture"] = relationship(
        "SourceLeadCapture", back_populates="qualifications"
    )

    __table_args__ = (
        Index(
            "ux_source_lead_qualification_capture_version",
            "capture_id",
            "qualification_version",
            unique=True,
        ),
        Index("ix_source_lead_qualification_status", "status", "qualified_at"),
        CheckConstraint(
            "status IN ('qualified', 'excluded')",
            name="ck_source_lead_qualification_status",
        ),
        CheckConstraint(
            "requested_notional_usd > 0",
            name="ck_source_lead_qualification_notional",
        ),
        CheckConstraint(
            "identity_registry_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_source_lead_qualification_registry_fingerprint",
        ),
        CheckConstraint(
            "qualification_version != 'source_lead_qualified_capture_v1' OR "
            "(identity_registry_version = 'source_lead_identity_registry_v1' AND "
            "identity_registry_fingerprint = "
            "'31604214fa148d3f86562a212fdc935029c82a7a4959a7b5001b6bd5637ff7f8')",
            name="ck_source_lead_qualification_v1_registry_contract",
        ),
        CheckConstraint(
            "qualification_version != 'source_lead_qualified_capture_v2' OR "
            "(identity_registry_version = 'source_lead_identity_registry_v2' AND "
            "identity_registry_fingerprint = "
            "'757fd1327593d07ca27efe17a031ae0eab95bf6998aecc1ec26f0df38667dca0')",
            name="ck_source_lead_qualification_v2_registry_contract",
        ),
        # Mirrors migration 0043 exactly (research/gate-source-lead-registry-
        # activation-v3) -- colleague review, 2026-08-29/30, PR 3 review
        # round: the v1 and v2 registry-contract constraints above were both
        # already mirrored here when each version activated, but the v3 one
        # migration 0043 created was never added to this model. A fresh
        # schema built from Base.metadata.create_all() (rather than through
        # alembic) would silently omit it.
        CheckConstraint(
            "qualification_version != 'source_lead_qualified_capture_v3' OR "
            "(identity_registry_version = 'source_lead_identity_registry_v3' AND "
            "identity_registry_fingerprint = "
            "'9d36c41442261cfe4e608342378e2d83f96c78afd537de682698796e77733236')",
            name="ck_source_lead_qualification_v3_registry_contract",
        ),
        # Mirrors migration 0051 (HYP-012 v4, PR D).
        CheckConstraint(
            "qualification_version != 'source_lead_qualified_capture_v4' OR "
            "(identity_registry_version = 'source_lead_identity_registry_v4' AND "
            "identity_registry_fingerprint = "
            "'7d5f635a4ed02013ad3bd5fb7bd118f5b80979427bf059a130279fa2c3bee189')",
            name="ck_source_lead_qualification_v4_registry_contract",
        ),
        CheckConstraint(
            "(status = 'qualified' AND canonical_asset_id IS NOT NULL "
            "AND selected_target_exchange IS NOT NULL "
            "AND selected_round_trip_impact_bps IS NOT NULL) OR "
            "(status = 'excluded' AND selected_target_exchange IS NULL "
            "AND selected_round_trip_impact_bps IS NULL)",
            name="ck_source_lead_qualification_selection",
        ),
        {"schema": "app"},
    )


_EXIT_OUTCOMES = (
    "'claimed', 'sampled', 'stale_book', 'fetch_failed', 'missed', "
    "'crashed_after_claim', 'unsupported_venue', 'instrument_unresolved', "
    "'below_min_order', 'insufficient_depth'"
)


class SourceLeadExitObservation(Base, TimestampMixin):
    """Exit-bar-end order book of one qualified HYP-012 v4 episode (migration 0052).

    Diagnostic only; the registered v2 verdict never reads it."""

    __tablename__ = "source_lead_exit_observations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    capture_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("app.source_lead_captures.id", ondelete="CASCADE"),
        nullable=False,
    )
    qualification_version: Mapped[str] = mapped_column(String(64), nullable=False)
    exit_version: Mapped[str] = mapped_column(String(64), nullable=False)
    target_exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    instrument_identity_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    native_symbol: Mapped[str | None] = mapped_column(String(128), nullable=True)
    entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    target_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    timeliness: Mapped[str | None] = mapped_column(String(16), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lateness_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    book_ts_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    book_cts_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    book_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    book_update_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    book_age_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    entry_ask_vwap: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    entry_notional_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    contract_size: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    contract_size_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    qty_step: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    hypothetical_qty_raw: Mapped[Decimal | None] = mapped_column(Numeric(38, 14), nullable=True)
    hypothetical_qty: Mapped[Decimal | None] = mapped_column(Numeric(38, 14), nullable=True)
    best_bid: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    best_ask: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    bid_vwap: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    bid_filled_qty: Mapped[Decimal | None] = mapped_column(Numeric(38, 14), nullable=True)
    spread_bps: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    impact_bps: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    book_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    book_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "ux_source_lead_exit_capture_version",
            "capture_id",
            "qualification_version",
            unique=True,
        ),
        Index("ix_source_lead_exit_outcome", "outcome"),
        CheckConstraint(f"outcome IN ({_EXIT_OUTCOMES})", name="ck_source_lead_exit_outcome"),
        CheckConstraint(
            "timeliness IS NULL OR timeliness IN ('on_time', 'late', 'missed')",
            name="ck_source_lead_exit_timeliness",
        ),
        CheckConstraint("attempts >= 0", name="ck_source_lead_exit_attempts"),
        {"schema": "app"},
    )


class FormalReadClaim(Base):
    """One durable formal-read claim per registered cohort (migration 0054)."""

    __tablename__ = "formal_read_claims"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    study_id: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    cohort_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    database_now: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_ids: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="claimed")
    lease_owner: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    candidate_ids_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    code_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    working_tree_dirty: Mapped[bool] = mapped_column(Boolean, nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "study_id", "contract_version", "cohort_start", name="uq_formal_read_claim_cohort"
        ),
        CheckConstraint("candidate_count >= 0", name="ck_formal_read_claim_count"),
        CheckConstraint(
            "(status = 'claimed' AND completed_at IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL "
            "AND result_fingerprint IS NOT NULL)",
            name="ck_formal_read_claim_status",
        ),
        {"schema": "app"},
    )


_SHADOW_OUTCOMES = (
    "'claimed', 'shadow_recorded', 'broker_rejected', 'stale_book', 'no_book_timestamp', "
    "'below_min_order', 'insufficient_depth', 'instrument_mismatch', 'fetch_failed', "
    "'crashed_after_claim', 'evaluation_error', 'crossed_book', 'instrument_not_tradable', "
    "'below_min_notional', 'above_max_market_qty'"
)


class SourceLeadShadowAttempt(Base):
    """HYP-012 v2 shadow-execution attempt per qualified episode (migration 0055)."""

    __tablename__ = "source_lead_shadow_attempts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    capture_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("app.source_lead_captures.id", ondelete="CASCADE"),
        nullable=False,
    )
    qualification_version: Mapped[str] = mapped_column(String(64), nullable=False)
    shadow_version: Mapped[str] = mapped_column(String(64), nullable=False)
    native_symbol: Mapped[str | None] = mapped_column(String(128), nullable=True)
    instrument_identity_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_first_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    qualified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    late: Mapped[bool] = mapped_column(Boolean, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    quote_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    quote_received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    book_ts_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    book_age_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gate_to_seen_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    detect_latency_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    from_qualified_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    process_latency_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    quote_latency_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    qty_step: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    min_order_qty: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    min_notional_usd: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    max_market_qty: Mapped[Decimal | None] = mapped_column(Numeric(38, 14), nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(38, 14), nullable=True)
    send_qty_vwap: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    send_notional_usd: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    capture_ask_vwap: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    send_ask_vwap: Mapped[Decimal | None] = mapped_column(Numeric(30, 14), nullable=True)
    quote_change_bps: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    decision_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        Index(
            "ux_source_lead_shadow_capture_version",
            "capture_id",
            "qualification_version",
            unique=True,
        ),
        CheckConstraint(f"outcome IN ({_SHADOW_OUTCOMES})", name="ck_source_lead_shadow_outcome"),
        CheckConstraint("attempts >= 0", name="ck_source_lead_shadow_attempts"),
        {"schema": "app"},
    )
