"""Persist bounded metadata for successful frozen research-report runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from schurfer_journal.models import ResearchReportRun
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine

from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True)
class ReportRunRecord:
    contract: str
    report_version: str
    generated_at: datetime
    dataset_since: datetime
    dataset_until_exclusive: datetime
    code_revision: str
    working_tree_dirty: bool
    decision_input_fingerprint: str
    market_path_fingerprint: str
    status: str
    verdict: str
    eligible_episodes: int
    asset_clusters: int
    calendar_weeks: int
    summary: dict[str, Any]

    def __post_init__(self) -> None:
        strings = (
            self.contract,
            self.report_version,
            self.code_revision,
            self.decision_input_fingerprint,
            self.market_path_fingerprint,
            self.status,
            self.verdict,
        )
        if any(not value.strip() for value in strings):
            raise ValueError("report registry strings must not be empty")
        if self.dataset_since >= self.dataset_until_exclusive:
            raise ValueError("report registry window must be positive")
        if min(self.eligible_episodes, self.asset_clusters, self.calendar_weeks) < 0:
            raise ValueError("report registry counts must be non-negative")


def report_run_statement(record: ReportRunRecord):  # type: ignore[no-untyped-def]
    """Build the insert independently so SQL shape can be unit tested."""
    return insert(ResearchReportRun).values(
        contract=record.contract,
        report_version=record.report_version,
        generated_at=record.generated_at,
        dataset_since=record.dataset_since,
        dataset_until_exclusive=record.dataset_until_exclusive,
        code_revision=record.code_revision,
        working_tree_dirty=record.working_tree_dirty,
        decision_input_fingerprint=record.decision_input_fingerprint,
        market_path_fingerprint=record.market_path_fingerprint,
        status=record.status,
        verdict=record.verdict,
        eligible_episodes=record.eligible_episodes,
        asset_clusters=record.asset_clusters,
        calendar_weeks=record.calendar_weeks,
        summary=record.summary,
    )


async def record_report_run(database_url: str, record: ReportRunRecord) -> None:
    """Append metadata only after a report has completed successfully."""
    engine = create_async_engine(async_database_url(database_url))
    try:
        async with engine.begin() as connection:
            await connection.execute(report_run_statement(record))
    finally:
        await engine.dispose()
