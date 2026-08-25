"""durable pre-outcome registration for prospective research cohorts

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_cohort_registrations",
        sa.Column("cohort_key", sa.String(length=128), nullable=False),
        sa.Column("strategy_name", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.String(length=32), nullable=False),
        sa.Column("contract_sha256", sa.LargeBinary(length=32), nullable=False),
        sa.Column("runtime_policy_sha256", sa.LargeBinary(length=32), nullable=False),
        sa.Column("cohort_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(contract_sha256) = 32",
            name="ck_research_cohort_contract_sha256_length",
        ),
        sa.CheckConstraint(
            "octet_length(runtime_policy_sha256) = 32",
            name="ck_research_cohort_runtime_policy_sha256_length",
        ),
        sa.PrimaryKeyConstraint("cohort_key"),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("research_cohort_registrations", schema="app")
