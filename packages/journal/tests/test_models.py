"""Tests for journal models and enums."""

from schurfer_journal.models import (
    Alert,
    AlertStatus,
    Exchange,
    MarketType,
    OutcomeLabel,
    OutcomeQuality,
    PumpAlertDelivery,
    PumpEvent,
    PumpEventSource,
    Side,
    Strategy,
    Trade,
    TradeDecision,
    TradeDecisionOutcome,
    TradeStatus,
)


class TestEnums:
    def test_exchange_values(self) -> None:
        assert Exchange.BYBIT == "bybit"
        assert Exchange.OKX == "okx"
        assert Exchange.HYPERLIQUID == "hyperliquid"

    def test_side_values(self) -> None:
        assert Side.LONG == "long"
        assert Side.SHORT == "short"

    def test_trade_status_values(self) -> None:
        assert TradeStatus.OPEN == "open"
        assert TradeStatus.CLOSED == "closed"
        assert TradeStatus.CANCELLED == "cancelled"

    def test_outcome_label_values(self) -> None:
        assert OutcomeLabel.WIN == "win"
        assert OutcomeLabel.LOSS == "loss"
        assert OutcomeLabel.BREAKEVEN == "breakeven"

    def test_outcome_quality_values(self) -> None:
        assert OutcomeQuality.PLANNED == "planned"
        assert OutcomeQuality.LUCKY == "lucky"
        assert OutcomeQuality.MISTAKE == "mistake"
        assert OutcomeQuality.FORCE_MAJEURE == "force_majeure"

    def test_alert_status_values(self) -> None:
        assert AlertStatus.PENDING == "pending"
        assert AlertStatus.APPROVED == "approved"
        assert AlertStatus.SKIPPED == "skipped"
        assert AlertStatus.EXPIRED == "expired"

    def test_market_type_values(self) -> None:
        assert MarketType.SPOT == "spot"
        assert MarketType.PERP == "perp"
        assert MarketType.FUTURES == "futures"

    def test_enum_is_str(self) -> None:
        # Enums inherit from str, so == comparison works without .value
        assert Exchange.BYBIT == "bybit"
        assert Side.SHORT == "short"
        # .value also works explicitly
        assert Exchange.BYBIT.value == "bybit"


class TestStrategyModel:
    def test_table_name(self) -> None:
        assert Strategy.__tablename__ == "strategies"

    def test_schema(self) -> None:
        assert Strategy.__table__.schema == "app"

    def test_required_columns(self) -> None:
        columns = {c.name for c in Strategy.__table__.columns}
        assert "id" in columns
        assert "name" in columns
        assert "version" in columns
        assert "created_at" in columns
        assert "updated_at" in columns


class TestAlertModel:
    def test_table_name(self) -> None:
        assert Alert.__tablename__ == "alerts"

    def test_schema(self) -> None:
        assert Alert.__table__.schema == "app"

    def test_required_columns(self) -> None:
        columns = {c.name for c in Alert.__table__.columns}
        assert "id" in columns
        assert "strategy_id" in columns
        assert "symbol" in columns
        assert "exchange" in columns
        assert "side" in columns
        assert "setup_context" in columns
        assert "status" in columns

    def test_foreign_key_to_strategy(self) -> None:
        fks = {fk.target_fullname for fk in Alert.__table__.foreign_keys}
        assert "app.strategies.id" in fks


class TestTradeModel:
    def test_table_name(self) -> None:
        assert Trade.__tablename__ == "trades"

    def test_schema(self) -> None:
        assert Trade.__table__.schema == "app"

    def test_required_columns(self) -> None:
        columns = {c.name for c in Trade.__table__.columns}
        required = {
            "id",
            "strategy_id",
            "symbol",
            "exchange",
            "market_type",
            "side",
            "size_usd",
            "leverage",
            "entry_price",
            "entry_at",
            "fees_usd",
            "funding_usd",
            "status",
            "setup_context",
            "created_at",
            "updated_at",
        }
        assert required.issubset(columns)

    def test_nullable_exit_fields(self) -> None:
        columns = {c.name: c for c in Trade.__table__.columns}
        assert columns["exit_price"].nullable is True
        assert columns["exit_at"].nullable is True
        assert columns["pnl_usd"].nullable is True
        assert columns["pnl_pct"].nullable is True

    def test_foreign_keys(self) -> None:
        fks = {fk.target_fullname for fk in Trade.__table__.foreign_keys}
        assert "app.strategies.id" in fks
        assert "app.alerts.id" in fks

    def test_gin_index_on_setup_context(self) -> None:
        indexes = {idx.name: idx for idx in Trade.__table__.indexes}
        assert "ix_trades_setup_context" in indexes
        gin_index = indexes["ix_trades_setup_context"]
        assert gin_index.dialect_kwargs.get("postgresql_using") == "gin"

    def test_numeric_precision(self) -> None:
        columns = {c.name: c for c in Trade.__table__.columns}
        assert columns["size_usd"].type.precision == 18
        assert columns["size_usd"].type.scale == 4
        assert columns["entry_price"].type.precision == 18
        assert columns["entry_price"].type.scale == 8


