"""add composite index on oi_snapshots for per-exchange DISTINCT ON queries

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Supports DISTINCT ON (exchange) ORDER BY exchange, recorded_at DESC/ASC
    # used by /api/pumps/{base}/signals to compute current and baseline OI totals.
    op.create_index(
        "ix_oi_snapshots_event_exchange_recorded_at",
        "oi_snapshots",
        ["event_id", "exchange", sa.text("recorded_at DESC")],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_oi_snapshots_event_exchange_recorded_at",
        table_name="oi_snapshots",
        schema="app",
    )
