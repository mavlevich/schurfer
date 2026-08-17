"""momentum bars: trade-derived price provenance + capability completeness

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-17

Part of feat/momentum-trade-price-source-v1 (ROADMAP item 8's own Binance
sub-line, PR2 of the 5-PR Binance-remediation sequence following
fix/binance-watch-input-readiness-v1). See docs/research/
momentum-trade-price-source-v1.md and docs/research/
binance-watch-input-readiness-v1.md for the incident this line of work
exists to fix: momentum_flow_watch_binance produced zero decisions for
32+ hours because Binance capture bars never populated close_price at
all (no ticker/price feed exists for Binance -- see
cmd/momentumcapturebinance's own package doc comment).

Adds nullable columns for two things momentum.Engine now tracks (see
that package's own PriceSource/Bar doc comments), neither of which
existed anywhere in this schema before:

  Price provenance -- which observation type a bar's own OHLC actually
  came from, populated identically in spirit for BOTH venues (Bybit's
  own AddTickerObservation mirrors these from its existing Ticker*
  diagnostics; Binance's own AddTrade populates them from accepted
  aggTrade prices): price_source, first_price_event_at,
  last_price_event_at, first_price_received_at, last_price_received_at,
  price_observed_this_minute.

  Capability-specific completeness -- open_interest_complete/
  price_complete are the SAME underlying feed-health signals
  ticker_complete/trades_complete already track, under names that stay
  accurate for a venue (Binance) whose own AddTickerObservation calls
  only ever carry OI, never a genuine ticker. ticker_complete/
  trades_complete themselves are NOT renamed here (a bigger, separate
  change deliberately not made now -- a colleague review's own finding);
  these are additive, not a replacement.

All nullable, no backfill: every column here describes something that
was never computed for any bar written before this migration, and NULL
honestly says "not tracked at the time" rather than a default value
implying a false negative (e.g. price_complete=false would incorrectly
read as "known incomplete" for historical rows that were never
evaluated against this concept at all).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BARS = "timeseries.bybit_momentum_bars_1m"


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE {_BARS}
            ADD COLUMN price_source              VARCHAR(32),
            ADD COLUMN first_price_event_at       TIMESTAMPTZ,
            ADD COLUMN last_price_event_at        TIMESTAMPTZ,
            ADD COLUMN first_price_received_at    TIMESTAMPTZ,
            ADD COLUMN last_price_received_at     TIMESTAMPTZ,
            ADD COLUMN price_observed_this_minute BOOLEAN,
            ADD COLUMN open_interest_complete      BOOLEAN,
            ADD COLUMN price_complete              BOOLEAN
    """)


def downgrade() -> None:
    op.execute(f"""
        ALTER TABLE {_BARS}
            DROP COLUMN IF EXISTS price_source,
            DROP COLUMN IF EXISTS first_price_event_at,
            DROP COLUMN IF EXISTS last_price_event_at,
            DROP COLUMN IF EXISTS first_price_received_at,
            DROP COLUMN IF EXISTS last_price_received_at,
            DROP COLUMN IF EXISTS price_observed_this_minute,
            DROP COLUMN IF EXISTS open_interest_complete,
            DROP COLUMN IF EXISTS price_complete
    """)
