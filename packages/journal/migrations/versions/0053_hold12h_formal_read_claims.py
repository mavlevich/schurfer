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

A claim is resumable, not a dead end: it pins the ordered WATCH ids of the cohort (read
without any return) and, before any verdict is computed, the digest of a write-once
snapshot of every input. Only one run holds the lease at a time. A run that crashed after
the claim is resumed by a later run once its lease expired, on the SAME contract sha,
WATCH ids and inputs snapshot. Every attempt writes its own immutable artifact; only the
lease owner completes the claim (naming its artifact) and then publishes the result.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

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
        sa.Column("watch_ids", postgresql.JSONB(), nullable=False),
        sa.Column("watch_ids_sha256", sa.String(length=64), nullable=False),
        sa.Column("inputs_digest", sa.String(length=64), nullable=True),
        # Outcome-blind coverage the claim was taken on, and whether the operator
        # explicitly accepted it being incomplete.
        sa.Column("coverage_closed", sa.Integer(), nullable=False),
        sa.Column("coverage_funding_covered", sa.Integer(), nullable=False),
        sa.Column("coverage_accounting_complete", sa.Integer(), nullable=False),
        sa.Column("coverage_open_positions", sa.Integer(), nullable=False),
        sa.Column("accepted_incomplete_coverage", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="claimed", nullable=False),
        sa.Column("lease_owner", sa.String(length=64), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("artifact_name", sa.Text(), nullable=True),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "status IN ('claimed', 'completed')", name="ck_hold12h_formal_read_claim_status"
        ),
        sa.CheckConstraint(
            "(status = 'completed') = (completed_at IS NOT NULL AND artifact_sha256 IS NOT NULL)",
            name="ck_hold12h_formal_read_claim_completed_at",
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
