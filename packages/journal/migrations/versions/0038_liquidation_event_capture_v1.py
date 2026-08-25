"""append-only public liquidation event capture

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-25

Bybit's allLiquidation stream and Binance's forceOrder snapshot are not the
same observation contract.  coverage_kind is therefore mandatory on every
row and heartbeat; consumers must never silently pool them as two complete
liquidation tapes.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENTS = "timeseries.liquidation_events"
_HEARTBEATS = "timeseries.liquidation_capture_heartbeats_1m"
_FINITE = "NOT IN ('NaN'::float8, 'Infinity'::float8, '-Infinity'::float8)"
_EVENT_SEGMENTBY = (
    "exchange, market_type, native_market_id, capture_version, coverage_kind, universe_version"
)
_HEARTBEAT_SEGMENTBY = "exchange, capture_version, coverage_kind, universe_version"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE {_EVENTS} (
            capture_version       VARCHAR(32) NOT NULL,
            exchange              VARCHAR(32) NOT NULL,
            market_type           VARCHAR(32) NOT NULL,
            native_market_id      VARCHAR(64) NOT NULL,
            universe_version      VARCHAR(64) NOT NULL,
            source_contract_variant VARCHAR(48) NOT NULL,
            coverage_kind         VARCHAR(40) NOT NULL,
            position_side         VARCHAR(8) NOT NULL,
            event_at              TIMESTAMPTZ NOT NULL,
            exchange_published_at TIMESTAMPTZ NOT NULL,
            received_at           TIMESTAMPTZ NOT NULL,
            persisted_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            source_session_id     VARCHAR(64) NOT NULL,
            source_event_key      BYTEA NOT NULL,
            payload_hash          BYTEA NOT NULL,
            quantity              DOUBLE PRECISION NOT NULL,
            quantity_unit         VARCHAR(32) NOT NULL,
            bankruptcy_price      DOUBLE PRECISION,
            order_price           DOUBLE PRECISION,
            average_price         DOUBLE PRECISION,
            last_filled_quantity  DOUBLE PRECISION,
            accumulated_filled_quantity DOUBLE PRECISION,
            estimated_liquidation_notional DOUBLE PRECISION,
            raw_payload           JSONB NOT NULL,
            PRIMARY KEY (exchange, event_at, source_event_key),
            CONSTRAINT liquidation_capture_version_v1
                CHECK (capture_version = 'liquidation_event_v1'),
            CONSTRAINT liquidation_coverage_kind_known
                CHECK (coverage_kind IN ('complete_stream', 'latest_per_symbol_1000ms')),
            CONSTRAINT liquidation_source_contract_variant_known
                CHECK (source_contract_variant IN (
                    'bybit_all_liquidation_v1',
                    'binance_merged_um_v1',
                    'binance_usdm_no_scope_tag_v1'
                )),
            CONSTRAINT liquidation_position_side_known
                CHECK (position_side IN ('long', 'short')),
            CONSTRAINT liquidation_hashes_sha256
                CHECK (octet_length(source_event_key) = 32 AND octet_length(payload_hash) = 32),
            CONSTRAINT liquidation_timestamps_ordered
                CHECK (event_at <= received_at + INTERVAL '5 seconds'
                    AND exchange_published_at <= received_at + INTERVAL '5 seconds'),
            CONSTRAINT liquidation_quantity_positive
                CHECK (quantity > 0 AND quantity {_FINITE}),
            CONSTRAINT liquidation_optional_numbers_positive
                CHECK (
                    (bankruptcy_price IS NULL OR
                        (bankruptcy_price > 0 AND bankruptcy_price {_FINITE}))
                    AND (order_price IS NULL OR
                        (order_price > 0 AND order_price {_FINITE}))
                    AND (average_price IS NULL OR
                        (average_price > 0 AND average_price {_FINITE}))
                    AND (last_filled_quantity IS NULL OR
                        (last_filled_quantity > 0 AND last_filled_quantity {_FINITE}))
                    AND (accumulated_filled_quantity IS NULL OR
                        (accumulated_filled_quantity > 0
                            AND accumulated_filled_quantity {_FINITE}))
                    AND (estimated_liquidation_notional IS NULL OR
                        (estimated_liquidation_notional > 0
                            AND estimated_liquidation_notional {_FINITE}))
                )
        )
    """)
    op.execute(f"""
        SELECT create_hypertable(
            '{_EVENTS}', 'event_at',
            chunk_time_interval => INTERVAL '1 day'
        )
    """)
    op.execute(f"""
        CREATE INDEX liquidation_events_symbol_time_idx
        ON {_EVENTS} (exchange, native_market_id, event_at DESC)
    """)
    op.execute(f"""
        ALTER TABLE {_EVENTS} SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = '{_EVENT_SEGMENTBY}',
            timescaledb.compress_orderby = 'event_at'
        )
    """)
    op.execute(f"SELECT add_compression_policy('{_EVENTS}', INTERVAL '1 day')")
    op.execute(f"SELECT add_retention_policy('{_EVENTS}', INTERVAL '180 days')")

    # A missing heartbeat is a durable coverage gap.  A zero-event minute
    # with a complete heartbeat is evidence of no observed events; the two
    # states must remain distinguishable in every future event study.
    op.execute(f"""
        CREATE TABLE {_HEARTBEATS} (
            exchange              VARCHAR(32) NOT NULL,
            capture_version       VARCHAR(32) NOT NULL,
            market_type           VARCHAR(32) NOT NULL,
            coverage_kind         VARCHAR(40) NOT NULL,
            process_session_id    VARCHAR(64) NOT NULL,
            universe_version      VARCHAR(64) NOT NULL,
            bucket_start          TIMESTAMPTZ NOT NULL,
            expected_connections  INTEGER NOT NULL,
            connected_connections INTEGER NOT NULL,
            data_loss_detected     BOOLEAN NOT NULL,
            complete               BOOLEAN NOT NULL,
            events_received_total  BIGINT NOT NULL,
            events_persisted_total BIGINT NOT NULL,
            duplicate_events_total BIGINT NOT NULL,
            queue_drops_total      BIGINT NOT NULL,
            invalid_events_total   BIGINT NOT NULL,
            out_of_scope_total     BIGINT NOT NULL,
            scope_tag_missing_accepted_total BIGINT NOT NULL,
            reconnect_total        BIGINT NOT NULL,
            read_timeout_total     BIGINT NOT NULL,
            recorded_at            TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (exchange, capture_version, process_session_id, bucket_start),
            CONSTRAINT liquidation_heartbeat_version_v1
                CHECK (capture_version = 'liquidation_event_v1'),
            CONSTRAINT liquidation_heartbeat_coverage_known
                CHECK (coverage_kind IN ('complete_stream', 'latest_per_symbol_1000ms')),
            CONSTRAINT liquidation_heartbeat_connections_valid
                CHECK (expected_connections > 0
                    AND connected_connections >= 0
                    AND connected_connections <= expected_connections),
            CONSTRAINT liquidation_heartbeat_complete_honest
                CHECK (NOT complete OR (
                    connected_connections = expected_connections
                    AND NOT data_loss_detected
                ))
        )
    """)
    op.execute(f"""
        SELECT create_hypertable(
            '{_HEARTBEATS}', 'bucket_start',
            chunk_time_interval => INTERVAL '7 days'
        )
    """)
    op.execute(f"""
        ALTER TABLE {_HEARTBEATS} SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = '{_HEARTBEAT_SEGMENTBY}',
            timescaledb.compress_orderby = 'bucket_start'
        )
    """)
    op.execute(f"SELECT add_compression_policy('{_HEARTBEATS}', INTERVAL '7 days')")
    op.execute(f"SELECT add_retention_policy('{_HEARTBEATS}', INTERVAL '180 days')")


def downgrade() -> None:
    for table in (_HEARTBEATS, _EVENTS):
        op.execute(f"SELECT remove_retention_policy('{table}', if_exists => true)")
        op.execute(f"SELECT remove_compression_policy('{table}', if_exists => true)")
        op.execute(f"DROP TABLE IF EXISTS {table}")
