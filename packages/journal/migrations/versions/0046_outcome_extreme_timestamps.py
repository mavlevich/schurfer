"""record when each outcome extreme occurred

Revision ID: 0046
Revises: 0045
Create Date: 2026-09-07

app.trade_decision_outcomes stored mfe_pct and mae_pct as magnitudes with no
timing, so for a position that touched both a favourable target and the stop
level the table could not say which came first. HYP-020 therefore could only
bound the stop question -- assume every position that ever reached the stop was
stopped there -- rather than answer it, and that bound landed within noise of
zero, the least useful place for a bound to land.

Two nullable timestamp columns fix that going forward. They are the start of the
minute bar that set each extreme, a bucket rather than an intra-bar instant,
which is the resolution the source bars have.

Never backfilled: the ordering of the extremes was not recorded for existing
rows and cannot be recovered from stored magnitudes. NULL therefore means
"written before this existed" and must not be read as "simultaneous".
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("mfe_at", "mae_at")


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column(
            "trade_decision_outcomes",
            sa.Column(name, sa.DateTime(timezone=True), nullable=True),
            schema="app",
        )


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("trade_decision_outcomes", name, schema="app")
