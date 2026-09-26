"""pin identity registry v4 fingerprint for source lead qualifications

Revision ID: 0051
Revises: 0050
Create Date: 2026-09-26

HYP-012 v4 (PR D). Adds a fourth, independent CHECK CONSTRAINT alongside the
v1 (0022), v2 (0041) and v3 (0043) ones: any row with
qualification_version='source_lead_qualified_capture_v4' must carry registry
v4 and its fingerprint. Existing v1/v2/v3 rows and constraints are untouched.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_source_lead_qualification_v4_registry_contract",
        "source_lead_qualifications",
        "qualification_version != 'source_lead_qualified_capture_v4' OR "
        "(identity_registry_version = 'source_lead_identity_registry_v4' AND "
        "identity_registry_fingerprint = "
        "'7d5f635a4ed02013ad3bd5fb7bd118f5b80979427bf059a130279fa2c3bee189')",
        schema="app",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_source_lead_qualification_v4_registry_contract",
        "source_lead_qualifications",
        schema="app",
        type_="check",
    )
