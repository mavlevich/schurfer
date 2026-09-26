"""source lead exit observations (HYP-012 v2 exit-book diagnostic)

Revision ID: 0052
Revises: 0051
Create Date: 2026-09-26

One row per qualified HYP-012 v4 episode: the Bybit order book of the entered
instrument sampled at the end of the cohort's own exit bar, so the v2 exit
proxy (OHLCV close plus 15 bps) can later be calibrated against a real book.
Diagnostic only: nothing in the registered v2 verdict reads this table.
A row is inserted as `claimed` before the network request, so a crash between
request and write can never be retried into a different, later quote.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OUTCOMES = (
    "'claimed', 'sampled', 'stale_book', 'fetch_failed', 'missed', "
    "'crashed_after_claim', 'unsupported_venue', 'instrument_unresolved', "
    "'below_min_order', 'insufficient_depth'"
)


def upgrade() -> None:
    op.create_table(
        "source_lead_exit_observations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "capture_id",
            sa.BigInteger(),
            sa.ForeignKey("app.source_lead_captures.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("qualification_version", sa.String(64), nullable=False),
        sa.Column("exit_version", sa.String(64), nullable=False),
        sa.Column("target_exchange", sa.String(32), nullable=False),
        sa.Column("instrument_identity_key", sa.String(512), nullable=True),
        sa.Column("native_symbol", sa.String(128), nullable=True),
        sa.Column("entry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("timeliness", sa.String(16), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lateness_ms", sa.BigInteger(), nullable=True),
        sa.Column("book_ts_ms", sa.BigInteger(), nullable=True),
        sa.Column("book_cts_ms", sa.BigInteger(), nullable=True),
        sa.Column("book_seq", sa.BigInteger(), nullable=True),
        sa.Column("book_update_id", sa.BigInteger(), nullable=True),
        sa.Column("book_age_ms", sa.BigInteger(), nullable=True),
        sa.Column("entry_ask_vwap", sa.Numeric(30, 14), nullable=True),
        sa.Column("entry_notional_usd", sa.Numeric(18, 4), nullable=True),
        sa.Column("contract_size", sa.Numeric(30, 14), nullable=True),
        sa.Column("contract_size_source", sa.String(32), nullable=True),
        sa.Column("qty_step", sa.Numeric(30, 14), nullable=True),
        sa.Column("hypothetical_qty_raw", sa.Numeric(38, 14), nullable=True),
        sa.Column("hypothetical_qty", sa.Numeric(38, 14), nullable=True),
        sa.Column("best_bid", sa.Numeric(30, 14), nullable=True),
        sa.Column("best_ask", sa.Numeric(30, 14), nullable=True),
        sa.Column("bid_vwap", sa.Numeric(30, 14), nullable=True),
        sa.Column("bid_filled_qty", sa.Numeric(38, 14), nullable=True),
        sa.Column("spread_bps", sa.Numeric(18, 4), nullable=True),
        sa.Column("impact_bps", sa.Numeric(18, 4), nullable=True),
        sa.Column("book_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("book_sha256", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(f"outcome IN ({OUTCOMES})", name="ck_source_lead_exit_outcome"),
        sa.CheckConstraint(
            "timeliness IS NULL OR timeliness IN ('on_time', 'late', 'missed')",
            name="ck_source_lead_exit_timeliness",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_source_lead_exit_attempts"),
        schema="app",
    )
    op.create_index(
        "ux_source_lead_exit_capture_version",
        "source_lead_exit_observations",
        ["capture_id", "qualification_version"],
        unique=True,
        schema="app",
    )
    op.create_index(
        "ix_source_lead_exit_outcome",
        "source_lead_exit_observations",
        ["outcome"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("source_lead_exit_observations", schema="app")
