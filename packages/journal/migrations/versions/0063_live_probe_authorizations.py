"""LIVE_PROBE authorization records and entry reservations.

Revision ID: 0063
Revises: 0062
Create Date: 2026-11-03

docs/research/live-probe-step2-design-v1.md, sections 2 and 4, and
docs/engineering/live-probe-loss-threshold-design-v1.md, sections 3 to 5.

  * ``live_probe_authorizations`` -- one row per probe, written by the owner through
    the ``live-probe`` CLI: the account and environment it is bound to, the contract,
    the allowed instruments, N, the notional per entry, L, ``entries_until`` and
    ``flat_by``, and the fields that stop entries (revoked, suspended, a costs
    incident, L reached).
  * ``live_probe_authorization_events`` -- the append-only history of every issue,
    revoke, suspend, resume, costs incident and L reached.
  * ``live_probe_reservations`` -- the chain ``probe -> idempotency_key -> attempt ->
    clientOrderId``, written before any exchange call. Unique on
    ``(probe_id, idempotency_key)``, so a repeated delivery finds its reservation and
    sends nothing.
  * ``live_probe_refusals`` -- every refused entry and why.

The database, not process memory, enforces:

  * no row of these tables is ever deleted, and events and refusals are never changed;
  * an issued record's terms never change, and a revoke or L reached is never undone;
  * a reservation only moves ``reserved -> sending | refused_before_send`` and
    ``sending -> placed | released``, and its chain never changes once written;
  * a reservation is written, and moves to ``sending``, only while its record admits an
    entry, read under a share lock of the record and on the clock of that statement;
  * its attempt is the entry attempt with its own ``clientOrderId``, exchange,
    instrument and side, its trade is the one journalled from that attempt, and an
    attempt or a trade counts for one reservation only;
  * no downgrade while any record or refusal exists.

Nothing reserves yet: the entry gate in ``place_order`` lands in PR 2.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENTS = (
    "issued",
    "revoked",
    "suspended",
    "resumed",
    "costs_incident",
    "costs_incident_cleared",
    "l_reached",
)
_STATES = ("reserved", "sending", "refused_before_send", "placed", "released")


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "live_probe_authorizations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("probe_id", sa.String(length=64), nullable=False),
        sa.Column("environment", sa.String(length=8), nullable=False),
        sa.Column("account_key", sa.String(length=64), nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("strategy", sa.String(length=64), nullable=False),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
        sa.Column("contract_sha256", sa.String(length=64), nullable=False),
        sa.Column("instruments", ARRAY(sa.String(length=64)), nullable=False),
        sa.Column("max_entries", sa.Integer(), nullable=False),
        sa.Column("notional_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("loss_threshold_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("entries_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("flat_by", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("issued_by", sa.String(length=64), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(length=64), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspended_reason", sa.Text(), nullable=True),
        sa.Column("costs_incident_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("costs_incident_reason", sa.Text(), nullable=True),
        sa.Column("l_reached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("l_reached_loss_usd", sa.Numeric(28, 12), nullable=True),
        sa.Column("l_reached_inputs", JSONB(), nullable=True),
        _updated_at(),
        sa.CheckConstraint(
            "probe_id ~ '^[a-z0-9][a-z0-9-]{2,63}$'", name="ck_live_probe_auth_probe_id"
        ),
        sa.CheckConstraint(
            "environment IN ('testnet', 'mainnet')", name="ck_live_probe_auth_environment"
        ),
        sa.CheckConstraint("exchange = 'bybit'", name="ck_live_probe_auth_exchange"),
        sa.CheckConstraint(
            "contract_sha256 ~ '^[0-9a-f]{64}$'", name="ck_live_probe_auth_contract_sha256"
        ),
        sa.CheckConstraint("cardinality(instruments) >= 1", name="ck_live_probe_auth_instruments"),
        sa.CheckConstraint("max_entries > 0", name="ck_live_probe_auth_max_entries"),
        # No size above USD 50 (design, "Not in this draft").
        sa.CheckConstraint(
            "notional_usd > 0 AND notional_usd <= 50", name="ck_live_probe_auth_notional"
        ),
        sa.CheckConstraint("loss_threshold_usd > 0", name="ck_live_probe_auth_loss_threshold"),
        sa.CheckConstraint("flat_by >= entries_until", name="ck_live_probe_auth_flat_by"),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by IS NULL)", name="ck_live_probe_auth_revoked"
        ),
        sa.CheckConstraint(
            "(suspended_at IS NULL) = (suspended_reason IS NULL)",
            name="ck_live_probe_auth_suspended",
        ),
        sa.CheckConstraint(
            "(costs_incident_at IS NULL) = (costs_incident_reason IS NULL)",
            name="ck_live_probe_auth_costs_incident",
        ),
        sa.CheckConstraint(
            "(l_reached_at IS NULL) = (l_reached_loss_usd IS NULL)"
            " AND (l_reached_at IS NULL) = (l_reached_inputs IS NULL)",
            name="ck_live_probe_auth_l_reached",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("probe_id", name="uq_live_probe_auth_probe_id"),
        schema="app",
    )
    op.create_table(
        "live_probe_authorization_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("probe_id", sa.String(length=64), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("detail", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "event IN (" + ", ".join(f"'{e}'" for e in _EVENTS) + ")",
            name="ck_live_probe_events_event",
        ),
        sa.ForeignKeyConstraint(["probe_id"], ["app.live_probe_authorizations.probe_id"]),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_index(
        "ix_live_probe_events_probe",
        "live_probe_authorization_events",
        ["probe_id", "at"],
        schema="app",
    )
    op.create_table(
        "live_probe_reservations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("probe_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("client_order_id", sa.String(length=32), nullable=False),
        sa.Column("native_market_id", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("notional_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("reserve_usd", sa.Numeric(28, 12), nullable=False),
        sa.Column("reserve_inputs", JSONB(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("state_reason", sa.Text(), nullable=True),
        sa.Column("attempt_id", sa.BigInteger(), nullable=True),
        sa.Column("trade_id", sa.BigInteger(), nullable=True),
        _created_at(),
        sa.Column("sending_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        _updated_at(),
        sa.CheckConstraint(
            "client_order_id ~ '^[0-9a-f]{32}$'", name="ck_live_probe_reservations_cid"
        ),
        sa.CheckConstraint("side IN ('long', 'short')", name="ck_live_probe_reservations_side"),
        sa.CheckConstraint("notional_usd > 0", name="ck_live_probe_reservations_notional"),
        sa.CheckConstraint("reserve_usd >= 0", name="ck_live_probe_reservations_reserve"),
        sa.CheckConstraint(
            "state IN (" + ", ".join(f"'{s}'" for s in _STATES) + ")",
            name="ck_live_probe_reservations_state",
        ),
        sa.CheckConstraint(
            "state NOT IN ('sending', 'placed', 'released') OR sending_at IS NOT NULL",
            name="ck_live_probe_reservations_sending_at",
        ),
        sa.CheckConstraint(
            "state NOT IN ('refused_before_send', 'placed', 'released') OR finished_at IS NOT NULL",
            name="ck_live_probe_reservations_finished_at",
        ),
        sa.CheckConstraint(
            "state <> 'placed' OR attempt_id IS NOT NULL",
            name="ck_live_probe_reservations_placed_attempt",
        ),
        sa.ForeignKeyConstraint(["probe_id"], ["app.live_probe_authorizations.probe_id"]),
        sa.ForeignKeyConstraint(["attempt_id"], ["app.live_order_attempts.id"]),
        sa.ForeignKeyConstraint(["trade_id"], ["app.trades.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "probe_id", "idempotency_key", name="uq_live_probe_reservations_idempotency"
        ),
        sa.UniqueConstraint("client_order_id", name="uq_live_probe_reservations_cid"),
        # One attempt and one trade count for one reservation only.
        sa.UniqueConstraint("attempt_id", name="uq_live_probe_reservations_attempt"),
        sa.UniqueConstraint("trade_id", name="uq_live_probe_reservations_trade"),
        schema="app",
    )
    op.create_index(
        "ix_live_probe_reservations_probe_state",
        "live_probe_reservations",
        ["probe_id", "state"],
        schema="app",
    )
    op.create_table(
        "live_probe_refusals",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        # No foreign key: an entry refused for a missing record names a probe id that
        # has no row.
        sa.Column("probe_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("decision_id", sa.String(length=128), nullable=True),
        sa.Column("reservation_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.String(length=48), nullable=False),
        sa.Column("detail", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column(
            "refused_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_index(
        "ix_live_probe_refusals_probe",
        "live_probe_refusals",
        ["probe_id", "refused_at"],
        schema="app",
    )

    op.execute(
        """
        CREATE FUNCTION app.live_probe_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only: % refused', TG_TABLE_NAME, TG_OP;
        END
        $$
        """
    )
    for table in ("live_probe_authorization_events", "live_probe_refusals"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_append_only
            BEFORE UPDATE OR DELETE ON app.{table}
            FOR EACH ROW EXECUTE FUNCTION app.live_probe_append_only()
            """
        )

    op.execute(
        """
        CREATE FUNCTION app.live_probe_authorizations_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'live_probe_authorizations: a record is never deleted';
            END IF;
            IF (NEW.probe_id, NEW.environment, NEW.account_key, NEW.exchange, NEW.strategy,
                NEW.contract_version, NEW.contract_sha256, NEW.instruments, NEW.max_entries,
                NEW.notional_usd, NEW.loss_threshold_usd, NEW.entries_until, NEW.flat_by,
                NEW.issued_at, NEW.issued_by)
               IS DISTINCT FROM
               (OLD.probe_id, OLD.environment, OLD.account_key, OLD.exchange, OLD.strategy,
                OLD.contract_version, OLD.contract_sha256, OLD.instruments, OLD.max_entries,
                OLD.notional_usd, OLD.loss_threshold_usd, OLD.entries_until, OLD.flat_by,
                OLD.issued_at, OLD.issued_by) THEN
                RAISE EXCEPTION 'live_probe_authorizations: the terms of probe % never change',
                    OLD.probe_id;
            END IF;
            IF OLD.revoked_at IS NOT NULL
               AND (NEW.revoked_at, NEW.revoked_by, NEW.revoke_reason)
                   IS DISTINCT FROM (OLD.revoked_at, OLD.revoked_by, OLD.revoke_reason) THEN
                RAISE EXCEPTION 'live_probe_authorizations: probe % stays revoked', OLD.probe_id;
            END IF;
            IF OLD.l_reached_at IS NOT NULL
               AND (NEW.l_reached_at, NEW.l_reached_loss_usd, NEW.l_reached_inputs)
                   IS DISTINCT FROM (OLD.l_reached_at, OLD.l_reached_loss_usd, OLD.l_reached_inputs)
            THEN
                RAISE EXCEPTION 'live_probe_authorizations: L reached on probe % stays',
                    OLD.probe_id;
            END IF;
            NEW.updated_at := now();
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER live_probe_authorizations_guard
        BEFORE UPDATE OR DELETE ON app.live_probe_authorizations
        FOR EACH ROW EXECUTE FUNCTION app.live_probe_authorizations_guard()
        """
    )

    op.execute(
        """
        CREATE FUNCTION app.live_probe_reservations_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            rec app.live_probe_authorizations%ROWTYPE;
            attempt app.live_order_attempts%ROWTYPE;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'live_probe_reservations: a reservation is never deleted';
            END IF;
            -- A share lock, so a revoke (or any change of the record) in flight is
            -- waited for and its committed row is read: a stale permission is never
            -- used. live_probe.py already holds the record's row lock here, record
            -- before reservation. SQL around it that changes a reservation first
            -- takes the opposite order, and a deadlock aborts it: no entry.
            SELECT * INTO rec FROM app.live_probe_authorizations
            WHERE probe_id = NEW.probe_id FOR SHARE;
            -- A reservation is written, and moves to sending, only while its record
            -- admits an entry, on the clock of this statement.
            IF TG_OP = 'INSERT' OR (NEW.state = 'sending' AND OLD.state <> 'sending') THEN
                IF rec.revoked_at IS NOT NULL OR rec.suspended_at IS NOT NULL
                   OR rec.l_reached_at IS NOT NULL OR rec.costs_incident_at IS NOT NULL
                   OR clock_timestamp() >= rec.entries_until
                   OR NOT (NEW.native_market_id = ANY (rec.instruments)) THEN
                    RAISE EXCEPTION 'live_probe_reservations: probe % admits no entry now',
                        NEW.probe_id;
                END IF;
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.state <> 'reserved' OR NEW.attempt_id IS NOT NULL
                   OR NEW.trade_id IS NOT NULL THEN
                    RAISE EXCEPTION 'live_probe_reservations: a reservation starts reserved';
                END IF;
                RETURN NEW;
            END IF;
            IF (NEW.probe_id, NEW.idempotency_key, NEW.decision_id, NEW.client_order_id,
                NEW.native_market_id, NEW.side, NEW.notional_usd, NEW.reserve_usd,
                NEW.reserve_inputs, NEW.created_at)
               IS DISTINCT FROM
               (OLD.probe_id, OLD.idempotency_key, OLD.decision_id, OLD.client_order_id,
                OLD.native_market_id, OLD.side, OLD.notional_usd, OLD.reserve_usd,
                OLD.reserve_inputs, OLD.created_at) THEN
                RAISE EXCEPTION 'live_probe_reservations: the chain of reservation % never changes',
                    OLD.id;
            END IF;
            IF OLD.attempt_id IS NOT NULL AND NEW.attempt_id IS DISTINCT FROM OLD.attempt_id THEN
                RAISE EXCEPTION 'live_probe_reservations: attempt of reservation % never changes',
                    OLD.id;
            END IF;
            IF OLD.trade_id IS NOT NULL AND NEW.trade_id IS DISTINCT FROM OLD.trade_id THEN
                RAISE EXCEPTION 'live_probe_reservations: trade of reservation % never changes',
                    OLD.id;
            END IF;
            IF NEW.attempt_id IS NOT NULL AND OLD.attempt_id IS NULL THEN
                SELECT * INTO attempt FROM app.live_order_attempts WHERE id = NEW.attempt_id;
                IF attempt.id IS NULL
                   OR attempt.operation IS DISTINCT FROM 'entry'
                   OR attempt.client_order_id IS DISTINCT FROM NEW.client_order_id
                   OR attempt.exchange IS DISTINCT FROM rec.exchange
                   OR attempt.native_market_id IS DISTINCT FROM NEW.native_market_id
                   OR attempt.side IS DISTINCT FROM NEW.side THEN
                    RAISE EXCEPTION
                        'live_probe_reservations: attempt % is not the entry attempt of '
                        'reservation %', NEW.attempt_id, OLD.id;
                END IF;
            END IF;
            IF NEW.trade_id IS NOT NULL AND OLD.trade_id IS NULL AND NOT EXISTS (
                SELECT 1 FROM app.live_order_attempts
                WHERE id = NEW.attempt_id AND trade_id = NEW.trade_id
            ) THEN
                RAISE EXCEPTION
                    'live_probe_reservations: trade % is not journalled from the attempt of '
                    'reservation %', NEW.trade_id, OLD.id;
            END IF;
            IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
                (OLD.state = 'reserved' AND NEW.state IN ('sending', 'refused_before_send'))
                OR (OLD.state = 'sending' AND NEW.state IN ('placed', 'released'))
            ) THEN
                RAISE EXCEPTION 'live_probe_reservations: % -> % refused for reservation %',
                    OLD.state, NEW.state, OLD.id;
            END IF;
            IF NEW.state = OLD.state AND NEW.state IN ('refused_before_send', 'released')
               AND (NEW.state_reason, NEW.finished_at, NEW.attempt_id, NEW.trade_id)
                   IS DISTINCT FROM
                   (OLD.state_reason, OLD.finished_at, OLD.attempt_id, OLD.trade_id)
            THEN
                RAISE EXCEPTION 'live_probe_reservations: reservation % is final', OLD.id;
            END IF;
            NEW.updated_at := now();
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER live_probe_reservations_guard
        BEFORE INSERT OR UPDATE OR DELETE ON app.live_probe_reservations
        FOR EACH ROW EXECUTE FUNCTION app.live_probe_reservations_guard()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM app.live_probe_authorizations)
               OR EXISTS (SELECT 1 FROM app.live_probe_refusals) THEN
                RAISE EXCEPTION 'cannot downgrade 0063 with LIVE_PROBE evidence';
            END IF;
        END $$
        """
    )
    op.execute("DROP TRIGGER live_probe_reservations_guard ON app.live_probe_reservations")
    op.execute("DROP FUNCTION app.live_probe_reservations_guard()")
    op.execute("DROP TRIGGER live_probe_authorizations_guard ON app.live_probe_authorizations")
    op.execute("DROP FUNCTION app.live_probe_authorizations_guard()")
    for table in ("live_probe_refusals", "live_probe_authorization_events"):
        op.execute(f"DROP TRIGGER {table}_append_only ON app.{table}")
    op.execute("DROP FUNCTION app.live_probe_append_only()")
    op.drop_index("ix_live_probe_refusals_probe", table_name="live_probe_refusals", schema="app")
    op.drop_table("live_probe_refusals", schema="app")
    op.drop_index(
        "ix_live_probe_reservations_probe_state",
        table_name="live_probe_reservations",
        schema="app",
    )
    op.drop_table("live_probe_reservations", schema="app")
    op.drop_index(
        "ix_live_probe_events_probe", table_name="live_probe_authorization_events", schema="app"
    )
    op.drop_table("live_probe_authorization_events", schema="app")
    op.drop_table("live_probe_authorizations", schema="app")
