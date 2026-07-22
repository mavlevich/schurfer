"""link trade decisions to their pump episode

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trade_decisions",
        sa.Column("pump_event_id", sa.BigInteger(), nullable=True),
        schema="app",
    )
    op.create_foreign_key(
        "fk_trade_decisions_pump_event_id",
        "trade_decisions",
        "pump_events",
        ["pump_event_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_trade_decisions_pump_event_id",
        "trade_decisions",
        ["pump_event_id"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trade_decisions_pump_event_id",
        table_name="trade_decisions",
        schema="app",
    )
    op.drop_constraint(
        "fk_trade_decisions_pump_event_id",
        "trade_decisions",
        schema="app",
        type_="foreignkey",
    )
    op.drop_column("trade_decisions", "pump_event_id", schema="app")
