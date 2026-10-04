"""History archive snapshots and snapshot sets.

Revision ID: 0059
Revises: 0058
Create Date: 2026-10-04

Plain tables are archived as point-in-time snapshots, not chunks
(docs/runbooks/hyp015-inputs-archive-design-v1.md). A catalog row now has a `unit`:

- `chunk` (the default, so a row written in the 0058 shape stays a chunk): a time range
  of a hypertable, as before (range and chunk name required, at least one row);
- `snapshot`: the rows of a table at one instant, inside a snapshot set (no range, no
  chunk name; it may hold zero rows).

`unit` and `snapshot_set` are immutable like the other content columns. One live
revision exists per dataset and range (chunks) or per dataset and set (snapshots).

`app.history_archive_snapshot_sets` groups the snapshots taken from one database
snapshot, with the pinned revisions of the time-series chunks they depend on and a
blind composition reference. A set moves `building -> verified` only when every
required member dataset has a verified snapshot row in it, `building -> abandoned`
with a reason, and `verified -> superseded` only in favour of a verified set of the
same purpose. A snapshot row of a verified set cannot be superseded. Nothing is ever
deleted.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUARD_0058 = """
CREATE OR REPLACE FUNCTION app.history_archive_datasets_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'history archive catalog rows are never deleted (id %)', OLD.id;
    END IF;
    IF (NEW.dataset, NEW.contract_version, NEW.source_table, NEW.chunk_name,
        NEW.range_start, NEW.range_end, NEW.revision, NEW.row_count, NEW.file_name,
        NEW.file_bytes, NEW.file_sha256, NEW.content_fingerprint,
        NEW.manifest_sha256, NEW.snapshot_at, NEW.code_revision, NEW.created_at)
       IS DISTINCT FROM
       (OLD.dataset, OLD.contract_version, OLD.source_table, OLD.chunk_name,
        OLD.range_start, OLD.range_end, OLD.revision, OLD.row_count, OLD.file_name,
        OLD.file_bytes, OLD.file_sha256, OLD.content_fingerprint,
        OLD.manifest_sha256, OLD.snapshot_at, OLD.code_revision, OLD.created_at)
    THEN
        RAISE EXCEPTION 'history archive content of row % is immutable', OLD.id;
    END IF;
    IF OLD.state = 'superseded' THEN
        RAISE EXCEPTION 'history archive row % is superseded and final', OLD.id;
    END IF;
    IF OLD.borg_archive IS NOT NULL AND NEW.borg_archive IS DISTINCT FROM OLD.borg_archive
    THEN
        RAISE EXCEPTION 'history archive row % already names its archive', OLD.id;
    END IF;
    IF NOT (
        (OLD.state = 'exported' AND NEW.state = 'archived')
        OR (OLD.state = 'archived' AND NEW.state = 'verified')
        OR NEW.state = 'superseded'
    ) THEN
        RAISE EXCEPTION 'history archive row % cannot move from % to %',
            OLD.id, OLD.state, NEW.state;
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END $$;
"""

_GUARD_0059 = (
    _GUARD_0058.replace(
        "NEW.manifest_sha256, NEW.snapshot_at, NEW.code_revision, NEW.created_at)\n       IS",
        "NEW.manifest_sha256, NEW.snapshot_at, NEW.code_revision, NEW.created_at,\n"
        "        NEW.unit, NEW.snapshot_set)\n       IS",
    )
    .replace(
        "OLD.manifest_sha256, OLD.snapshot_at, OLD.code_revision, OLD.created_at)\n    THEN",
        "OLD.manifest_sha256, OLD.snapshot_at, OLD.code_revision, OLD.created_at,\n"
        "        OLD.unit, OLD.snapshot_set)\n    THEN",
    )
    .replace(
        "    IF NOT (\n        (OLD.state = 'exported'",
        "    IF NEW.state = 'superseded' AND OLD.unit = 'snapshot' AND EXISTS (\n"
        "        SELECT 1 FROM app.history_archive_snapshot_sets\n"
        "        WHERE set_id = OLD.snapshot_set AND state = 'verified'\n"
        "    ) THEN\n"
        "        RAISE EXCEPTION 'history archive row % belongs to verified set %',\n"
        "            OLD.id, OLD.snapshot_set;\n"
        "    END IF;\n"
        "    IF NOT (\n        (OLD.state = 'exported'",
    )
)


def upgrade() -> None:
    assert _GUARD_0059.count("NEW.unit, NEW.snapshot_set") == 1
    assert _GUARD_0059.count("belongs to verified set") == 1
    op.execute(
        r"""
        ALTER TABLE app.history_archive_datasets
            ADD COLUMN unit TEXT NOT NULL DEFAULT 'chunk',
            ADD COLUMN snapshot_set TEXT,
            ALTER COLUMN chunk_name DROP NOT NULL,
            ALTER COLUMN range_start DROP NOT NULL,
            ALTER COLUMN range_end DROP NOT NULL,
            DROP CONSTRAINT ck_history_archive_range,
            DROP CONSTRAINT ck_history_archive_counts,
            ADD CONSTRAINT ck_history_archive_unit CHECK (
                (unit = 'chunk' AND chunk_name IS NOT NULL AND range_start IS NOT NULL
                    AND range_end IS NOT NULL AND range_end > range_start
                    AND snapshot_set IS NULL AND row_count > 0)
                OR (unit = 'snapshot' AND chunk_name IS NULL AND range_start IS NULL
                    AND range_end IS NULL AND snapshot_set IS NOT NULL AND row_count >= 0)
            ),
            ADD CONSTRAINT ck_history_archive_file_bytes CHECK (file_bytes > 0);
        CREATE UNIQUE INDEX uq_history_archive_snapshot_revision
            ON app.history_archive_datasets (dataset, contract_version, snapshot_set, revision)
            WHERE unit = 'snapshot';
        CREATE UNIQUE INDEX uq_history_archive_live_snapshot
            ON app.history_archive_datasets (dataset, snapshot_set)
            WHERE unit = 'snapshot' AND state <> 'superseded';

        CREATE TABLE app.history_archive_snapshot_sets (
            set_id TEXT PRIMARY KEY,
            purpose TEXT NOT NULL,
            required_datasets TEXT[] NOT NULL,
            snapshot_at TIMESTAMPTZ NOT NULL,
            code_revision TEXT NOT NULL,
            pinned_chunks JSONB NOT NULL,
            reference JSONB NOT NULL,
            reference_sha256 TEXT NOT NULL,
            state TEXT NOT NULL,
            verified_at TIMESTAMPTZ,
            superseded_by TEXT REFERENCES app.history_archive_snapshot_sets (set_id),
            closed_reason TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_history_archive_set_members CHECK (
                cardinality(required_datasets) > 0
            ),
            CONSTRAINT ck_history_archive_set_reference CHECK (
                reference_sha256 ~ '^[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_history_archive_set_state CHECK (
                (state = 'building' AND verified_at IS NULL AND superseded_by IS NULL
                    AND closed_reason IS NULL)
                OR (state = 'verified' AND verified_at IS NOT NULL AND superseded_by IS NULL
                    AND closed_reason IS NULL)
                OR (state = 'abandoned' AND verified_at IS NULL AND superseded_by IS NULL
                    AND closed_reason IS NOT NULL)
                OR (state = 'superseded' AND verified_at IS NOT NULL
                    AND superseded_by IS NOT NULL AND superseded_by <> set_id)
            )
        );

        CREATE FUNCTION app.history_archive_snapshot_sets_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            missing TEXT[];
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'snapshot set % is never deleted', OLD.set_id;
            END IF;
            IF (NEW.set_id, NEW.purpose, NEW.required_datasets, NEW.snapshot_at,
                NEW.code_revision, NEW.pinned_chunks, NEW.reference, NEW.reference_sha256,
                NEW.created_at)
               IS DISTINCT FROM
               (OLD.set_id, OLD.purpose, OLD.required_datasets, OLD.snapshot_at,
                OLD.code_revision, OLD.pinned_chunks, OLD.reference, OLD.reference_sha256,
                OLD.created_at)
            THEN
                RAISE EXCEPTION 'snapshot set % content is immutable', OLD.set_id;
            END IF;
            IF OLD.state = 'building' AND NEW.state = 'verified' THEN
                SELECT array_agg(d) INTO missing FROM unnest(OLD.required_datasets) AS d
                WHERE NOT EXISTS (
                    SELECT 1 FROM app.history_archive_datasets r
                    WHERE r.unit = 'snapshot' AND r.snapshot_set = OLD.set_id
                      AND r.dataset = d AND r.state = 'verified'
                );
                IF missing IS NOT NULL THEN
                    RAISE EXCEPTION 'snapshot set % lacks verified members %',
                        OLD.set_id, missing;
                END IF;
            ELSIF OLD.state = 'building' AND NEW.state = 'abandoned' THEN
                NULL;
            ELSIF OLD.state = 'verified' AND NEW.state = 'superseded' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM app.history_archive_snapshot_sets s
                    WHERE s.set_id = NEW.superseded_by AND s.state = 'verified'
                      AND s.purpose = OLD.purpose AND s.snapshot_at > OLD.snapshot_at
                ) THEN
                    RAISE EXCEPTION 'snapshot set % may only give way to a newer verified set',
                        OLD.set_id;
                END IF;
            ELSE
                RAISE EXCEPTION 'snapshot set % cannot move from % to %',
                    OLD.set_id, OLD.state, NEW.state;
            END IF;
            NEW.updated_at := now();
            RETURN NEW;
        END $$;
        CREATE TRIGGER history_archive_snapshot_sets_guard
            BEFORE UPDATE OR DELETE ON app.history_archive_snapshot_sets
            FOR EACH ROW EXECUTE FUNCTION app.history_archive_snapshot_sets_guard();
        """
    )
    op.execute(_GUARD_0059)


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM app.history_archive_datasets WHERE unit <> 'chunk')
               OR EXISTS (SELECT 1 FROM app.history_archive_snapshot_sets) THEN
                RAISE EXCEPTION 'history archive snapshots exist; refusing to drop';
            END IF;
        END $$;
        DROP TABLE app.history_archive_snapshot_sets;
        DROP FUNCTION app.history_archive_snapshot_sets_guard();
        DROP INDEX app.uq_history_archive_live_snapshot;
        DROP INDEX app.uq_history_archive_snapshot_revision;
        ALTER TABLE app.history_archive_datasets
            DROP CONSTRAINT ck_history_archive_unit,
            DROP CONSTRAINT ck_history_archive_file_bytes,
            ADD CONSTRAINT ck_history_archive_range CHECK (range_end > range_start),
            ADD CONSTRAINT ck_history_archive_counts CHECK (row_count > 0 AND file_bytes > 0),
            ALTER COLUMN chunk_name SET NOT NULL,
            ALTER COLUMN range_start SET NOT NULL,
            ALTER COLUMN range_end SET NOT NULL,
            DROP COLUMN snapshot_set,
            DROP COLUMN unit;
        """
    )
    op.execute(_GUARD_0058)
