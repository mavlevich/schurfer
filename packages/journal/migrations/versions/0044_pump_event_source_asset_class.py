"""record the asset class of each scanned pump event source

Revision ID: 0044
Revises: 0043
Create Date: 2026-09-07

ENG-018. The broad multi-exchange scanner admits any active `/USDT:USDT`
ticker and records market_type='swap', which says the instrument is a
perpetual but nothing about what it tracks. LBank 24H stock futures such as
DJT, LYTE and PURR therefore entered pump cohorts as extreme crypto pumps
when their fresh 24-hour baseline initialized (2026-08-25 production audit).

Five nullable columns, never backfilled: rows written before the classifier
existed carry no evidence of their class, and inventing one retroactively
would contaminate exactly the cohorts this is meant to protect. NULL means
"never classified", which is distinguishable from the classifier's own
'unknown', meaning "classified, and the venue exposes no usable evidence".

Additive and inert for existing readers: no consumer selects these columns
until it opts in.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("asset_class", 32),
    ("asset_class_source", 64),
    ("asset_class_evidence", 256),
    ("asset_class_confidence", 32),
    ("asset_class_version", 32),
)


def upgrade() -> None:
    for name, length in _COLUMNS:
        op.add_column(
            "pump_event_sources",
            sa.Column(name, sa.String(length=length), nullable=True),
            schema="app",
        )
    # Cohort queries filter on the class, so the index carries the class first.
    # Partial on NOT NULL: unclassified history is the majority of the table
    # today and is never selected by class.
    op.create_index(
        "ix_pump_event_sources_asset_class",
        "pump_event_sources",
        ["asset_class", "exchange"],
        unique=False,
        schema="app",
        postgresql_where=sa.text("asset_class IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pump_event_sources_asset_class",
        table_name="pump_event_sources",
        schema="app",
    )
    for name, _ in reversed(_COLUMNS):
        op.drop_column("pump_event_sources", name, schema="app")
