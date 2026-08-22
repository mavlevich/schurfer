"""add early momentum episode lifecycle and trade idempotency key

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-22

Durable candidate->episode->claim->open lifecycle for early_momentum_v3.
Postgres is the source of truth: a partial unique index enforces at most one
live (armed/claimed) episode per instrument, and the claim UPDATE is a
single atomic statement so a crashed worker's stale claim can be safely
reclaimed by a later tick without a second, race-prone "release" step.

status/terminal_reason are deliberately separate columns (not one enum with
a value per rejection cause) so the state machine and any CHECK constraints
stay small and the reason vocabulary can grow without touching status.

app.trades gets two additive, nullable columns: episode_id (not unique --
a future scale-in leg reuses the same episode) and entry_idempotency_key,
whose partial unique index every idempotent INSERT must reference with the
identical WHERE clause for Postgres to infer it as the conflict target.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EPISODES = "app.early_momentum_episodes"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE {_EPISODES} (
            episode_id           UUID PRIMARY KEY,
            strategy_id           BIGINT NOT NULL REFERENCES app.strategies(id),
            contract_sha256       BYTEA NOT NULL,

            source_exchange       VARCHAR(32) NOT NULL,
            source_native_id      VARCHAR(64) NOT NULL,

            exchange               VARCHAR(32) NOT NULL,
            native_market_id       VARCHAR(64) NOT NULL,
            -- Nullable: the scanner only resolves the route (native ids +
            -- identity keys) at ARM time -- it has no live exchange client
            -- to derive the CCXT-unified symbol from. Filled in once the
            -- trigger loop resolves it via a live client at claim time.
            execution_symbol       VARCHAR(64),
            execution_identity_key TEXT NOT NULL,
            source_identity_key    TEXT NOT NULL,
            cluster_key             TEXT NOT NULL,

            ceiling               NUMERIC(30, 14) NOT NULL,
            features              JSONB NOT NULL,

            armed_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at            TIMESTAMPTZ NOT NULL,

            status                VARCHAR(16) NOT NULL,
            terminal_reason       TEXT,

            claim_token           UUID,
            claimed_at            TIMESTAMPTZ,
            claim_expires_at      TIMESTAMPTZ,
            claim_attempts        INTEGER NOT NULL DEFAULT 0,

            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT early_momentum_episodes_contract_hash
                CHECK (octet_length(contract_sha256) = 32),
            CONSTRAINT early_momentum_episodes_status
                CHECK (status IN (
                    'armed', 'claimed', 'opened', 'closed',
                    'expired', 'rejected', 'suppressed'
                )),
            CONSTRAINT early_momentum_episodes_terminal_reason_shape
                CHECK (
                    (status IN ('armed', 'claimed', 'opened', 'closed')
                        AND terminal_reason IS NULL)
                    OR (status IN ('expired', 'rejected', 'suppressed')
                        AND terminal_reason IS NOT NULL)
                ),
            CONSTRAINT early_momentum_episodes_claim_shape
                CHECK (
                    (status = 'claimed'
                        AND claim_token IS NOT NULL
                        AND claimed_at IS NOT NULL
                        AND claim_expires_at IS NOT NULL)
                    OR status <> 'claimed'
                ),
            CONSTRAINT early_momentum_episodes_claim_attempts
                CHECK (claim_attempts >= 0)
        )
    """)
    op.execute(f"""
        CREATE UNIQUE INDEX ux_early_momentum_episodes_live_instrument
        ON {_EPISODES} (exchange, native_market_id)
        WHERE status IN ('armed', 'claimed')
    """)
    op.execute(f"""
        CREATE INDEX ix_early_momentum_episodes_armed_expiry
        ON {_EPISODES} (expires_at)
        WHERE status = 'armed'
    """)
    op.execute(f"""
        CREATE INDEX ix_early_momentum_episodes_claim_expiry
        ON {_EPISODES} (claim_expires_at)
        WHERE status = 'claimed'
    """)
    op.execute(f"""
        CREATE INDEX ix_early_momentum_episodes_instrument_recent
        ON {_EPISODES} (exchange, native_market_id, armed_at DESC)
    """)

    op.execute("""
        ALTER TABLE app.trades
        ADD COLUMN episode_id UUID NULL
            REFERENCES app.early_momentum_episodes(episode_id),
        ADD COLUMN entry_idempotency_key TEXT NULL
    """)
    op.execute("""
        CREATE INDEX ix_trades_episode_id ON app.trades (episode_id)
        WHERE episode_id IS NOT NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_trades_entry_idempotency_key
        ON app.trades (entry_idempotency_key)
        WHERE entry_idempotency_key IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.ux_trades_entry_idempotency_key")
    op.execute("DROP INDEX IF EXISTS app.ix_trades_episode_id")
    op.execute("ALTER TABLE app.trades DROP COLUMN IF EXISTS entry_idempotency_key")
    op.execute("ALTER TABLE app.trades DROP COLUMN IF EXISTS episode_id")
    op.execute(f"DROP TABLE IF EXISTS {_EPISODES}")
