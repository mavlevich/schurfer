"""Record book-side costs at the HYP-012 v2 shadow send time.

Revision ID: 0056
Revises: 0055
Create Date: 2026-09-29

These nullable diagnostics do not change the shadow decision or formal v2 read.
The capture version distinguishes an old worker from a new worker with an
unusable book even when every quote-cost field is NULL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "source_lead_shadow_attempts"
_SCHEMA = "app"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("send_cost_capture_version", sa.String(64), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE, sa.Column("send_spread_bps", sa.Numeric(18, 4), nullable=True), schema=_SCHEMA
    )
    op.add_column(
        _TABLE,
        sa.Column("send_notional_ask_impact_bps", sa.Numeric(18, 4), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column("send_qty_ask_impact_bps", sa.Numeric(18, 4), nullable=True),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "send_qty_ask_impact_bps", schema=_SCHEMA)
    op.drop_column(_TABLE, "send_notional_ask_impact_bps", schema=_SCHEMA)
    op.drop_column(_TABLE, "send_spread_bps", schema=_SCHEMA)
    op.drop_column(_TABLE, "send_cost_capture_version", schema=_SCHEMA)
