"""add partial index on pump_events for stats query by base over closed episodes

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Supports GET /api/pumps/{base}/stats: WHERE base = $1 AND closed_at IS NOT NULL.
    # Partial index excludes open episodes, keeping it small as history grows.
    op.create_index(
        "ix_pump_events_base_closed",
        "pump_events",
        ["base"],
        schema="app",
        postgresql_where=sa.text("closed_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pump_events_base_closed",
        table_name="pump_events",
        schema="app",
    )
