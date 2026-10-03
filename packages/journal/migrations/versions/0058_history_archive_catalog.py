"""History archive catalog and the inert insert fence for the LSR pilot.

Revision ID: 0058
Revises: 0057
Create Date: 2026-10-04

`app.history_archive_datasets` records each exported range of a history dataset
(docs/runbooks/history-archive-design-v1.md). A row only moves forward:
`exported -> archived -> verified`, or to `superseded` from any live state, and each
state carries its own evidence (CHECK; every proof column is required explicitly with
IS NOT NULL, because a CHECK that evaluates to NULL passes). Content columns never
change once written and rows are never deleted, so the catalog is an audit trail of
what left the database.
One live (not superseded) revision exists per dataset and range.

`app.history_archive_fences` holds, per dataset, the instant below which the source
table no longer accepts rows. No fence is raised here: the pilot's row is inserted at
`-infinity`, which lets every insert through, exactly as before. The row exists from
the start because a fence may only ever MOVE (an UPDATE that never lowers it; rows are
never deleted). The BEFORE INSERT trigger on `app.live_long_short_ratio` reads that row
`FOR SHARE`: a READ COMMITTED writer then sees the latest fence, and a writer whose
REPEATABLE READ snapshot predates a fence move fails to serialize instead of inserting
under the old one. A fence row inserted later could not give that guarantee, since a
snapshot older than the row would not see it at all.

`app.live_long_short_ratio` is created by the market-hotset service (EnsureSchema), not
by a migration. The same idempotent DDL runs here so a fresh database has the table the
trigger attaches to; on production it already exists and nothing about it changes.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SQLSTATE the fence trigger raises (spelled out in its body); class "SH" is not used
# by PostgreSQL.
FENCE_SQLSTATE = "SH001"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app.live_long_short_ratio (
            ts TIMESTAMPTZ NOT NULL,
            base TEXT NOT NULL,
            exchange TEXT NOT NULL,
            ratio NUMERIC NOT NULL,
            long_account NUMERIC,
            short_account NUMERIC,
            PRIMARY KEY (exchange, base, ts)
        );
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM timescaledb_information.hypertables
                WHERE hypertable_schema = 'app' AND hypertable_name = 'live_long_short_ratio'
            ) THEN
                PERFORM create_hypertable('app.live_long_short_ratio', 'ts');
            END IF;
        END $$;
        """
    )
    op.execute(
        r"""
        CREATE TABLE app.history_archive_datasets (
            id BIGSERIAL PRIMARY KEY,
            dataset TEXT NOT NULL,
            contract_version TEXT NOT NULL,
            source_table TEXT NOT NULL,
            chunk_name TEXT NOT NULL,
            range_start TIMESTAMPTZ NOT NULL,
            range_end TIMESTAMPTZ NOT NULL,
            revision INTEGER NOT NULL,
            state TEXT NOT NULL,
            row_count BIGINT NOT NULL,
            file_name TEXT NOT NULL,
            file_bytes BIGINT NOT NULL,
            file_sha256 TEXT NOT NULL,
            content_fingerprint TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            snapshot_at TIMESTAMPTZ NOT NULL,
            code_revision TEXT NOT NULL,
            borg_archive TEXT,
            archived_at TIMESTAMPTZ,
            verified_at TIMESTAMPTZ,
            verified_sha256 TEXT,
            verified_fingerprint TEXT,
            superseded_reason TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_history_archive_range CHECK (range_end > range_start),
            CONSTRAINT ck_history_archive_revision CHECK (revision >= 1),
            CONSTRAINT ck_history_archive_counts CHECK (row_count > 0 AND file_bytes > 0),
            CONSTRAINT ck_history_archive_hashes CHECK (
                file_sha256 ~ '^[0-9a-f]{64}$' AND manifest_sha256 ~ '^[0-9a-f]{64}$'
                AND content_fingerprint ~ '^[a-z0-9_]+:[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_history_archive_state CHECK (
                (state = 'exported' AND borg_archive IS NULL AND archived_at IS NULL
                    AND verified_at IS NULL AND superseded_reason IS NULL)
                OR (state = 'archived' AND borg_archive IS NOT NULL AND archived_at IS NOT NULL
                    AND verified_at IS NULL AND superseded_reason IS NULL)
                OR (state = 'verified' AND borg_archive IS NOT NULL AND archived_at IS NOT NULL
                    AND verified_at IS NOT NULL
                    AND verified_sha256 IS NOT NULL AND verified_sha256 = file_sha256
                    AND verified_fingerprint IS NOT NULL
                    AND verified_fingerprint = content_fingerprint
                    AND superseded_reason IS NULL)
                OR (state = 'superseded' AND superseded_reason IS NOT NULL)
            ),
            CONSTRAINT uq_history_archive_revision
                UNIQUE (dataset, contract_version, range_start, range_end, revision)
        );
        CREATE UNIQUE INDEX uq_history_archive_live_range
            ON app.history_archive_datasets (dataset, range_start, range_end)
            WHERE state <> 'superseded';

        CREATE FUNCTION app.history_archive_datasets_guard() RETURNS trigger
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
        CREATE TRIGGER history_archive_datasets_guard
            BEFORE UPDATE OR DELETE ON app.history_archive_datasets
            FOR EACH ROW EXECUTE FUNCTION app.history_archive_datasets_guard();

        CREATE TABLE app.history_archive_fences (
            dataset TEXT PRIMARY KEY,
            source_table TEXT NOT NULL,
            closed_before TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE FUNCTION app.history_archive_fences_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'history archive fence % is never deleted', OLD.dataset;
            END IF;
            IF NEW.dataset IS DISTINCT FROM OLD.dataset
               OR NEW.source_table IS DISTINCT FROM OLD.source_table THEN
                RAISE EXCEPTION 'history archive fence % changes only its instant', OLD.dataset;
            END IF;
            IF NEW.closed_before < OLD.closed_before THEN
                RAISE EXCEPTION 'history archive fence % never moves back (% to %)',
                    OLD.dataset, OLD.closed_before, NEW.closed_before;
            END IF;
            NEW.updated_at := now();
            RETURN NEW;
        END $$;
        CREATE TRIGGER history_archive_fences_guard
            BEFORE UPDATE OR DELETE ON app.history_archive_fences
            FOR EACH ROW EXECUTE FUNCTION app.history_archive_fences_guard();
        INSERT INTO app.history_archive_fences (dataset, source_table, closed_before)
            VALUES ('lsr_history', 'app.live_long_short_ratio', '-infinity');
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.history_archive_fence_lsr() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            fence TIMESTAMPTZ;
        BEGIN
            SELECT closed_before INTO fence FROM app.history_archive_fences
                WHERE dataset = 'lsr_history' FOR SHARE;
            IF NEW.ts < fence THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'SH001',
                    MESSAGE = format(
                        'live_long_short_ratio is archived below %s; row at %s refused',
                        fence, NEW.ts);
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER history_archive_fence
            BEFORE INSERT ON app.live_long_short_ratio
            FOR EACH ROW EXECUTE FUNCTION app.history_archive_fence_lsr();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM app.history_archive_datasets)
               OR EXISTS (SELECT 1 FROM app.history_archive_fences
                          WHERE closed_before <> '-infinity') THEN
                RAISE EXCEPTION 'history archive catalog or fences hold rows; refusing to drop';
            END IF;
        END $$;
        DROP TRIGGER history_archive_fence ON app.live_long_short_ratio;
        DROP FUNCTION app.history_archive_fence_lsr();
        DROP TABLE app.history_archive_fences;
        DROP FUNCTION app.history_archive_fences_guard();
        DROP TABLE app.history_archive_datasets;
        DROP FUNCTION app.history_archive_datasets_guard();
        """
    )
