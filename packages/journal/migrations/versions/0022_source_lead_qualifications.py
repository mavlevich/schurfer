"""add source lead identity qualifications

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_lead_qualifications",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("capture_id", sa.BigInteger(), nullable=False),
        sa.Column("qualification_version", sa.String(length=64), nullable=False),
        sa.Column("identity_registry_version", sa.String(length=64), nullable=False),
        sa.Column("identity_registry_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("venue_selector_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("canonical_asset_id", sa.String(length=128), nullable=True),
        sa.Column("selected_target_exchange", sa.String(length=32), nullable=True),
        sa.Column("selected_round_trip_impact_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("requested_notional_usd", sa.Numeric(18, 4), nullable=False),
        sa.Column("qualified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('qualified', 'excluded')",
            name="ck_source_lead_qualification_status",
        ),
        sa.CheckConstraint(
            "requested_notional_usd > 0",
            name="ck_source_lead_qualification_notional",
        ),
        sa.CheckConstraint(
            "identity_registry_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_source_lead_qualification_registry_fingerprint",
        ),
        sa.CheckConstraint(
            "qualification_version != 'source_lead_qualified_capture_v1' OR "
            "(identity_registry_version = 'source_lead_identity_registry_v1' AND "
            "identity_registry_fingerprint = "
            "'31604214fa148d3f86562a212fdc935029c82a7a4959a7b5001b6bd5637ff7f8')",
            name="ck_source_lead_qualification_v1_registry_contract",
        ),
        sa.CheckConstraint(
            "(status = 'qualified' AND canonical_asset_id IS NOT NULL "
            "AND selected_target_exchange IS NOT NULL "
            "AND selected_round_trip_impact_bps IS NOT NULL) OR "
            "(status = 'excluded' AND selected_target_exchange IS NULL "
            "AND selected_round_trip_impact_bps IS NULL)",
            name="ck_source_lead_qualification_selection",
        ),
        sa.ForeignKeyConstraint(
            ["capture_id"], ["app.source_lead_captures.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_index(
        "ux_source_lead_qualification_capture_version",
        "source_lead_qualifications",
        ["capture_id", "qualification_version"],
        unique=True,
        schema="app",
    )
    op.create_index(
        "ix_source_lead_qualification_status",
        "source_lead_qualifications",
        ["status", "qualified_at"],
        unique=False,
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("source_lead_qualifications", schema="app")
