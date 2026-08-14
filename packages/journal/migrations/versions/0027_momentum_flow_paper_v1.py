"""add prospective momentum-flow paper probe

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-14

Every WATCH is claimed before its non-recoverable entry quote is requested. A worker
crash therefore becomes an explicit interrupted entry instead of a later fabricated
fill. Horizon rows are created at entry time so missed executable quotes remain in the
denominator.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNS = "app.momentum_flow_paper_runs"
_PROBES = "app.momentum_flow_paper_probes"
_OUTCOMES = "app.momentum_flow_paper_outcomes"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE {_RUNS} (
            paper_version       VARCHAR(64) PRIMARY KEY,
            contract_sha256     CHAR(64) NOT NULL,
            contract_json       JSONB NOT NULL,
            cohort_started_at   TIMESTAMPTZ NOT NULL,
            status              VARCHAR(16) NOT NULL DEFAULT 'active',
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT momentum_flow_paper_runs_hash
                CHECK (contract_sha256 ~ '^[0-9a-f]{{64}}$'),
            CONSTRAINT momentum_flow_paper_runs_contract
                CHECK (jsonb_typeof(contract_json) = 'object'),
            CONSTRAINT momentum_flow_paper_runs_status
                CHECK (status IN ('active', 'stopped'))
        )
    """)
    op.execute(f"""
        CREATE TABLE {_PROBES} (
            paper_id            UUID PRIMARY KEY,
            paper_version       VARCHAR(64) NOT NULL
                REFERENCES {_RUNS}(paper_version) ON DELETE RESTRICT,
            watch_version       VARCHAR(64) NOT NULL,
            watch_id            UUID NOT NULL,
            episode_id          UUID NOT NULL,
            exchange            VARCHAR(32) NOT NULL,
            market_type         VARCHAR(16) NOT NULL,
            symbol              VARCHAR(32) NOT NULL,
            watch_bucket_start  TIMESTAMPTZ NOT NULL,
            watch_decision_at   TIMESTAMPTZ NOT NULL,
            claimed_at          TIMESTAMPTZ NOT NULL,

            entry_status        VARCHAR(32) NOT NULL DEFAULT 'pending',
            entry_reason        VARCHAR(64),
            entry_quote_requested_at TIMESTAMPTZ,
            entry_quote_observed_at  TIMESTAMPTZ,
            entry_exchange_event_at  TIMESTAMPTZ,
            entry_quote_latency_ms   INTEGER,
            unified_symbol      VARCHAR(64),
            market_id           VARCHAR(64),
            contract_size       DOUBLE PRECISION,
            entry_best_bid      DOUBLE PRECISION,
            entry_best_ask      DOUBLE PRECISION,
            entry_mid           DOUBLE PRECISION,
            entry_spread_bps    DOUBLE PRECISION,
            entry_vwap          DOUBLE PRECISION,
            entry_impact_bps    DOUBLE PRECISION,
            entry_filled_notional_usd DOUBLE PRECISION,
            entry_at            TIMESTAMPTZ,

            position_status     VARCHAR(24) NOT NULL DEFAULT 'not_open',
            exit_reason         VARCHAR(32),
            exit_quote_requested_at TIMESTAMPTZ,
            exit_quote_observed_at  TIMESTAMPTZ,
            exit_exchange_event_at  TIMESTAMPTZ,
            exit_quote_latency_ms   INTEGER,
            exit_best_bid       DOUBLE PRECISION,
            exit_best_ask       DOUBLE PRECISION,
            exit_mid            DOUBLE PRECISION,
            exit_spread_bps     DOUBLE PRECISION,
            exit_vwap           DOUBLE PRECISION,
            exit_impact_bps     DOUBLE PRECISION,
            exit_filled_notional_usd DOUBLE PRECISION,
            exit_at             TIMESTAMPTZ,

            max_favorable_return_pct DOUBLE PRECISION,
            max_adverse_return_pct   DOUBLE PRECISION,
            gross_return_pct    DOUBLE PRECISION,
            net_return_pct      DOUBLE PRECISION,
            gross_pnl_usd       DOUBLE PRECISION,
            net_pnl_usd         DOUBLE PRECISION,
            fees_usd            DOUBLE PRECISION,
            funding_usd         DOUBLE PRECISION,
            accounting_status   VARCHAR(16),
            accounting_error    TEXT,
            last_error          TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT uq_momentum_flow_paper_watch UNIQUE (paper_version, watch_id),
            CONSTRAINT momentum_flow_paper_entry_status CHECK (
                entry_status IN (
                    'pending', 'opened', 'rejected_stale', 'rejected_quote',
                    'unresolved_interrupted'
                )
            ),
            CONSTRAINT momentum_flow_paper_position_status CHECK (
                position_status IN ('not_open', 'open', 'closed', 'exit_unresolved')
            ),
            CONSTRAINT momentum_flow_paper_entry_shape CHECK (
                (entry_status = 'opened'
                    AND entry_at IS NOT NULL
                    AND entry_vwap > 0
                    AND entry_filled_notional_usd > 0
                    AND position_status IN ('open', 'closed', 'exit_unresolved'))
                OR
                (entry_status <> 'opened'
                    AND entry_at IS NULL
                    AND position_status = 'not_open')
            ),
            CONSTRAINT momentum_flow_paper_exit_shape CHECK (
                (position_status = 'closed'
                    AND exit_at IS NOT NULL
                    AND exit_vwap > 0
                    AND exit_reason IN ('stop_loss', 'max_hold'))
                OR
                (position_status <> 'closed' AND exit_at IS NULL)
            ),
            CONSTRAINT momentum_flow_paper_quote_latency CHECK (
                (entry_quote_latency_ms IS NULL OR entry_quote_latency_ms >= 0)
                AND (exit_quote_latency_ms IS NULL OR exit_quote_latency_ms >= 0)
            ),
            CONSTRAINT momentum_flow_paper_watch_timing CHECK (
                watch_decision_at >= watch_bucket_start
                AND claimed_at >= watch_decision_at
            )
        )
    """)
    op.execute(f"""
        CREATE TABLE {_OUTCOMES} (
            paper_id            UUID NOT NULL
                REFERENCES {_PROBES}(paper_id) ON DELETE CASCADE,
            horizon_minutes     INTEGER NOT NULL,
            due_at              TIMESTAMPTZ NOT NULL,
            status              VARCHAR(24) NOT NULL DEFAULT 'pending',
            quote_requested_at  TIMESTAMPTZ,
            quote_observed_at   TIMESTAMPTZ,
            exchange_event_at   TIMESTAMPTZ,
            quote_latency_ms    INTEGER,
            best_bid            DOUBLE PRECISION,
            best_ask            DOUBLE PRECISION,
            mid                 DOUBLE PRECISION,
            spread_bps          DOUBLE PRECISION,
            bid_vwap            DOUBLE PRECISION,
            bid_impact_bps      DOUBLE PRECISION,
            filled_notional_usd DOUBLE PRECISION,
            gross_return_pct    DOUBLE PRECISION,
            net_return_pct      DOUBLE PRECISION,
            gross_pnl_usd       DOUBLE PRECISION,
            net_pnl_usd         DOUBLE PRECISION,
            fees_usd            DOUBLE PRECISION,
            funding_usd         DOUBLE PRECISION,
            accounting_status   VARCHAR(16),
            accounting_error    TEXT,
            error               TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (paper_id, horizon_minutes),
            CONSTRAINT momentum_flow_paper_outcome_horizon CHECK (horizon_minutes > 0),
            CONSTRAINT momentum_flow_paper_outcome_status CHECK (
                status IN ('pending', 'complete', 'missed_deadline')
            ),
            CONSTRAINT momentum_flow_paper_outcome_shape CHECK (
                (status = 'pending' AND quote_observed_at IS NULL)
                OR (status = 'complete' AND quote_observed_at IS NOT NULL AND bid_vwap > 0)
                OR (status = 'missed_deadline' AND quote_observed_at IS NULL AND error IS NOT NULL)
            ),
            CONSTRAINT momentum_flow_paper_outcome_latency CHECK (
                quote_latency_ms IS NULL OR quote_latency_ms >= 0
            )
        )
    """)
    op.execute(f"""
        CREATE INDEX momentum_flow_paper_probes_open
        ON {_PROBES} (paper_version, updated_at)
        WHERE position_status = 'open'
    """)
    op.execute(f"""
        CREATE INDEX momentum_flow_paper_outcomes_pending
        ON {_OUTCOMES} (due_at)
        WHERE status = 'pending'
    """)


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {_OUTCOMES}")
    op.execute(f"DROP TABLE IF EXISTS {_PROBES}")
    op.execute(f"DROP TABLE IF EXISTS {_RUNS}")
