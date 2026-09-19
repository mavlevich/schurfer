"""remove the automatic Timescale retention policy from bybit_momentum_bars_1m

Revision ID: 0050
Revises: 0049
Create Date: 2026-09-19

Migration 0024 attached ``add_retention_policy(timeseries.bybit_momentum_bars_1m,
INTERVAL '35 days')``, which drops chunks on a schedule with NO check that the day was
exported and confirmed offsite. Gated deletion (cold_bar_gated_deletion*) replaces it: a
day's chunk is dropped only after that exact day is proven exported, offsite, and unchanged.
This migration removes the automatic policy so the two never race.

DOWNGRADE IS A DELIBERATE NO-OP. It does NOT re-add the automatic policy: re-adding it would
reintroduce ungated deletion that could drop an unexported/unconfirmed day (the failure this
whole line of work exists to prevent). Retention is now application-managed via the
gated-deletion job, so a rollback simply leaves the automatic policy removed rather than
restoring the unsafe behaviour. If the automatic policy is ever genuinely wanted again it must
be added deliberately in a new migration, never resurrected by a schema rollback. (A raising
downgrade was rejected: it would also block every migration-chain downgrade that steps through
this revision.)
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
    # Intentionally does NOT restore the automatic retention policy (that would reintroduce
    # ungated deletion). Retention stays application-managed by cold-bar gated deletion.
    pass
