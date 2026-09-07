"""widen pump_event_source asset_class_evidence to TEXT

Revision ID: 0045
Revises: 0044
Create Date: 2026-09-07

Colleague review of the ENG-018 classifier: asset_class_evidence is built from
venue-controlled values -- `xt.tags=[...]` from XT's own tag list, and
`bybit.symbolType=<raw>` -- and was stored as VARCHAR(256). A venue returning
a longer payload than expected would make the INSERT fail with "value too long
for type character varying(256)".

That failure is not confined to one row. persistence.upsert_pumps runs the
whole scan batch in one transaction and returns an empty mapping on any
exception, and its own contract says the caller must then not publish a new
Redis snapshot at all. So an over-long string from an external API could stop
the scanner from producing usable output, not merely drop one instrument's
class.

TEXT has no length limit and, in PostgreSQL, no performance difference from
VARCHAR. The classifier also bounds the string it builds (MAX_EVIDENCE_LENGTH)
so a pathological payload cannot bloat rows, but that is defence in depth: this
column type is what removes the failure mode.

Safe on a live table: VARCHAR(n) to TEXT is binary-coercible, so PostgreSQL
performs it as a catalog change without rewriting the table. The downgrade
does rewrite and would fail on any row already longer than 256 characters,
which is inherent to narrowing a column and is why the classifier's own bound
sits below that only for new writes -- documented here rather than hidden.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "pump_event_sources",
        "asset_class_evidence",
        existing_type=sa.String(length=256),
        type_=sa.Text(),
        existing_nullable=True,
        schema="app",
    )


def downgrade() -> None:
    op.alter_column(
        "pump_event_sources",
        "asset_class_evidence",
        existing_type=sa.Text(),
        type_=sa.String(length=256),
        existing_nullable=True,
        schema="app",
        postgresql_using="left(asset_class_evidence, 256)",
    )
