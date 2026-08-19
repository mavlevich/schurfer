"""add prospective momentum-flow WATCH audit

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-14

The run row freezes the first prospective cohort boundary and contract hash. The
hypertable records one compact evaluation per exact venue instrument and closed UTC
minute, including quality and signal rejections. This deliberate denominator is
required to measure opportunity rate, false-WATCH rate, and precursor recall without
reconstructing decisions after outcomes are known.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNS = "app.momentum_flow_watch_runs"
_STATES = "app.momentum_flow_watch_states"
_EVALUATIONS = "timeseries.momentum_flow_watch_evaluations_1m"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE {_RUNS} (
            watch_version       VARCHAR(64) PRIMARY KEY,
            contract_sha256     CHAR(64) NOT NULL,
            contract_json       JSONB NOT NULL,
            cohort_started_at   TIMESTAMPTZ NOT NULL,
            last_bucket_start   TIMESTAMPTZ,
            status              VARCHAR(16) NOT NULL DEFAULT 'active',
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT momentum_flow_watch_runs_contract_sha256
                CHECK (contract_sha256 ~ '^[0-9a-f]{{64}}$'),
            CONSTRAINT momentum_flow_watch_runs_contract_json
                CHECK (jsonb_typeof(contract_json) = 'object'),
            CONSTRAINT momentum_flow_watch_runs_status
                CHECK (status IN ('active', 'stopped'))
        )
    """)

    op.execute(f"""
        CREATE TABLE {_STATES} (
            watch_version       VARCHAR(64) NOT NULL
                REFERENCES {_RUNS}(watch_version) ON DELETE CASCADE,
            exchange            VARCHAR(32) NOT NULL,
            market_type         VARCHAR(16) NOT NULL,
            symbol              VARCHAR(32) NOT NULL,
            active_episode      BOOLEAN NOT NULL,
            clear_streak        INTEGER NOT NULL,
            last_watch_at       TIMESTAMPTZ,
            episode_id          UUID,
            last_bucket_start   TIMESTAMPTZ NOT NULL,
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (watch_version, exchange, market_type, symbol),
            CONSTRAINT momentum_flow_watch_states_clear_streak
                CHECK (clear_streak >= 0),
            CONSTRAINT momentum_flow_watch_states_episode_shape CHECK (
                (active_episode AND episode_id IS NOT NULL AND last_watch_at IS NOT NULL)
                OR (NOT active_episode AND episode_id IS NULL)
            )
        )
    """)

    op.execute(f"""
        CREATE TABLE {_EVALUATIONS} (
            exchange            VARCHAR(32) NOT NULL,
            market_type         VARCHAR(16) NOT NULL,
            symbol              VARCHAR(32) NOT NULL,
            capture_version     VARCHAR(32) NOT NULL,
            watch_version       VARCHAR(64) NOT NULL,
            bucket_start        TIMESTAMPTZ NOT NULL,
            universe_version    VARCHAR(64) NOT NULL,

            quality_ready       BOOLEAN NOT NULL,
            raw_qualified       BOOLEAN NOT NULL,
            decision_status     VARCHAR(40) NOT NULL,
            reason_codes        TEXT[] NOT NULL DEFAULT '{{}}',

            price_return_60m_pct                 DOUBLE PRECISION,
            price_return_15m_pct                 DOUBLE PRECISION,
            oi_growth_60m_pct                    DOUBLE PRECISION,
            buy_notional_15m_usd                 DOUBLE PRECISION,
            sell_notional_15m_usd                DOUBLE PRECISION,
            flow_notional_15m_usd                DOUBLE PRECISION,
            buy_imbalance_15m                    DOUBLE PRECISION,
            flow_acceleration_15m_vs_prior_45m   DOUBLE PRECISION,

            cross_section_size                   INTEGER NOT NULL,
            oi_growth_threshold_pct              DOUBLE PRECISION,
            buy_imbalance_threshold              DOUBLE PRECISION,
            flow_acceleration_threshold          DOUBLE PRECISION,

            source_event_at      TIMESTAMPTZ,
            source_received_at   TIMESTAMPTZ,
            bucket_ready_at      TIMESTAMPTZ,
            evaluator_started_at TIMESTAMPTZ NOT NULL,
            evaluator_completed_at TIMESTAMPTZ NOT NULL,
            decision_at          TIMESTAMPTZ NOT NULL,

            episode_id           UUID,
            watch_id             UUID,
            state_active_after   BOOLEAN NOT NULL,
            state_clear_streak_after INTEGER NOT NULL,
            state_last_watch_at_after TIMESTAMPTZ,

            input_hash           BYTEA NOT NULL,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

            PRIMARY KEY (exchange, market_type, symbol, watch_version, bucket_start),
            CONSTRAINT momentum_flow_watch_evaluations_status CHECK (
                decision_status IN (
                    'watch', 'rejected_quality', 'rejected_signal',
                    'suppressed_active_episode', 'suppressed_cooldown'
                )
            ),
            CONSTRAINT momentum_flow_watch_evaluations_hash
                CHECK (octet_length(input_hash) = 32),
            CONSTRAINT momentum_flow_watch_evaluations_clear_streak
                CHECK (state_clear_streak_after >= 0),
            CONSTRAINT momentum_flow_watch_evaluations_cross_section
                CHECK (cross_section_size >= 0),
            CONSTRAINT momentum_flow_watch_evaluations_timing CHECK (
                evaluator_completed_at >= evaluator_started_at
                AND decision_at >= evaluator_started_at
                AND decision_at <= evaluator_completed_at
            ),
            CONSTRAINT momentum_flow_watch_evaluations_watch_shape CHECK (
                (decision_status = 'watch' AND raw_qualified AND watch_id IS NOT NULL)
                OR (decision_status <> 'watch' AND watch_id IS NULL)
            ),
            CONSTRAINT momentum_flow_watch_evaluations_signal_shape CHECK (
                (raw_qualified AND decision_status IN (
                    'watch', 'suppressed_active_episode', 'suppressed_cooldown'
                ))
                OR (NOT raw_qualified AND decision_status IN (
                    'rejected_quality', 'rejected_signal'
                ))
            ),
            CONSTRAINT momentum_flow_watch_evaluations_quality_shape CHECK (
                (quality_ready AND decision_status <> 'rejected_quality')
                OR (NOT quality_ready AND decision_status = 'rejected_quality')
            )
        )
    """)

    op.execute(f"""
        SELECT create_hypertable(
            '{_EVALUATIONS}', 'bucket_start',
            chunk_time_interval => INTERVAL '1 day'
        )
    """)
    op.execute(f"""
        ALTER TABLE {_EVALUATIONS} SET (
            timescaledb.compress,
            timescaledb.compress_segmentby =
                'exchange, market_type, symbol, watch_version',
            timescaledb.compress_orderby = 'bucket_start'
        )
    """)
    op.execute(f"SELECT add_compression_policy('{_EVALUATIONS}', INTERVAL '1 day')")
    op.execute(f"SELECT add_retention_policy('{_EVALUATIONS}', INTERVAL '45 days')")
    op.execute(f"""
        CREATE INDEX momentum_flow_watch_evaluations_watch_decisions
        ON {_EVALUATIONS} (watch_version, bucket_start DESC)
        WHERE decision_status = 'watch'
    """)


def downgrade() -> None:
    op.execute(f"SELECT remove_retention_policy('{_EVALUATIONS}', if_exists => true)")
    op.execute(f"SELECT remove_compression_policy('{_EVALUATIONS}', if_exists => true)")
    op.execute(f"DROP TABLE IF EXISTS {_EVALUATIONS}")
    op.execute(f"DROP TABLE IF EXISTS {_STATES}")
    op.execute(f"DROP TABLE IF EXISTS {_RUNS}")
