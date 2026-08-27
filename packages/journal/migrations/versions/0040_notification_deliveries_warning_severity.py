"""allow warning severity in notification deliveries

Revision ID: 0040
Revises: 0039
Create Date: 2026-08-27

The notifier's liquidation-capture health monitor needs a severity tier
between 'info' and 'critical' for real (non-transient) degraded-health
alerts, distinct from purely informational messages and reserved 'critical'
severity. Widens ck_notification_deliveries_severity to admit 'warning'.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_notification_deliveries_severity",
        "notification_deliveries",
        schema="app",
        type_="check",
    )
    op.create_check_constraint(
        "ck_notification_deliveries_severity",
        "notification_deliveries",
        "severity IN ('critical', 'warning', 'trade', 'research', 'info')",
        schema="app",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_notification_deliveries_severity",
        "notification_deliveries",
        schema="app",
        type_="check",
    )
    op.create_check_constraint(
        "ck_notification_deliveries_severity",
        "notification_deliveries",
        "severity IN ('critical', 'trade', 'research', 'info')",
        schema="app",
    )
