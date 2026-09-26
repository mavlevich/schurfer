"""durable one-read claims for registered research cohorts

Revision ID: 0054
Revises: 0053
Create Date: 2026-09-26

A registered cohort is read exactly once. The claim is inserted and committed
BEFORE any outcome is fetched and is unique per (study, contract version, cohort
start), so a second run refuses instead of producing a second verdict, even when
late rows would change the prefix. It records the candidate set the read used.
First user: HYP-012 forward cohort v2. HYP-015 keeps its own table (0053).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "formal_read_claims",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("study_id", sa.String(64), nullable=False),
        sa.Column("contract_version", sa.String(64), nullable=False),
        sa.Column("cohort_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("database_now", sa.DateTime(timezone=True), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("candidate_ids_sha256", sa.String(64), nullable=False),
        sa.Column("code_revision", sa.String(64), nullable=False),
        sa.Column("working_tree_dirty", sa.Boolean(), nullable=False),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "study_id",
            "contract_version",
            "cohort_start",
            name="uq_formal_read_claim_cohort",
        ),
        sa.CheckConstraint("candidate_count >= 0", name="ck_formal_read_claim_count"),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("formal_read_claims", schema="app")
