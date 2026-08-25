from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, LargeBinary, String, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ResearchCohortRegistration(Base):
    """Immutable first-writer-wins boundary for a prospective cohort."""

    __tablename__ = "research_cohort_registrations"

    cohort_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    strategy_name: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    contract_sha256: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    runtime_policy_sha256: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    cohort_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "octet_length(contract_sha256) = 32",
            name="ck_research_cohort_contract_sha256_length",
        ),
        CheckConstraint(
            "octet_length(runtime_policy_sha256) = 32",
            name="ck_research_cohort_runtime_policy_sha256_length",
        ),
        {"schema": "app"},
    )
