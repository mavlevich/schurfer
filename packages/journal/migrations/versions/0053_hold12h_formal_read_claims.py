"""durable one-read claim for the HYP-015 hold12h formal verdict

Revision ID: 0053
Revises: 0052
Create Date: 2026-09-25

The hold12h verdict is read exactly once, at a pre-declared decision-time prefix. A
claim tied to a chosen output directory does not enforce that: a second run with another
directory would read the returns again. This table is the durable claim, inserted and
committed BEFORE any return is read. It is unique per cohort (contract version + both
frozen bounds), deliberately NOT per contract sha: otherwise editing any field would
produce a new sha and allow a second read of the same cohort. The sha is stored for audit.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hold12h_formal_read_claims",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
        sa.Column("contract_sha256", sa.String(length=64), nullable=False),
        sa.Column("cohort_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_prefix_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("code_revision", sa.String(length=64), nullable=False),
        sa.Column("working_tree_dirty", sa.Boolean(), nullable=False),
        sa.Column("output_dir", sa.Text(), nullable=False),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "contract_version",
            "cohort_start",
            "decision_prefix_end",
            name="uq_hold12h_formal_read_claim_cohort",
        ),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("hold12h_formal_read_claims", schema="app")
