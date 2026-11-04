"""Emergency close reasons of the LIVE_PROBE loss threshold and restart recovery.

Revision ID: 0064
Revises: 0063
Create Date: 2026-11-04

docs/engineering/live-probe-loss-threshold-design-v1.md, sections 4 and 5. A probe
position is closed through the emergency close task of
``live-entry-failure-paths-design-v1.md`` when its loss reaches L
(``probe_loss_threshold``) and on every start of the execution service
(``probe_recovery``). Only the allowed reasons change.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BEFORE = ("stop_placement_failed", "late_fill_flattened")
_AFTER = (*_BEFORE, "probe_loss_threshold", "probe_recovery")


def _reasons(reasons: tuple[str, ...]) -> str:
    return "reason IN (" + ", ".join(f"'{r}'" for r in reasons) + ")"


def upgrade() -> None:
    op.drop_constraint(
        "ck_live_emergency_closes_reason", "live_emergency_closes", type_="check", schema="app"
    )
    op.create_check_constraint(
        "ck_live_emergency_closes_reason", "live_emergency_closes", _reasons(_AFTER), schema="app"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM app.live_emergency_closes
                WHERE reason IN ('probe_loss_threshold', 'probe_recovery')
            ) THEN
                RAISE EXCEPTION 'cannot downgrade 0064 with probe emergency close tasks';
            END IF;
        END $$
        """
    )
    op.drop_constraint(
        "ck_live_emergency_closes_reason", "live_emergency_closes", type_="check", schema="app"
    )
    op.create_check_constraint(
        "ck_live_emergency_closes_reason", "live_emergency_closes", _reasons(_BEFORE), schema="app"
    )
