"""pin identity registry v2 fingerprint for source lead qualifications

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-28

research/gate-source-lead-registry-activation-v2. Adds a second, independent
CHECK CONSTRAINT alongside the existing v1 one
(ck_source_lead_qualification_v1_registry_contract, migration 0022) pinning
the identity_registry_version/identity_registry_fingerprint any row with
qualification_version='source_lead_qualified_capture_v2' must carry. Does
not touch the v1 constraint, the v1 registry file, or any existing v1 rows
-- those stay exactly as they are. source_lead_qualification.py's live
constants (QUALIFICATION_VERSION, DEFAULT_REGISTRY_RESOURCE,
EXPECTED_REGISTRY_VERSION, EXPECTED_REGISTRY_FINGERPRINT) now point at v2,
so every new qualification SourceLeadCaptureWorker writes going forward
uses this constraint; existing v1 rows remain valid under the v1 constraint
that already governs them.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_source_lead_qualification_v2_registry_contract",
        "source_lead_qualifications",
        "qualification_version != 'source_lead_qualified_capture_v2' OR "
        "(identity_registry_version = 'source_lead_identity_registry_v2' AND "
        "identity_registry_fingerprint = "
        "'757fd1327593d07ca27efe17a031ae0eab95bf6998aecc1ec26f0df38667dca0')",
        schema="app",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_source_lead_qualification_v2_registry_contract",
        "source_lead_qualifications",
        schema="app",
        type_="check",
    )
