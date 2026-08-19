"""bybit momentum bars 1m hypertable

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-10

First table in the `timeseries` schema (see init-db.sql: "Schema for
time-series data: OHLCV, ticks, funding, OI"), which existed but was
otherwise empty until this migration. Backs the momentum-capture line
(ROADMAP "Active course" item 5, feat/bybit-early-momentum-capture-v1).

Design decisions, from independent review before this migration was
written:

- Primary key is (exchange, market_type, symbol, capture_version,
  bucket_start), not just (symbol, bucket_start): a future schema/contract
  version bump or a second market_type must never collide with rows an
  older contract wrote. capture_version pins which histogram bucket
  boundaries and field set a row was written under; universe_version is a
  separate, per-run value (which exact frozen symbol set was live when
  this row was captured), not part of the identity key.
- Histogram and top-K are native typed arrays (integer[]/double
  precision[]), not JSONB: at roughly 700 symbols x 1440 minutes/day
  (~1M rows/day), JSONB's per-row repeated key overhead is real, and array
  length is already disambiguated by capture_version.
- payload_hash lets the writer distinguish "this is a harmless retry of a
  row we already wrote" from "two different computed rows collided on the
  same primary key", which a bare ON CONFLICT DO NOTHING cannot do.
- Chunk interval, compression, and retention are set here rather than left
  to be configured ad hoc later. Retention defaults to 35 days (a 4-week
  lookback plus margin); revisit before relying on it for anything needing
  a longer window.
- Prices and OI are DOUBLE PRECISION, not NUMERIC: the whole pipeline
  upstream (Bybit's string ticker/trade fields, ohlcv.go, trades.go,
  momentum.go) is float64 end to end, so a decimal column here would claim
  a precision the data never actually had, while also costing more bytes
  on disk than a plain 8-byte float. NUMERIC belongs on tables backed by a
  real decimal source of truth (fills, positions), not this one.
- payload_hash is BYTEA (32 raw bytes), not a 64-character hex TEXT: same
  hash, roughly half the storage.
- Compression is scheduled at 1 day, not 7: a 7-day compress_after would
  mean the 48-72h resource canary this table exists for never observes any
  compression at all. Revisit once steady-state operation (rather than a
  short canary) is the actual concern.

Storage was independently measured (not projected from a single row) before
this migration was finalized: a real 18-minute capture against the live
735-symbol universe (13,240 rows), plus that same real data tiled across a
synthetic ~1-day depth (993,000 rows) to get a realistic per-symbol
compression-batch depth. Results: heap+PK-index hot footprint is
~1143 bytes/row (~1.14 GiB/day uncompressed at full 735-symbol/1440-minute
scale, under the 1.5-2 GiB hot-chunk gate); manually compressing the
realistic-depth chunk gave 6.22x compression (vs only 1.48x at the
too-shallow 18-minute depth, confirming batch depth, not row width, drives
the ratio), projecting to roughly 188 MiB/day compressed at that ratio.
The tiled data is exact-content repeats across tiles, which likely makes
6.22x mildly optimistic versus genuinely varied production data (real
zero-trade bars, ~26% of the 18-minute sample, compress just as well either
way; smoothly-varying real values compress somewhat less than bit-identical
repeats). Treat 188 MiB/day as the optimistic end and confirm against the
actual canary's first compressed day before relying on it long-term.

Two CHECK constraints below encode invariants a future writer or migration
bug could otherwise silently violate: payload_hash must be exactly the
32 raw bytes of a SHA-256 digest, and complete can only be true when both
ticker_complete and trades_complete are (matching how momentum.Bar itself
derives Complete).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "timeseries.bybit_momentum_bars_1m"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE {_TABLE} (
            exchange          VARCHAR(32)  NOT NULL DEFAULT 'bybit',
            market_type       VARCHAR(16)  NOT NULL DEFAULT 'linear',
            symbol            VARCHAR(32)  NOT NULL,
            capture_version   VARCHAR(32)  NOT NULL,
            bucket_start      TIMESTAMPTZ  NOT NULL,
            universe_version  VARCHAR(64)  NOT NULL,

            open_price      DOUBLE PRECISION,
            high_price      DOUBLE PRECISION,
            low_price       DOUBLE PRECISION,
            close_price     DOUBLE PRECISION,
            last_bid_price  DOUBLE PRECISION,
            last_ask_price  DOUBLE PRECISION,

            buy_total_notional_usd       DOUBLE PRECISION NOT NULL DEFAULT 0,
            buy_trade_count              INTEGER NOT NULL DEFAULT 0,
            buy_hist_counts              INTEGER[] NOT NULL,
            buy_hist_notional            DOUBLE PRECISION[] NOT NULL,
            buy_top_notional             DOUBLE PRECISION[] NOT NULL DEFAULT '{{}}',
            buy_max_10s_notional_usd     DOUBLE PRECISION NOT NULL DEFAULT 0,
            buy_max_30s_notional_usd     DOUBLE PRECISION NOT NULL DEFAULT 0,
            buy_block_trade_count        INTEGER NOT NULL DEFAULT 0,
            buy_block_trade_notional_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
            buy_rpi_trade_count          INTEGER NOT NULL DEFAULT 0,
            buy_rpi_trade_notional_usd   DOUBLE PRECISION NOT NULL DEFAULT 0,

            sell_total_notional_usd       DOUBLE PRECISION NOT NULL DEFAULT 0,
            sell_trade_count              INTEGER NOT NULL DEFAULT 0,
            sell_hist_counts              INTEGER[] NOT NULL,
            sell_hist_notional            DOUBLE PRECISION[] NOT NULL,
            sell_top_notional             DOUBLE PRECISION[] NOT NULL DEFAULT '{{}}',
            sell_max_10s_notional_usd     DOUBLE PRECISION NOT NULL DEFAULT 0,
            sell_max_30s_notional_usd     DOUBLE PRECISION NOT NULL DEFAULT 0,
            sell_block_trade_count        INTEGER NOT NULL DEFAULT 0,
            sell_block_trade_notional_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
            sell_rpi_trade_count          INTEGER NOT NULL DEFAULT 0,
            sell_rpi_trade_notional_usd   DOUBLE PRECISION NOT NULL DEFAULT 0,

            open_interest                    DOUBLE PRECISION,
            open_interest_event_at           TIMESTAMPTZ,
            open_interest_observed_at        TIMESTAMPTZ,
            open_interest_value              DOUBLE PRECISION,
            open_interest_value_event_at     TIMESTAMPTZ,
            open_interest_value_observed_at  TIMESTAMPTZ,
            ticker_observed_this_minute      BOOLEAN NOT NULL DEFAULT FALSE,

            trade_count               INTEGER NOT NULL DEFAULT 0,
            duplicate_trades_dropped  INTEGER NOT NULL DEFAULT 0,
            late_trades_dropped       INTEGER NOT NULL DEFAULT 0,

            first_trade_event_at     TIMESTAMPTZ,
            last_trade_event_at      TIMESTAMPTZ,
            first_trade_received_at  TIMESTAMPTZ,
            last_trade_received_at   TIMESTAMPTZ,
            trade_lag_sum_ms         BIGINT NOT NULL DEFAULT 0,
            trade_lag_max_ms         BIGINT NOT NULL DEFAULT 0,
            trade_lag_count          INTEGER NOT NULL DEFAULT 0,
            min_trade_seq            BIGINT,
            max_trade_seq            BIGINT,
            out_of_order_trade_count INTEGER NOT NULL DEFAULT 0,

            first_ticker_event_at     TIMESTAMPTZ,
            last_ticker_event_at      TIMESTAMPTZ,
            first_ticker_received_at  TIMESTAMPTZ,
            last_ticker_received_at   TIMESTAMPTZ,
            ticker_lag_sum_ms         BIGINT NOT NULL DEFAULT 0,
            ticker_lag_max_ms         BIGINT NOT NULL DEFAULT 0,
            ticker_lag_count          INTEGER NOT NULL DEFAULT 0,

            unbackfilled_gap_minutes  INTEGER NOT NULL DEFAULT 0,
            unbackfilled_gap_from     TIMESTAMPTZ,
            unbackfilled_gap_to       TIMESTAMPTZ,

            ticker_complete  BOOLEAN NOT NULL,
            trades_complete  BOOLEAN NOT NULL,
            complete         BOOLEAN NOT NULL,

            payload_hash  BYTEA NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

            PRIMARY KEY (exchange, market_type, symbol, capture_version, bucket_start),
            CONSTRAINT payload_hash_is_sha256 CHECK (octet_length(payload_hash) = 32),
            CONSTRAINT complete_requires_both_feeds
                CHECK (NOT complete OR (ticker_complete AND trades_complete))
        )
    """)

    # create_hypertable's default index (bucket_start) plus the primary key
    # above cover the two dominant query shapes (one symbol's history;
    # everything in a time range) without a hand-added secondary index.
    op.execute(f"""
        SELECT create_hypertable(
            '{_TABLE}', 'bucket_start',
            chunk_time_interval => INTERVAL '1 day'
        )
    """)

    # Compression eligibility is scoped by segmentby: queries filtering on
    # these columns can still skip decompressing whole segments.
    op.execute(f"""
        ALTER TABLE {_TABLE} SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'exchange, market_type, symbol, capture_version',
            timescaledb.compress_orderby = 'bucket_start'
        )
    """)
    op.execute(f"SELECT add_compression_policy('{_TABLE}', INTERVAL '1 day')")

    # 35 days: a 4-week discovery lookback plus margin. Reassess before any
    # decision that needs a longer window relies on this table.
    op.execute(f"SELECT add_retention_policy('{_TABLE}', INTERVAL '35 days')")


def downgrade() -> None:
    op.execute(f"SELECT remove_retention_policy('{_TABLE}', if_exists => true)")
    op.execute(f"SELECT remove_compression_policy('{_TABLE}', if_exists => true)")
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
