"""Durable emergency close tasks for live positions that must be flattened.

Revision ID: 0061
Revises: 0060
Create Date: 2026-10-30

docs/engineering/live-entry-failure-paths-design-v1.md, section 1. One row per
position the execution service must flatten after a failed stop placement or a late
fill. Its close orders stay ``close`` rows in ``app.live_order_attempts``, linked by
the new ``task_id``, so a close is never journalled in a second list.

The database, not process memory, enforces:

  * one task per source entry attempt, ever;
  * one active task per exact identity;
  * the executor claim (``claim_owner``, ``claim_expires_at``), so two executors
    never own one task.

Nothing creates a task yet. The stop placement and late fill paths that do land in
later PRs.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE = "status IN ('requested', 'sending', 'unknown', 'manual_required')"


def upgrade() -> None:
    op.create_table(
        "live_emergency_closes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_attempt_id", sa.BigInteger(), nullable=False),
        sa.Column("entry_client_order_id", sa.String(length=32), nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("base", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("native_market_id", sa.String(length=64), nullable=False),
        sa.Column("market_type", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("trade_id", sa.BigInteger(), nullable=True),
        sa.Column("owned_quantity", sa.Numeric(18, 8), nullable=False),
        sa.Column("owned_remainder", sa.Numeric(18, 8), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("rejected_sends", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claim_owner", sa.String(length=64), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.Column("confirmed_flat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accounted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "entry_client_order_id ~ '^[0-9a-f]{32}$'",
            name="ck_live_emergency_closes_entry_cid",
        ),
        sa.CheckConstraint("side IN ('long', 'short')", name="ck_live_emergency_closes_side"),
        sa.CheckConstraint("owned_quantity > 0", name="ck_live_emergency_closes_owned"),
        sa.CheckConstraint(
            "owned_remainder >= 0 AND owned_remainder <= owned_quantity",
            name="ck_live_emergency_closes_remainder",
        ),
        sa.CheckConstraint(
            "reason IN ('stop_placement_failed', 'late_fill_flattened')",
            name="ck_live_emergency_closes_reason",
        ),
        sa.CheckConstraint(
            "status IN ('requested', 'sending', 'unknown', 'confirmed_flat', 'accounted', "
            "'manual_required')",
            name="ck_live_emergency_closes_status",
        ),
        sa.CheckConstraint("rejected_sends >= 0", name="ck_live_emergency_closes_rejected"),
        sa.ForeignKeyConstraint(
            ["source_attempt_id"], ["app.live_order_attempts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["trade_id"], ["app.trades.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_index(
        "ux_live_emergency_closes_source_attempt",
        "live_emergency_closes",
        ["source_attempt_id"],
        unique=True,
        schema="app",
    )
    op.create_index(
        "ux_live_emergency_closes_active_identity",
        "live_emergency_closes",
        ["exchange", "native_market_id", "market_type", "side"],
        unique=True,
        schema="app",
        postgresql_where=sa.text(_ACTIVE),
    )
    op.create_index(
        "ix_live_emergency_closes_status",
        "live_emergency_closes",
        ["status"],
        schema="app",
    )

    op.add_column(
        "live_order_attempts",
        sa.Column("task_id", sa.BigInteger(), nullable=True),
        schema="app",
    )
    op.create_foreign_key(
        "fk_live_order_attempts_task_id",
        "live_order_attempts",
        "live_emergency_closes",
        ["task_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_live_order_attempts_task_close",
        "live_order_attempts",
        "task_id IS NULL OR operation = 'close'",
        schema="app",
    )
    op.create_index(
        "ix_live_order_attempts_task_id",
        "live_order_attempts",
        ["task_id"],
        schema="app",
        postgresql_where=sa.text("task_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM app.live_emergency_closes) THEN
                RAISE EXCEPTION 'cannot downgrade 0061 with emergency close evidence';
            END IF;
        END $$
        """
    )
    op.drop_index("ix_live_order_attempts_task_id", table_name="live_order_attempts", schema="app")
    op.drop_constraint(
        "ck_live_order_attempts_task_close", "live_order_attempts", type_="check", schema="app"
    )
    op.drop_constraint(
        "fk_live_order_attempts_task_id", "live_order_attempts", type_="foreignkey", schema="app"
    )
    op.drop_column("live_order_attempts", "task_id", schema="app")
    op.drop_table("live_emergency_closes", schema="app")
