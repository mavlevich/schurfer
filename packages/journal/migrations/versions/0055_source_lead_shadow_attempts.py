"""source lead shadow execution attempts (HYP-012 v2)

Revision ID: 0055
Revises: 0054
Create Date: 2026-09-26

One row per qualified HYP-012 v4 episode seen by the execution service in SHADOW
mode: the full funnel (skips included) and the timing chain from capture to a fresh
Bybit quote at the intended send time. Claimed before the quote request, so a crash
or a slow decision write can never re-request a different quote for the same episode.
Valid intents are also recorded as trade_decisions through ShadowBroker. No order is
ever placed and no return is computed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OUTCOMES = (
    "'claimed', 'shadow_recorded', 'broker_rejected', 'stale_book', 'no_book_timestamp', "
    "'below_min_order', 'insufficient_depth', 'instrument_mismatch', 'fetch_failed', "
    "'crashed_after_claim'"
)


def upgrade() -> None:
    op.create_table(
        "source_lead_shadow_attempts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "capture_id",
            sa.BigInteger(),
            sa.ForeignKey("app.source_lead_captures.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("qualification_version", sa.String(64), nullable=False),
        sa.Column("shadow_version", sa.String(64), nullable=False),
        sa.Column("native_symbol", sa.String(128), nullable=True),
        sa.Column("instrument_identity_key", sa.String(512), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("qualified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("late", sa.Boolean(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quote_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quote_received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("book_ts_ms", sa.BigInteger(), nullable=True),
        sa.Column("book_age_ms", sa.BigInteger(), nullable=True),
        sa.Column("detect_latency_ms", sa.BigInteger(), nullable=False),
        sa.Column("from_qualified_ms", sa.BigInteger(), nullable=False),
        sa.Column("process_latency_ms", sa.BigInteger(), nullable=True),
        sa.Column("quote_latency_ms", sa.BigInteger(), nullable=True),
        sa.Column("qty_step", sa.Numeric(30, 14), nullable=True),
        sa.Column("min_order_qty", sa.Numeric(30, 14), nullable=True),
        sa.Column("quantity", sa.Numeric(38, 14), nullable=True),
        sa.Column("capture_ask_vwap", sa.Numeric(30, 14), nullable=True),
        sa.Column("send_ask_vwap", sa.Numeric(30, 14), nullable=True),
        sa.Column("quote_change_bps", sa.Numeric(18, 4), nullable=True),
        sa.Column("decision_id", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(f"outcome IN ({OUTCOMES})", name="ck_source_lead_shadow_outcome"),
        sa.CheckConstraint("attempts >= 0", name="ck_source_lead_shadow_attempts"),
        schema="app",
    )
    op.create_index(
        "ux_source_lead_shadow_capture_version",
        "source_lead_shadow_attempts",
        ["capture_id", "qualification_version"],
        unique=True,
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("source_lead_shadow_attempts", schema="app")
