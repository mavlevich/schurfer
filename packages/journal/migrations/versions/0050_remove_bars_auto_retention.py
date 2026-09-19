"""remove the automatic Timescale retention policy from bybit_momentum_bars_1m

Revision ID: 0050
Revises: 0049
Create Date: 2026-09-19

Migration 0024 attached ``add_retention_policy(timeseries.bybit_momentum_bars_1m,
INTERVAL '35 days')``, which drops chunks on a schedule with NO check that the day was
exported and confirmed offsite. Gated deletion (cold_bar_gated_deletion*) replaces it: a
day's chunk is dropped only after that exact day is proven exported, offsite, and unchanged.
This migration removes the automatic policy so the two never race.

DOWNGRADE FAILS LOUDLY. Re-adding the unconditional policy would reintroduce ungated
deletion that could drop an unexported/unconfirmed day (the failure this whole line of work
exists to prevent), so the downgrade refuses rather than silently restoring it. Retention is
now application-managed via the gated-deletion job; if the automatic policy is ever truly
wanted again it must be added deliberately, not resurrected by a schema rollback.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "timeseries.bybit_momentum_bars_1m"


def upgrade() -> None:
    # Idempotent: if_exists so a re-run (or an environment where the policy was already
    # removed) is a no-op rather than an error.
    op.execute(f"SELECT remove_retention_policy('{_TABLE}', if_exists => true)")


def downgrade() -> None:
    raise RuntimeError(
        "Refusing to downgrade 0050: re-adding the automatic 35-day retention policy on "
        f"{_TABLE} would reintroduce UNGATED deletion that can drop an unexported or "
        "unconfirmed day. Retention is now application-managed via cold-bar gated deletion. "
        "If the automatic policy is genuinely wanted again, add it deliberately in a new "
        "migration, do not roll back into it."
    )
