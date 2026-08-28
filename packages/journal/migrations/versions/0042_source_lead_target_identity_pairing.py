"""pair identity_match_method with identity_verified for v2 target observations

Revision ID: 0042
Revises: 0041
Create Date: 2026-08-28

research/gate-source-lead-registry-activation-v2 (colleague review,
2026-08-28). Adds a CHECK CONSTRAINT on source_lead_target_observations
pinning that identity_match_method='registry_exact_v2' always carries
identity_verified=true and identity_match_method='registry_lookup_v2' always
carries identity_verified=false. Before this, every capture-side failure was
tagged registry_exact_v2 with identity_verified always false regardless of
whether a market was ever actually resolved -- this makes the two fields
mutually consistent at the database level, not just by capture-code
convention. Does not touch the existing base_symbol_v1 constraint
(ck_source_lead_target_provisional_identity, migration prior to this PR) or
any historical rows using that method.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_source_lead_target_v2_identity_pairing",
        "source_lead_target_observations",
        "identity_match_method NOT IN ('registry_exact_v2', 'registry_lookup_v2') OR "
        "identity_verified = (identity_match_method = 'registry_exact_v2')",
        schema="app",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_source_lead_target_v2_identity_pairing",
        "source_lead_target_observations",
        schema="app",
        type_="check",
    )
