"""add prospective source lead capture

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_lead_captures",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("capture_version", sa.String(length=64), nullable=False),
        sa.Column("source_exchange", sa.String(length=32), nullable=False),
        sa.Column("base", sa.String(length=64), nullable=False),
        sa.Column("source_symbol", sa.String(length=128), nullable=False),
        sa.Column("source_identity_key", sa.String(length=512), nullable=True),
        sa.Column("source_market_id", sa.String(length=128), nullable=True),
        sa.Column("source_occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collector_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capture_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capture_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("eligibility_reason", sa.String(length=64), nullable=False),
        sa.Column("source_change_pct", sa.Numeric(12, 4), nullable=False),
        sa.Column("source_price", sa.Numeric(30, 14), nullable=True),
        sa.Column("source_volume_24h_usd", sa.Numeric(24, 4), nullable=True),
        sa.Column("first_sources", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
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
            "status IN ('collecting', 'complete', 'excluded', 'abandoned')",
            name="ck_source_lead_captures_status",
        ),
        sa.CheckConstraint(
            "source_change_pct >= -5000 AND source_change_pct <= 5000",
            name="ck_source_lead_captures_change",
        ),
        sa.CheckConstraint(
            "(status = 'collecting' AND capture_completed_at IS NULL) OR "
            "(status <> 'collecting' AND capture_completed_at IS NOT NULL)",
            name="ck_source_lead_captures_completion",
        ),
        sa.ForeignKeyConstraint(["event_id"], ["app.pump_events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_index(
        "ux_source_lead_captures_event_version",
        "source_lead_captures",
        ["event_id", "capture_version"],
        unique=True,
        schema="app",
    )
    op.create_index(
        "ix_source_lead_captures_observed",
        "source_lead_captures",
        ["source_first_observed_at"],
        schema="app",
    )
    op.create_index(
        "ix_source_lead_captures_status",
        "source_lead_captures",
        ["status"],
        schema="app",
    )

    op.create_table(
        "source_lead_target_observations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("capture_id", sa.BigInteger(), nullable=False),
        sa.Column("target_exchange", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("eligibility_reason", sa.String(length=64), nullable=False),
        sa.Column("identity_match_method", sa.String(length=32), nullable=False),
        sa.Column("identity_verified", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("requested_notional_usd", sa.Numeric(18, 4), nullable=False),
        sa.Column("instrument", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ticker", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("liquidity", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
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
        sa.CheckConstraint("latency_ms >= 0", name="ck_source_lead_target_latency"),
        sa.CheckConstraint(
            "requested_notional_usd > 0",
            name="ck_source_lead_target_notional",
        ),
        sa.CheckConstraint(
            "status IN ('sampled', 'excluded', 'fetch_failed')",
            name="ck_source_lead_target_status",
        ),
        sa.CheckConstraint(
            "NOT (identity_match_method = 'base_symbol_v1' AND identity_verified)",
            name="ck_source_lead_target_provisional_identity",
        ),
        sa.ForeignKeyConstraint(
            ["capture_id"], ["app.source_lead_captures.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_index(
        "ux_source_lead_target_capture_exchange",
        "source_lead_target_observations",
        ["capture_id", "target_exchange"],
        unique=True,
        schema="app",
    )
    op.create_index(
        "ix_source_lead_target_observed",
        "source_lead_target_observations",
        ["observed_at"],
        schema="app",
    )
    op.create_index(
        "ix_source_lead_target_status",
        "source_lead_target_observations",
        ["status"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("source_lead_target_observations", schema="app")
    op.drop_table("source_lead_captures", schema="app")