class TestTradeDecisionModels:
    def test_decision_table(self) -> None:
        assert TradeDecision.__tablename__ == "trade_decisions"
        assert TradeDecision.__table__.schema == "app"
        assert "decision_id" in TradeDecision.__table__.columns
        assert "features" in TradeDecision.__table__.columns
        assert "liquidity" in TradeDecision.__table__.columns
        assert "price" in TradeDecision.__table__.columns
        assert "pump_event_id" in TradeDecision.__table__.columns

    def test_decision_pump_event_foreign_key(self) -> None:
        pump_event_id = TradeDecision.__table__.columns["pump_event_id"]
        assert pump_event_id.nullable is True
        assert {fk.target_fullname for fk in pump_event_id.foreign_keys} == {"app.pump_events.id"}

    def test_outcome_table_and_key(self) -> None:
        assert TradeDecisionOutcome.__tablename__ == "trade_decision_outcomes"
        assert TradeDecisionOutcome.__table__.schema == "app"
        columns = TradeDecisionOutcome.__table__.columns
        assert columns["decision_id"].nullable is False
        assert columns["horizon_minutes"].nullable is False
        assert columns["resolver_version"].nullable is False
        assert columns["entry_price"].nullable is True

        constraints = {c.name: c for c in TradeDecisionOutcome.__table__.constraints}
        assert "uq_trade_decision_outcomes_decision_horizon_version" in constraints

    def test_outcome_foreign_key(self) -> None:
        fks = {fk.target_fullname for fk in TradeDecisionOutcome.__table__.foreign_keys}
        assert "app.trade_decisions.decision_id" in fks


class TestPumpEventModel:
    def test_measurement_and_entry_times_are_distinct(self) -> None:
        columns = PumpEvent.__table__.columns

        assert columns["first_seen_at"].nullable is False
        assert columns["entry_qualified_at"].nullable is True


class TestPumpEventSourceModel:
    def test_table_shape(self) -> None:
        assert PumpEventSource.__tablename__ == "pump_event_sources"
        assert PumpEventSource.__table__.schema == "app"
        columns = PumpEventSource.__table__.columns
        assert columns["event_id"].nullable is False
        assert columns["exchange"].nullable is False
        assert columns["identity_key"].nullable is True
        assert columns["market_id"].nullable is True
        assert columns["onboarded_at"].nullable is True
        assert columns["first_ticker_at"].nullable is True
        assert columns["identity_conflict"].nullable is False
        assert columns["first_seen_at"].nullable is False
        assert columns["first_change_pct"].nullable is False
        assert columns["observation_count"].nullable is False

    def test_event_foreign_key_and_unique_venue(self) -> None:
        fks = {fk.target_fullname for fk in PumpEventSource.__table__.foreign_keys}
        constraints = {constraint.name for constraint in PumpEventSource.__table__.constraints}

        assert fks == {"app.pump_events.id"}
        assert "uq_pump_event_source_venue" in constraints


class TestPumpAlertDeliveryModel:
    def test_table_shape(self) -> None:
        assert PumpAlertDelivery.__tablename__ == "pump_alert_deliveries"
        assert PumpAlertDelivery.__table__.schema == "app"
        columns = PumpAlertDelivery.__table__.columns
        assert columns["event_id"].nullable is False
        assert columns["threshold_pct"].nullable is False
        assert columns["scanner_observed_at"].nullable is False
        assert columns["notification_sent_at"].nullable is False

    def test_event_foreign_key_and_delivery_identity(self) -> None:
        fks = {fk.target_fullname for fk in PumpAlertDelivery.__table__.foreign_keys}
        constraints = {constraint.name for constraint in PumpAlertDelivery.__table__.constraints}

        assert fks == {"app.pump_events.id"}
        assert "uq_pump_alert_delivery_event_channel_kind_threshold" in constraints
