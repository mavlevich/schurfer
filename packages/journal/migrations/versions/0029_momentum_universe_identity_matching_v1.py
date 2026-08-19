"""momentum universe cross-venue identity matching (asset clusters)

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-17

Resolution step of feat/momentum-universe-identity-resolution-v1 (ROADMAP
phase 3 item 8's own second half, following feat/momentum-universe-
identity-foundation-v1 / migration 0028). See docs/research/momentum-
universe-identity-foundation-v1.md's own "What a future resolution PR
inherits" section for the target match_status vocabulary this migration
implements: candidate, confirmed, conflict, insufficient_evidence,
manual_review_required, not_same_asset (not_same_asset is declared here
but not yet produced by the v1 classifier -- see momentum_universe_
identity_classifier.py's own module doc comment for why).

Deliberately venue-count-agnostic, not a Bybit/Binance-specific pairwise
table: a naive design would have hardcoded bybit_identity_key/
binance_identity_key columns, which stops working the moment a third venue
is added. Instead this is a cluster model -- one asset_clusters row per
real-world asset a matching run identifies, and N cluster_members rows
under it (one per venue's own instrument judged to belong to that asset).
Adding a 20th venue later is new rows in cluster_members, not a schema
change.

cluster_key is the base asset symbol itself (e.g. "BTC"), not a surrogate
id: matches every other table in this schema (bars/watch_runs/
momentum_universe_instruments all use natural keys, no BIGSERIAL). This
assumes base is unique per exchange within one matching run's own snapshot
read -- true of every live snapshot as of this migration (verified against
prod: 516 Bybit / 525 Binance ready instruments, zero duplicate bases
within either exchange) but not schema-enforced upstream, so
cluster_members' own primary key includes native_market_id specifically to
survive a violation rather than trust the assumption blindly: if one
exchange ever contributes two different instruments under the same base
in one matching run, both land as separate member rows with match_status
manual_review_required, surfacing the ambiguity instead of a PK collision
forcing the classifier to silently pick one and drop the other (see the
classifier's own doc comment).

A cluster_members row's own match_status is scoped to that ONE venue
instrument's membership, not the whole cluster: a cluster with three
members can have two confirmed and one conflict (e.g. two venues share a
long-established asset, a third venue's own same-ticker instrument onboarded
recently with no correlating evidence) -- collapsing that to one cluster-
level status would hide exactly the divergence a resolution step exists to
surface.

No FK from cluster_members back to momentum_universe_instruments: a
matching run reads the LATEST ready snapshot per exchange and is fully
recomputed (upserted) each time it runs, not permanently pinned to the
specific snapshot row that produced it -- the same reasoning
PersistUniverseSnapshot's own idempotent-upsert design already applies to
snapshots themselves, applied one layer up. identity_key is carried on each
member row for provenance/debugging (which exact versioned identity this
membership was computed from) but is not a live FK target, since a
snapshot's identity_key is not unique across repeated snapshots by design
(see momentum_universe_instruments' own migration 0028 docstring: an
unchanged instrument reproduces the same identity_key on every re-fetch).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLUSTERS = "app.momentum_universe_asset_clusters"
_MEMBERS = "app.momentum_universe_cluster_members"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE {_CLUSTERS} (
            cluster_key           TEXT         NOT NULL,
            base                  VARCHAR(32)  NOT NULL,
            canonical_market_type VARCHAR(64)  NOT NULL,
            match_ruleset_version VARCHAR(32)  NOT NULL,
            member_count          INTEGER      NOT NULL,
            resolved_at           TIMESTAMPTZ  NOT NULL,
            created_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),

            PRIMARY KEY (cluster_key),
            CONSTRAINT member_count_at_least_two
                CHECK (member_count >= 2)
        )
    """)

    op.execute(f"""
        CREATE TABLE {_MEMBERS} (
            cluster_key       TEXT         NOT NULL,
            exchange          VARCHAR(32)  NOT NULL,
            native_market_id  VARCHAR(64)  NOT NULL,
            identity_key      TEXT         NOT NULL,
            onboarded_at      TIMESTAMPTZ  NOT NULL,
            match_status      VARCHAR(32)  NOT NULL,
            match_reason      TEXT         NOT NULL,

            PRIMARY KEY (cluster_key, exchange, native_market_id),
            FOREIGN KEY (cluster_key)
                REFERENCES {_CLUSTERS} (cluster_key)
                ON DELETE CASCADE,
            CONSTRAINT match_status_known_value CHECK (
                match_status IN (
                    'candidate',
                    'confirmed',
                    'conflict',
                    'insufficient_evidence',
                    'manual_review_required',
                    'not_same_asset'
                )
            )
        )
    """)

    # A matching run needs "what is this venue's own current cluster
    # membership" as fast as "what is in this cluster" -- both directions,
    # not just the primary key's own (cluster_key, exchange) order.
    op.execute(f"""
        CREATE INDEX ix_momentum_universe_cluster_members_exchange
            ON {_MEMBERS} (exchange, native_market_id)
    """)


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {_MEMBERS}")
    op.execute(f"DROP TABLE IF EXISTS {_CLUSTERS}")
