"""Administrative stop as a third terminal state of a formal-read claim.

Revision ID: 0057
Revises: 0056
Create Date: 2026-10-02

A registered cohort can now end without a read: `admin_stopped` records that its
registered accrual rule closed it before any return was computed. The row is inserted
once, in place of a claim, under the same unique (study, contract version, cohort start)
key, so a stop and a formal read can never both exist for one cohort.

A trigger keeps the terminal states terminal. A `claimed` row may only become
`completed` (the existing lease takeover still updates its lease); no update may turn a
claim into `admin_stopped`, and `completed` and `admin_stopped` rows never change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "formal_read_claims"
_SCHEMA = "app"
_STATUS_CHECK = "ck_formal_read_claim_status"

_OLD_STATUS = (
    "(status = 'claimed' AND completed_at IS NULL) OR "
    "(status = 'completed' AND completed_at IS NOT NULL "
    "AND result_fingerprint IS NOT NULL)"
)
_NEW_STATUS = (
    "(status = 'claimed' AND completed_at IS NULL AND terminal_reason IS NULL) OR "
    "(status = 'completed' AND completed_at IS NOT NULL "
    "AND result_fingerprint IS NOT NULL AND terminal_reason IS NULL) OR "
    "(status = 'admin_stopped' AND completed_at IS NOT NULL "
    "AND result_fingerprint IS NOT NULL AND terminal_reason IS NOT NULL)"
)


def upgrade() -> None:
    op.add_column(
        _TABLE, sa.Column("terminal_reason", sa.String(64), nullable=True), schema=_SCHEMA
    )
    op.drop_constraint(_STATUS_CHECK, _TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(_STATUS_CHECK, _TABLE, _NEW_STATUS, schema=_SCHEMA)
    op.execute(
        """
        CREATE FUNCTION app.formal_read_claims_terminal_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status IN ('completed', 'admin_stopped') THEN
                RAISE EXCEPTION 'formal read claim % is terminal (%)', OLD.id, OLD.status;
            END IF;
            IF NEW.status NOT IN ('claimed', 'completed') THEN
                RAISE EXCEPTION 'an open formal read claim cannot become %', NEW.status;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_formal_read_claims_terminal_guard
        BEFORE UPDATE ON app.formal_read_claims
        FOR EACH ROW EXECUTE FUNCTION app.formal_read_claims_terminal_guard()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM app.formal_read_claims WHERE status = 'admin_stopped') THEN
                RAISE EXCEPTION 'refusing to downgrade: administrative stops exist';
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP TRIGGER trg_formal_read_claims_terminal_guard ON app.formal_read_claims")
    op.execute("DROP FUNCTION app.formal_read_claims_terminal_guard()")
    op.drop_constraint(_STATUS_CHECK, _TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(_STATUS_CHECK, _TABLE, _OLD_STATUS, schema=_SCHEMA)
    op.drop_column(_TABLE, "terminal_reason", schema=_SCHEMA)
