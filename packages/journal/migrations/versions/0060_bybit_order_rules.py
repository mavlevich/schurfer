"""Bybit linear order rules, captured hourly for the HYP-015 executable portfolio.

Revision ID: 0060
Revises: 0059
Create Date: 2026-10-11

The HYP-015 confirmation design (docs/research/hyp015-confirmation-stage-v1.md) sizes
each paper entry as a real USD 50 order: the quantity rounded down to the venue's step,
refused below its minimum quantity or order value. Paper rows do not carry those rules,
so they are captured beside them, outcome-blind:

  * ``bybit_order_rule_defs`` -- one row per distinct rule set of one instrument, keyed
    by the SHA-256 of the venue's raw instruments-info row, so an unchanged instrument
    costs nothing per capture; the raw row is kept for audit;
  * ``bybit_order_rule_captures`` -- one row per capture pass: its time, its terminal
    status and, for a complete pass, every listed symbol mapped to its rule key. A
    decision is sized only from a complete capture shortly before it; anything else is
    an unverified entry, never a feasible one.

Nothing here reads a paper row or a return.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bybit_order_rule_defs",
        sa.Column("rule_sha256", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("qty_step", sa.Numeric(), nullable=True),
        sa.Column("min_order_qty", sa.Numeric(), nullable=True),
        sa.Column("max_mkt_order_qty", sa.Numeric(), nullable=True),
        sa.Column("min_notional_usd", sa.Numeric(), nullable=True),
        sa.Column("tick_size", sa.Numeric(), nullable=True),
        sa.Column("raw", JSONB(), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("rule_sha256"),
        sa.CheckConstraint("rule_sha256 ~ '^[0-9a-f]{64}$'", name="ck_bybit_order_rule_sha"),
        sa.CheckConstraint(
            "(qty_step IS NULL OR qty_step > 0) AND (min_order_qty IS NULL OR min_order_qty > 0)"
            " AND (max_mkt_order_qty IS NULL OR max_mkt_order_qty > 0)"
            " AND (min_notional_usd IS NULL OR min_notional_usd > 0)"
            " AND (tick_size IS NULL OR tick_size > 0)",
            name="ck_bybit_order_rule_positive",
        ),
        schema="app",
    )
    op.create_table(
        "bybit_order_rule_captures",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("instrument_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("members", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("source_version", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('complete', 'failed')", name="ck_bybit_order_rule_capture_status"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(members) = 'object'", name="ck_bybit_order_rule_capture_members"
        ),
        sa.CheckConstraint(
            "status = 'complete' OR members = '{}'::jsonb",
            name="ck_bybit_order_rule_capture_failed_empty",
        ),
        schema="app",
    )
    # The lookup: the newest complete capture at or before a decision.
    op.create_index(
        "ix_bybit_order_rule_captures_complete",
        "bybit_order_rule_captures",
        ["source_version", "captured_at"],
        unique=False,
        schema="app",
        postgresql_where=sa.text("status = 'complete'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_bybit_order_rule_captures_complete",
        table_name="bybit_order_rule_captures",
        schema="app",
    )
    op.drop_table("bybit_order_rule_captures", schema="app")
    op.drop_table("bybit_order_rule_defs", schema="app")
