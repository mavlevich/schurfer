"""add pump event entry qualification timestamp

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pump_events",
        sa.Column("entry_qualified_at", sa.DateTime(timezone=True), nullable=True),
        schema="app",
    )
    # Every pre-split event was created only after crossing the 30% entry floor.
    op.execute(
        """
        UPDATE app.pump_events
        SET entry_qualified_at = first_seen_at
        WHERE entry_qualified_at IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("pump_events", "entry_qualified_at", schema="app")
