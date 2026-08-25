"""add versioned derivatives context to momentum bars

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-25

The existing one-minute momentum row is the natural storage grain for
point-in-time mark/index/funding state.  Adding nullable columns avoids a
second 1.5M-row/day hypertable while keeping old rows honest: rows captured
before this migration have derivatives_context_version IS NULL and must not
be interpreted as zero funding or a zero basis.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "timeseries.bybit_momentum_bars_1m"

_COLUMN_DDLS = (
    "ADD COLUMN derivatives_context_version VARCHAR(32)",
    "ADD COLUMN mark_price DOUBLE PRECISION",
    "ADD COLUMN mark_price_event_at TIMESTAMPTZ",
    "ADD COLUMN mark_price_observed_at TIMESTAMPTZ",
    "ADD COLUMN index_price DOUBLE PRECISION",
    "ADD COLUMN index_price_event_at TIMESTAMPTZ",
    "ADD COLUMN index_price_observed_at TIMESTAMPTZ",
    "ADD COLUMN funding_rate DOUBLE PRECISION",
    "ADD COLUMN funding_rate_event_at TIMESTAMPTZ",
    "ADD COLUMN funding_rate_observed_at TIMESTAMPTZ",
    "ADD COLUMN next_funding_at TIMESTAMPTZ",
    "ADD COLUMN next_funding_event_at TIMESTAMPTZ",
    "ADD COLUMN next_funding_observed_at TIMESTAMPTZ",
    "ADD COLUMN derivatives_observed_this_minute BOOLEAN",
    "ADD COLUMN derivatives_complete BOOLEAN",
)

_VALIDATION_FUNCTION = "timeseries.validate_momentum_derivatives_context_v1"
_VALIDATION_TRIGGER = "validate_momentum_derivatives_context_v1"


def upgrade() -> None:
    # TimescaleDB's compressed-chunk propagation fails with "invalid attnum"
    # when all 15 columns are added in one ALTER statement, and validating a
    # new CHECK against historical compressed chunks fails with "unrecognized
    # node type" even when NOT VALID. One schema mutation per statement plus a
    # BEFORE trigger avoids decompressing every historical chunk (large
    # temporary disk headroom) while still rejecting every invalid new/update
    # row with SQLSTATE check_violation.
    for ddl in _COLUMN_DDLS:
        op.execute(f"ALTER TABLE {_TABLE} {ddl}")
    op.execute(f"""
        CREATE FUNCTION {_VALIDATION_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.derivatives_context_version IS NULL THEN
                IF NEW.derivatives_complete IS NOT NULL
                    OR NEW.derivatives_observed_this_minute IS NOT NULL
                    OR NEW.mark_price IS NOT NULL OR NEW.mark_price_event_at IS NOT NULL
                    OR NEW.mark_price_observed_at IS NOT NULL OR NEW.index_price IS NOT NULL
                    OR NEW.index_price_event_at IS NOT NULL
                    OR NEW.index_price_observed_at IS NOT NULL OR NEW.funding_rate IS NOT NULL
                    OR NEW.funding_rate_event_at IS NOT NULL
                    OR NEW.funding_rate_observed_at IS NOT NULL OR NEW.next_funding_at IS NOT NULL
                    OR NEW.next_funding_event_at IS NOT NULL
                    OR NEW.next_funding_observed_at IS NOT NULL
                THEN
                    RAISE EXCEPTION 'derivatives values require derivatives_context_version'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;

            IF NEW.derivatives_context_version <> 'derivatives_context_v1' THEN
                RAISE EXCEPTION 'unknown derivatives_context_version: %',
                    NEW.derivatives_context_version
                    USING ERRCODE = 'check_violation';
            END IF;

            IF NEW.derivatives_complete IS NULL
                OR NEW.derivatives_observed_this_minute IS NULL
            THEN
                RAISE EXCEPTION 'derivatives version requires completeness flags'
                    USING ERRCODE = 'check_violation';
            END IF;

            IF (NEW.mark_price IS NULL) <> (NEW.mark_price_event_at IS NULL)
                OR (NEW.mark_price IS NULL) <> (NEW.mark_price_observed_at IS NULL)
                OR (NEW.index_price IS NULL) <> (NEW.index_price_event_at IS NULL)
                OR (NEW.index_price IS NULL) <> (NEW.index_price_observed_at IS NULL)
                OR (NEW.funding_rate IS NULL) <> (NEW.funding_rate_event_at IS NULL)
                OR (NEW.funding_rate IS NULL) <> (NEW.funding_rate_observed_at IS NULL)
                OR (NEW.next_funding_at IS NULL) <> (NEW.next_funding_event_at IS NULL)
                OR (NEW.next_funding_at IS NULL) <> (NEW.next_funding_observed_at IS NULL)
            THEN
                RAISE EXCEPTION 'derivatives value/provenance tuple is incomplete'
                    USING ERRCODE = 'check_violation';
            END IF;

            IF NEW.mark_price IS NOT NULL AND (
                NEW.mark_price <= 0 OR NEW.mark_price IN (
                    'NaN'::DOUBLE PRECISION, 'Infinity'::DOUBLE PRECISION,
                    '-Infinity'::DOUBLE PRECISION
                )
            ) THEN
                RAISE EXCEPTION 'mark_price must be finite and positive'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.index_price IS NOT NULL AND (
                NEW.index_price <= 0 OR NEW.index_price IN (
                    'NaN'::DOUBLE PRECISION, 'Infinity'::DOUBLE PRECISION,
                    '-Infinity'::DOUBLE PRECISION
                )
            ) THEN
                RAISE EXCEPTION 'index_price must be finite and positive'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.funding_rate IS NOT NULL AND NEW.funding_rate IN (
                'NaN'::DOUBLE PRECISION, 'Infinity'::DOUBLE PRECISION,
                '-Infinity'::DOUBLE PRECISION
            ) THEN
                RAISE EXCEPTION 'funding_rate must be finite'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.derivatives_complete AND (
                NEW.mark_price IS NULL OR NEW.index_price IS NULL
                OR NEW.funding_rate IS NULL OR NEW.next_funding_at IS NULL
            ) THEN
                RAISE EXCEPTION 'derivatives_complete requires every context value'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute(f"""
        CREATE TRIGGER {_VALIDATION_TRIGGER}
        BEFORE INSERT OR UPDATE ON {_TABLE}
        FOR EACH ROW EXECUTE FUNCTION {_VALIDATION_FUNCTION}()
    """)


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {_VALIDATION_TRIGGER} ON {_TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS {_VALIDATION_FUNCTION}()")
    for column in reversed(tuple(ddl.split()[2] for ddl in _COLUMN_DDLS)):
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {column}")
