"""live position reconciliation v1

Revision ID: 0039
Revises: 0038
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "live_reconciliation_incidents",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("incident_key", sa.String(length=128), nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("native_market_id", sa.String(length=64), nullable=False),
        sa.Column("market_type", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("contracts", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("discrepancy_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column(
            "evidence_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("alert_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovery_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'manual_required')",
            name="ck_live_reconciliation_incidents_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )

    op.create_index(
        "ux_live_reconciliation_incidents_key",
        "live_reconciliation_incidents",
        ["incident_key"],
        unique=True,
        schema="app",
    )
    op.create_index(
        "ix_live_reconciliation_incidents_status",
        "live_reconciliation_incidents",
        ["status"],
        schema="app",
    )

    op.add_column(
        "live_order_attempts",
        sa.Column("native_market_id", sa.String(length=64), nullable=True),
        schema="app",
    )
    op.add_column(
        "live_order_attempts",
        sa.Column("market_type", sa.String(length=32), nullable=True),
        schema="app",
    )
    op.add_column(
        "live_order_attempts",
        sa.Column("requested_amount", sa.Numeric(precision=18, scale=8), nullable=True),
        schema="app",
    )
    op.add_column(
        "live_order_attempts",
        sa.Column("reconciliation_timestamp", sa.DateTime(timezone=True), nullable=True),
        schema="app",
    )
    op.add_column(
        "live_order_attempts",
        sa.Column("reconciliation_error", sa.Text(), nullable=True),
        schema="app",
    )

    op.alter_column(
        "live_order_attempts",
        "status",
        type_=sa.String(length=32),
        existing_type=sa.String(length=16),
        schema="app",
    )

    op.execute("ALTER TABLE app.live_order_attempts DROP CONSTRAINT ck_live_order_attempts_status")
    op.execute(
        "ALTER TABLE app.live_order_attempts ADD CONSTRAINT ck_live_order_attempts_status "
        "CHECK (status IN ('pending', 'accepted', 'completed', 'failed', "
        "'submission_unknown', 'no_fill', 'manual_required'))"
    )


def downgrade() -> None:
    # Never reinterpret an ambiguous/partially reconciled order as a plain
    # failure during rollback. The operator must resolve it before an older
    # binary, which cannot understand the state, is allowed to start.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM app.live_order_attempts
                WHERE status NOT IN ('pending', 'accepted', 'completed', 'failed')
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade 0039 with unresolved reconciliation statuses';
            END IF;
        END $$
        """
    )
    op.execute("ALTER TABLE app.live_order_attempts DROP CONSTRAINT ck_live_order_attempts_status")
    op.execute(
        "ALTER TABLE app.live_order_attempts ADD CONSTRAINT ck_live_order_attempts_status "
        "CHECK (status IN ('pending', 'accepted', 'completed', 'failed'))"
    )
    op.alter_column(
        "live_order_attempts",
        "status",
        type_=sa.String(length=16),
        existing_type=sa.String(length=32),
        schema="app",
    )

    op.drop_column("live_order_attempts", "reconciliation_error", schema="app")
    op.drop_column("live_order_attempts", "reconciliation_timestamp", schema="app")
    op.drop_column("live_order_attempts", "requested_amount", schema="app")
    op.drop_column("live_order_attempts", "market_type", schema="app")
    op.drop_column("live_order_attempts", "native_market_id", schema="app")

    op.drop_index(
        "ix_live_reconciliation_incidents_status",
        table_name="live_reconciliation_incidents",
        schema="app",
    )
    op.drop_index(
        "ux_live_reconciliation_incidents_key",
        table_name="live_reconciliation_incidents",
        schema="app",
    )
    op.drop_table("live_reconciliation_incidents", schema="app")
