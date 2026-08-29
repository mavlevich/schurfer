"""pin identity registry v3 fingerprint for source lead qualifications

Revision ID: 0043
Revises: 0042
Create Date: 2026-08-29

research/gate-source-lead-registry-activation-v3 (PR 2 of 3). Adds a third,
independent CHECK CONSTRAINT alongside the existing v1 one
(ck_source_lead_qualification_v1_registry_contract, migration 0022) and v2
one (ck_source_lead_qualification_v2_registry_contract, migration 0041)
pinning the identity_registry_version/identity_registry_fingerprint any row
with qualification_version='source_lead_qualified_capture_v3' must carry.

Deliberately inert on deploy: no row anywhere carries
qualification_version='source_lead_qualified_capture_v3' yet --
source_lead_qualification.py's live constants (QUALIFICATION_VERSION,
DEFAULT_REGISTRY_RESOURCE, EXPECTED_REGISTRY_VERSION,
EXPECTED_REGISTRY_FINGERPRINT, EVIDENCE_DIR) still point at v2 in this PR,
so SourceLeadCaptureWorker keeps writing v2-tagged rows exactly as before.
This migration only pre-registers the v3 contract so PR 3 (the commit that
actually bumps QUALIFICATION_VERSION to v3 and starts writing v3-tagged
rows) does not also need a migration in the same deploy. Does not touch the
v1 or v2 constraints, the v1/v2 registry files, or any existing v1/v2 rows
-- those stay exactly as they are.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_source_lead_qualification_v3_registry_contract",
        "source_lead_qualifications",
        "qualification_version != 'source_lead_qualified_capture_v3' OR "
        "(identity_registry_version = 'source_lead_identity_registry_v3' AND "
        "identity_registry_fingerprint = "
        "'9d36c41442261cfe4e608342378e2d83f96c78afd537de682698796e77733236')",
        schema="app",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_source_lead_qualification_v3_registry_contract",
        "source_lead_qualifications",
        schema="app",
        type_="check",
    )
