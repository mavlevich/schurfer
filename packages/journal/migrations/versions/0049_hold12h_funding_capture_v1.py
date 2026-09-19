"""prospective per-instrument funding-settlement capture for the HYP-015 hold12h verdict

Revision ID: 0049
Revises: 0048
Create Date: 2026-09-19

The hold12h verdict's actual-funding contract needs real per-instrument settlement
events over an arbitrary hold interval, which the pump-anchored funding tables cannot
give (biased, conditional coverage). This adds a small PROSPECTIVE capture for the exact
HYP-015 instruments:

  * ``hold12h_funding_settlements`` -- one row per settlement event, unique on
    ``(exchange, native_market_id, settlement_at, source_version)`` so a duplicate is a
    hard integrity error, not a silent double-charge; the native payload is kept for audit.
  * ``hold12h_funding_coverage_runs`` -- one row per resolver fetch of an instrument
    window with its requested bounds and TERMINAL status, so "the interval was fully
    covered" is a recorded fact (a settlement gap is proven, not assumed).

Nothing here reads returns; it captures the cost side the verdict later charges.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hold12h_funding_settlements",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("native_market_id", sa.String(length=128), nullable=False),
        sa.Column("unified_symbol", sa.String(length=128), nullable=False),
        sa.Column("market_type", sa.String(length=16), nullable=False),
        # The venue's funding settlement instant (unified CCXT ms -> tz-aware).
        sa.Column("settlement_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("funding_rate", sa.Double(), nullable=False),
        # Provenance: the venue event time (== settlement here), when the resolver observed
        # the row, and when it fetched the page.
        sa.Column("source_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("native_payload", JSONB(), nullable=False),
        sa.Column("source_version", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("funding_rate = funding_rate", name="ck_hold12h_funding_rate_finite"),
        # The dedup identity: one settlement per instrument+instant+source version.
        sa.UniqueConstraint(
            "exchange",
            "native_market_id",
            "settlement_at",
            "source_version",
            name="uq_hold12h_funding_settlement",
        ),
        schema="app",
    )
    # The dominant read: settlements for one instrument inside a hold window.
    op.create_index(
        "ix_hold12h_funding_settlement_instrument_time",
        "hold12h_funding_settlements",
        ["exchange", "native_market_id", "settlement_at"],
        unique=False,
        schema="app",
    )

    op.create_table(
        "hold12h_funding_coverage_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("native_market_id", sa.String(length=128), nullable=False),
        sa.Column("unified_symbol", sa.String(length=128), nullable=False),
        sa.Column("market_type", sa.String(length=16), nullable=False),
        sa.Column("requested_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_until", sa.DateTime(timezone=True), nullable=False),
        # Terminal status of the fetch: only 'complete' proves full coverage of the window;
        # 'integrity_conflict' BLOCKS any overlapping 'complete' run until a human resolves it.
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("settlements_written", sa.Integer(), nullable=False, server_default="0"),
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
            "status IN ('complete', 'fetch_failed', 'invalid_response', "
            "'pagination_exhausted', 'incomplete', 'integrity_conflict')",
            name="ck_hold12h_funding_coverage_status",
        ),
        sa.CheckConstraint(
            "requested_until > requested_since", name="ck_hold12h_funding_coverage_window"
        ),
        schema="app",
    )
    # Read the newest COMPLETE run covering an instrument's requested_until.
    op.create_index(
        "ix_hold12h_funding_coverage_instrument",
        "hold12h_funding_coverage_runs",
        ["exchange", "native_market_id", "requested_until"],
        unique=False,
        schema="app",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_hold12h_funding_coverage_instrument",
        table_name="hold12h_funding_coverage_runs",
        schema="app",
    )
    op.drop_table("hold12h_funding_coverage_runs", schema="app")
    op.drop_index(
        "ix_hold12h_funding_settlement_instrument_time",
        table_name="hold12h_funding_settlements",
        schema="app",
    )
    op.drop_table("hold12h_funding_settlements", schema="app")
