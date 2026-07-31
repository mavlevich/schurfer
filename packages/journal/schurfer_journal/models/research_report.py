from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ResearchReportRun(Base):
    """Sanitized metadata for one successful frozen research-report run."""

    __tablename__ = "research_report_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    contract: Mapped[str] = mapped_column(String(64), nullable=False)
    report_version: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dataset_since: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dataset_until_exclusive: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    code_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    working_tree_dirty: Mapped[bool] = mapped_column(Boolean, nullable=False)
    decision_input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    market_path_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    verdict: Mapped[str] = mapped_column(String(64), nullable=False)
    eligible_episodes: Mapped[int] = mapped_column(Integer, nullable=False)
    asset_clusters: Mapped[int] = mapped_column(Integer, nullable=False)
    calendar_weeks: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "dataset_since < dataset_until_exclusive",
            name="ck_research_report_runs_window",
        ),
        CheckConstraint(
            "eligible_episodes >= 0 AND asset_clusters >= 0 AND calendar_weeks >= 0",
            name="ck_research_report_runs_counts",
        ),
        Index("ix_research_report_runs_contract_generated", "contract", generated_at.desc()),
        {"schema": "app"},
    )
