"""add versioned exchange-instrument identity to pump sources

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = (
        sa.Column("identity_key", sa.String(512), nullable=True),
        sa.Column("market_id", sa.String(128), nullable=True),
        sa.Column("unified_symbol", sa.String(128), nullable=True),
        sa.Column("display_name", sa.String(256), nullable=True),
        sa.Column("market_type", sa.String(16), nullable=True),
        sa.Column("base_asset", sa.String(64), nullable=True),
        sa.Column("quote_asset", sa.String(32), nullable=True),
        sa.Column("settle_asset", sa.String(32), nullable=True),
        sa.Column("contract_size", sa.Double(), nullable=True),
        sa.Column("onboarded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_ticker_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ticker_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "identity_conflict",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    for column in columns:
        op.add_column("pump_event_sources", column, schema="app")

    op.create_index(
        "ix_pump_event_sources_identity_key_first_seen",
        "pump_event_sources",
        ["identity_key", "first_seen_at"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pump_event_sources_identity_key_first_seen",
        table_name="pump_event_sources",
        schema="app",
    )
    for column in (
        "identity_conflict",
        "last_ticker_at",
        "first_ticker_at",
        "onboarded_at",
        "contract_size",
        "settle_asset",
        "quote_asset",
        "base_asset",
        "market_type",
        "display_name",
        "unified_symbol",
        "market_id",
        "identity_key",
    ):
        op.drop_column("pump_event_sources", column, schema="app")
