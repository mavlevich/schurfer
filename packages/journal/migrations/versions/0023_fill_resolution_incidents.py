"""add fill resolution incidents

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fill_resolution_incidents",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("base", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("order_id", sa.String(length=128), nullable=False),
        sa.Column("trade_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("resolved_price", sa.Numeric(30, 14), nullable=True),
        sa.Column("resolved_source", sa.String(length=32), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovery_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
            "operation IN ('open', 'close')",
            name="ck_fill_resolution_incidents_operation",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'resolving', 'resolved', 'manual_required')",
            name="ck_fill_resolution_incidents_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_fill_resolution_incidents_attempt_count",
        ),
        sa.CheckConstraint(
            "(status IN ('pending', 'resolving', 'manual_required') "
            " AND resolved_at IS NULL AND resolved_price IS NULL AND resolved_source IS NULL) "
            "OR "
            "(status = 'resolved' "
            " AND resolved_at IS NOT NULL AND resolved_price IS NOT NULL "
            " AND resolved_source IS NOT NULL)",
            name="ck_fill_resolution_incidents_resolution",
        ),
        sa.ForeignKeyConstraint(["trade_id"], ["app.trades.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_index(
        "ux_fill_resolution_incidents_exchange_order",
        "fill_resolution_incidents",
        ["exchange", "order_id"],
        unique=True,
        schema="app",
    )
    op.create_index(
        "ix_fill_resolution_incidents_status",
        "fill_resolution_incidents",
        ["status"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("fill_resolution_incidents", schema="app")
