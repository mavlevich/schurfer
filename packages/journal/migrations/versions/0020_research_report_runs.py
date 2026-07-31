"""add research report run registry

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_report_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("contract", sa.String(length=64), nullable=False),
        sa.Column("report_version", sa.String(length=64), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dataset_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dataset_until_exclusive", sa.DateTime(timezone=True), nullable=False),
        sa.Column("code_revision", sa.String(length=64), nullable=False),
        sa.Column("working_tree_dirty", sa.Boolean(), nullable=False),
        sa.Column("decision_input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("market_path_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("verdict", sa.String(length=64), nullable=False),
        sa.Column("eligible_episodes", sa.Integer(), nullable=False),
        sa.Column("asset_clusters", sa.Integer(), nullable=False),
        sa.Column("calendar_weeks", sa.Integer(), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "dataset_since < dataset_until_exclusive",
            name="ck_research_report_runs_window",
        ),
        sa.CheckConstraint(
            "eligible_episodes >= 0 AND asset_clusters >= 0 AND calendar_weeks >= 0",
            name="ck_research_report_runs_counts",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_index(
        "ix_research_report_runs_contract_generated",
        "research_report_runs",
        ["contract", sa.text("generated_at DESC")],
        unique=False,
        schema="app",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_report_runs_contract_generated",
        table_name="research_report_runs",
        schema="app",
    )
    op.drop_table("research_report_runs", schema="app")
