"""momentum universe identity foundation (snapshots + instruments)

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-15

Foundation step of feat/momentum-universe-identity-foundation-v1 (ROADMAP
phase 3 item 8's own first half). This migration adds persistence for
per-instrument catalog identity metadata (native market id, base/quote/
settle, onboarding time) captured alongside each venue's own frozen
universe -- something that did not exist anywhere before this PR: the
existing timeseries.bybit_momentum_bars_1m table records exchange/symbol
per bar, but nothing durable ever recorded WHICH instrument that symbol
actually was at that point in time, beyond the bar-owning process's own
in-memory Universe (thrown away on restart).

Two tables, not one flat table, on a colleague review's own recommendation
before implementation started: snapshot-level provenance (which fetch,
when, how many instruments, a whole-payload hash) is recorded once per
snapshot rather than repeated on every instrument row, and instruments are
atomically linked to their own snapshot via the natural composite key both
tables share -- no surrogate auto-increment id, matching every other table
in this schema (bars/watch_runs/watch_states all use natural keys, no
BIGSERIAL anywhere in this database).

universe_version alone is NOT enough to detect an identity-relevant
change (also a colleague review finding, before implementation): the same
symbol SET can stay identical while a symbol's own onboarded_at silently
changes underneath it (delisted and relisted under the same ticker).
catalog_version is a separate hash of the normalized instrument records
plus schema_version, so that case produces a new snapshot even when
universe_version does not.

Fail-closed identity, enforced at the DB level, not just in application
code (identity_key_only_when_ready): identity_key and onboarded_at are
non-NULL if and only if identity_status = 'ready'. A row with incomplete
or invalid source data gets a named status (missing_onboarded_at,
invalid_onboarded_at, invalid_assets, unsupported_market_type) and a NULL
identity_key -- never a key built from partial or guessed data.

This migration deliberately stops at single-venue identity metadata.
Nothing here compares two venues' own Instruments against each other,
computes identity_conflict, or claims two rows are the same real-world
asset -- that is a separate, not-yet-built cross-venue RESOLUTION step
(see docs/research/momentum-universe-identity-foundation-v1.md), which is
exactly why identity_status has no 'confirmed'/'conflict' value here: this
table has no way to know either of those yet.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SNAPSHOTS = "app.momentum_universe_snapshots"
_INSTRUMENTS = "app.momentum_universe_instruments"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE {_SNAPSHOTS} (
            exchange          VARCHAR(32)  NOT NULL,
            universe_version  VARCHAR(64)  NOT NULL,
            catalog_version   VARCHAR(64)  NOT NULL,
            capture_version   VARCHAR(32)  NOT NULL,
            schema_version    VARCHAR(32)  NOT NULL,
            captured_at       TIMESTAMPTZ  NOT NULL,
            instrument_count  INTEGER      NOT NULL,
            payload_hash      BYTEA        NOT NULL,
            created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),

            PRIMARY KEY (exchange, universe_version, catalog_version),
            CONSTRAINT payload_hash_is_sha256
                CHECK (octet_length(payload_hash) = 32),
            CONSTRAINT instrument_count_non_negative
                CHECK (instrument_count >= 0)
        )
    """)

    # Most recent snapshot per exchange is the one query this table exists
    # to answer quickly (a future capture-startup check, a future
    # resolution step's own "latest per venue" read); the primary key
    # alone would need a full scan for that.
    op.execute(f"""
        CREATE INDEX ix_momentum_universe_snapshots_exchange_captured_at
            ON {_SNAPSHOTS} (exchange, captured_at DESC)
    """)

    op.execute(f"""
        CREATE TABLE {_INSTRUMENTS} (
            exchange              VARCHAR(32)  NOT NULL,
            universe_version      VARCHAR(64)  NOT NULL,
            catalog_version       VARCHAR(64)  NOT NULL,
            native_market_id      VARCHAR(64)  NOT NULL,
            base                  VARCHAR(32)  NOT NULL,
            quote                 VARCHAR(32)  NOT NULL,
            settle                VARCHAR(32)  NOT NULL,
            native_market_type    VARCHAR(64)  NOT NULL,
            canonical_market_type VARCHAR(64)  NOT NULL,
            onboarded_at          TIMESTAMPTZ,
            identity_status       VARCHAR(32)  NOT NULL,
            identity_key          TEXT,
            metadata_hash         BYTEA        NOT NULL,

            PRIMARY KEY (exchange, universe_version, catalog_version, native_market_id),
            FOREIGN KEY (exchange, universe_version, catalog_version)
                REFERENCES {_SNAPSHOTS} (exchange, universe_version, catalog_version)
                ON DELETE CASCADE,
            CONSTRAINT metadata_hash_is_sha256
                CHECK (octet_length(metadata_hash) = 32),
            CONSTRAINT identity_status_known_value CHECK (
                identity_status IN (
                    'ready',
                    'missing_onboarded_at',
                    'invalid_onboarded_at',
                    'invalid_assets',
                    'unsupported_market_type'
                )
            ),
            -- The fail-closed invariant itself, enforced at the DB level:
            -- identity_key and onboarded_at are both non-NULL if and only
            -- if identity_status = 'ready'. Symmetric on purpose (an
            -- earlier version only constrained identity_key on the
            -- non-ready branch, which would have silently accepted a
            -- non-ready row carrying a stale onboarded_at) -- a bug in
            -- whatever wrote the row, not data this table will silently
            -- accept.
            CONSTRAINT identity_key_only_when_ready CHECK (
                (identity_status = 'ready'
                    AND identity_key IS NOT NULL
                    AND onboarded_at IS NOT NULL)
                OR (identity_status != 'ready'
                    AND identity_key IS NULL
                    AND onboarded_at IS NULL)
            )
        )
    """)

    # identity_key lookups are the shape a future cross-venue resolution
    # step needs (partial index: only 'ready' rows ever have a non-NULL
    # key, so there is nothing to index on the rest).
    op.execute(f"""
        CREATE INDEX ix_momentum_universe_instruments_identity_key
            ON {_INSTRUMENTS} (identity_key)
            WHERE identity_key IS NOT NULL
    """)


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {_INSTRUMENTS}")
    op.execute(f"DROP TABLE IF EXISTS {_SNAPSHOTS}")
