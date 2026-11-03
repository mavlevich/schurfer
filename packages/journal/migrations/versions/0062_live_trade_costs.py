"""Exchange cost evidence for live trades and the accounting generation.

Revision ID: 0062
Revises: 0061
Create Date: 2026-11-03

docs/engineering/live-net-accounting-design-v1.md, sections 5 and 7.

  * ``trade_cost_legs`` -- one row per Bybit execution attributed to a live trade:
    each fill with its fee, and each funding settlement. Unique on
    ``(exchange, exec_id)``, so an execution is never counted twice. The cost worker
    writes the rows in the same transaction as the trade's final figures.
  * ``pnl_generation`` -- one row holding a number that every transaction changing an
    input of the PnL tracker increments before it commits. The tracker publishes the
    number it read with its readiness lease, and an entry is admitted only while the
    number is still the same, so a tick that read the journal before a close or a cost
    settlement can never admit an entry.

Triggers increment the number, so no writer can forget it. A sequence would not do:
its value is visible before the writing transaction commits. The tracker's inputs, and
the changes that increment it:

  * ``trades``: a non-paper row whose status, PnL, exit time, accounting version or
    status, or setup context changes, and a non-paper row deleted or inserted closed.
    An open trade inserted changes no input, and the frequent updates of open trades
    (excursions, stops) touch none of these columns;
  * ``trade_close_fills``: any row inserted, changed or deleted;
  * ``fill_resolution_incidents``: a row inserted or deleted, or its status changed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BUMP = "UPDATE app.pnl_generation SET value = value + 1, updated_at = now() WHERE id = 1"
_NOT_PAPER = "COALESCE({row}.setup_context->>'paper', 'false') <> 'true'"


def upgrade() -> None:
    op.create_table(
        "trade_cost_legs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("trade_id", sa.BigInteger(), nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("exec_id", sa.String(length=128), nullable=False),
        sa.Column("exec_type", sa.String(length=16), nullable=False),
        sa.Column("order_id", sa.String(length=128), nullable=True),
        sa.Column("client_order_id", sa.String(length=64), nullable=True),
        sa.Column("side", sa.String(length=8), nullable=True),
        sa.Column("quantity", sa.Numeric(28, 12), nullable=False),
        sa.Column("price", sa.Numeric(28, 12), nullable=True),
        sa.Column("value_usd", sa.Numeric(28, 12), nullable=True),
        sa.Column("fee_usd", sa.Numeric(28, 12), nullable=False),
        sa.Column("fee_currency", sa.String(length=16), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "exec_type IN ('trade', 'funding')", name="ck_trade_cost_legs_exec_type"
        ),
        sa.CheckConstraint("quantity >= 0", name="ck_trade_cost_legs_quantity"),
        sa.ForeignKeyConstraint(["trade_id"], ["app.trades.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_index(
        "ux_trade_cost_legs_exec",
        "trade_cost_legs",
        ["exchange", "exec_id"],
        unique=True,
        schema="app",
    )
    op.create_index("ix_trade_cost_legs_trade", "trade_cost_legs", ["trade_id"], schema="app")

    op.create_table(
        "pnl_generation",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_pnl_generation_single_row"),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.execute("INSERT INTO app.pnl_generation (id, value) VALUES (1, 0)")

    op.execute(
        f"""
        CREATE FUNCTION app.bump_pnl_generation_trades() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status = 'closed' AND {_NOT_PAPER.format(row="NEW")} THEN
                    {_BUMP};
                END IF;
            ELSIF TG_OP = 'DELETE' THEN
                IF {_NOT_PAPER.format(row="OLD")} THEN
                    {_BUMP};
                END IF;
            ELSIF (
                NEW.status IS DISTINCT FROM OLD.status
                OR NEW.pnl_usd IS DISTINCT FROM OLD.pnl_usd
                OR NEW.exit_at IS DISTINCT FROM OLD.exit_at
                OR NEW.accounting_version IS DISTINCT FROM OLD.accounting_version
                OR NEW.accounting_status IS DISTINCT FROM OLD.accounting_status
                OR NEW.setup_context IS DISTINCT FROM OLD.setup_context
            ) AND ({_NOT_PAPER.format(row="OLD")} OR {_NOT_PAPER.format(row="NEW")}) THEN
                {_BUMP};
            END IF;
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION app.bump_pnl_generation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            {_BUMP};
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_trades_pnl_generation
        AFTER INSERT OR UPDATE OR DELETE ON app.trades
        FOR EACH ROW EXECUTE FUNCTION app.bump_pnl_generation_trades()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_trade_close_fills_pnl_generation
        AFTER INSERT OR UPDATE OR DELETE ON app.trade_close_fills
        FOR EACH ROW EXECUTE FUNCTION app.bump_pnl_generation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_fill_incidents_pnl_generation_insert_delete
        AFTER INSERT OR DELETE ON app.fill_resolution_incidents
        FOR EACH ROW EXECUTE FUNCTION app.bump_pnl_generation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_fill_incidents_pnl_generation_status
        AFTER UPDATE ON app.fill_resolution_incidents
        FOR EACH ROW WHEN (NEW.status IS DISTINCT FROM OLD.status)
        EXECUTE FUNCTION app.bump_pnl_generation()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM app.trade_cost_legs) THEN
                RAISE EXCEPTION 'cannot downgrade 0062 with trade cost evidence';
            END IF;
        END $$
        """
    )
    op.execute(
        "DROP TRIGGER trg_fill_incidents_pnl_generation_status ON app.fill_resolution_incidents"
    )
    op.execute(
        "DROP TRIGGER trg_fill_incidents_pnl_generation_insert_delete "
        "ON app.fill_resolution_incidents"
    )
    op.execute("DROP TRIGGER trg_trade_close_fills_pnl_generation ON app.trade_close_fills")
    op.execute("DROP TRIGGER trg_trades_pnl_generation ON app.trades")
    op.execute("DROP FUNCTION app.bump_pnl_generation()")
    op.execute("DROP FUNCTION app.bump_pnl_generation_trades()")
    op.drop_table("pnl_generation", schema="app")
    op.drop_index("ix_trade_cost_legs_trade", table_name="trade_cost_legs", schema="app")
    op.drop_index("ux_trade_cost_legs_exec", table_name="trade_cost_legs", schema="app")
    op.drop_table("trade_cost_legs", schema="app")
