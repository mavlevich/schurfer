"""Tests for journal models and enums."""

from schurfer_journal.models import (
    Alert,
    AlertStatus,
    EarlyMomentumEpisode,
    Exchange,
    FillResolutionIncident,
    MarketType,
    NotificationDelivery,
    OutcomeLabel,
    OutcomeQuality,
    PumpAlertDelivery,
    PumpDerivativesContextRun,
    PumpDerivativesContextSample,
    PumpEvent,
    PumpEventSource,
    ResearchReportRun,
    Side,
    SourceLeadCapture,
    SourceLeadQualification,
    SourceLeadTargetObservation,
    Strategy,
    Trade,
    TradeDecision,
    TradeDecisionOutcome,
    TradeExitLiquidityObservation,
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
            "slippage_usd",
            "gross_pnl_usd",
            "gross_pnl_pct",
            "net_pnl_usd",
            "net_pnl_pct",
            "accounting_version",
            "accounting_status",
            "accounting_error",
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
        assert "app.early_momentum_episodes.episode_id" in fks

    def test_episode_id_is_nullable_and_not_unique(self) -> None:
        # Backward compat for rows that predate the episode lifecycle, and a
        # future scale-in leg reusing the same episode_id for a second row.
        columns = {c.name: c for c in Trade.__table__.columns}
        assert columns["episode_id"].nullable is True
        indexes = {idx.name: idx for idx in Trade.__table__.indexes}
        assert indexes["ix_trades_episode_id"].unique is not True

    def test_entry_idempotency_key_has_partial_unique_index(self) -> None:
        indexes = {idx.name: idx for idx in Trade.__table__.indexes}
        idx = indexes["ux_trades_entry_idempotency_key"]
        assert idx.unique is True
        assert "entry_idempotency_key IS NOT NULL" in str(
            idx.dialect_kwargs.get("postgresql_where")
        )

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


class TestTradeExitLiquidityObservationModel:
    def test_table_contract(self) -> None:
        assert TradeExitLiquidityObservation.__tablename__ == ("trade_exit_liquidity_observations")
        assert TradeExitLiquidityObservation.__table__.schema == "app"
        columns = {
            column.name: column for column in TradeExitLiquidityObservation.__table__.columns
        }
        assert {
            "trade_id",
            "observed_at",
            "exchange",
            "symbol",
            "market_id",
            "status",
            "requested_notional_usd",
            "filled_notional_usd",
            "best_bid",
            "best_ask",
            "mid",
            "spread_bps",
            "bid_vwap",
            "bid_impact_bps",
            "ask_vwap",
            "ask_impact_bps",
            "contract_size",
            "latency_ms",
            "error",
        }.issubset(columns)
        assert columns["best_ask"].type.precision == 30
        assert columns["best_ask"].type.scale == 14

    def test_trade_id_is_unique_and_references_trade(self) -> None:
        indexes = {index.name: index for index in TradeExitLiquidityObservation.__table__.indexes}
        assert indexes["ux_trade_exit_liquidity_observations_trade_id"].unique
        foreign_keys = {
            foreign_key.target_fullname
            for foreign_key in TradeExitLiquidityObservation.__table__.foreign_keys
        }
        assert foreign_keys == {"app.trades.id"}


class TestEarlyMomentumEpisodeModel:
    def test_table_contract(self) -> None:
        assert EarlyMomentumEpisode.__tablename__ == "early_momentum_episodes"
        assert EarlyMomentumEpisode.__table__.schema == "app"
        columns = {c.name: c for c in EarlyMomentumEpisode.__table__.columns}
        assert {
            "episode_id",
            "strategy_id",
            "contract_sha256",
            "source_exchange",
            "source_native_id",
            "exchange",
            "native_market_id",
            "execution_symbol",
            "execution_identity_key",
            "source_identity_key",
            "cluster_key",
            "ceiling",
            "features",
            "armed_at",
            "expires_at",
            "status",
            "terminal_reason",
            "claim_token",
            "claimed_at",
            "claim_expires_at",
            "claim_attempts",
        }.issubset(columns)

    def test_foreign_key_to_strategy_registry(self) -> None:
        # No duplicate strategy/strategy_version string columns -- identity
        # is only ever the FK to app.strategies (name, version).
        fks = {fk.target_fullname for fk in EarlyMomentumEpisode.__table__.foreign_keys}
        assert fks == {"app.strategies.id"}
        columns = {c.name for c in EarlyMomentumEpisode.__table__.columns}
        assert "strategy" not in columns
        assert "strategy_version" not in columns

    def test_no_trade_id_column(self) -> None:
        # Deliberately no episodes.trade_id -- trades.episode_id is the only
        # FK direction (avoids a circular FK and supports multi-leg episodes).
        columns = {c.name for c in EarlyMomentumEpisode.__table__.columns}
        assert "trade_id" not in columns

    def test_live_instrument_partial_unique_index(self) -> None:
        indexes = {idx.name: idx for idx in EarlyMomentumEpisode.__table__.indexes}
        idx = indexes["ux_early_momentum_episodes_live_instrument"]
        assert idx.unique is True
        assert set(idx.columns.keys()) == {"exchange", "native_market_id"}
        assert "status IN ('armed', 'claimed')" in str(idx.dialect_kwargs.get("postgresql_where"))

    def test_armed_and_claim_expiry_partial_indexes(self) -> None:
        indexes = {idx.name: idx for idx in EarlyMomentumEpisode.__table__.indexes}
        armed = indexes["ix_early_momentum_episodes_armed_expiry"]
        assert "status = 'armed'" in str(armed.dialect_kwargs.get("postgresql_where"))
        claimed = indexes["ix_early_momentum_episodes_claim_expiry"]
        assert "status = 'claimed'" in str(claimed.dialect_kwargs.get("postgresql_where"))


class TestSourceLeadCaptureModels:
    def test_capture_has_forward_timestamp_and_denominator_contract(self) -> None:
        assert SourceLeadCapture.__tablename__ == "source_lead_captures"
        columns = SourceLeadCapture.__table__.columns
        assert {
            "event_id",
            "capture_version",
            "source_occurred_at",
            "source_published_at",
            "source_first_observed_at",
            "collector_started_at",
            "capture_started_at",
            "status",
            "eligibility_reason",
            "first_sources",
            "error",
        }.issubset({column.name for column in columns})
        indexes = {index.name: index for index in SourceLeadCapture.__table__.indexes}
        assert indexes["ux_source_lead_captures_event_version"].unique
        constraints = {constraint.name for constraint in SourceLeadCapture.__table__.constraints}
        assert "ck_source_lead_captures_completion" in constraints

    def test_target_keeps_identity_and_quote_quality_explicit(self) -> None:
        assert SourceLeadTargetObservation.__tablename__ == "source_lead_target_observations"
        columns = SourceLeadTargetObservation.__table__.columns
        assert {
            "capture_id",
            "target_exchange",
            "status",
            "eligibility_reason",
            "identity_match_method",
            "identity_verified",
            "observed_at",
            "occurred_at",
            "published_at",
            "requested_notional_usd",
            "instrument",
            "ticker",
            "liquidity",
        }.issubset({column.name for column in columns})
        indexes = {index.name: index for index in SourceLeadTargetObservation.__table__.indexes}
        assert indexes["ux_source_lead_target_capture_exchange"].unique
        constraints = {
            constraint.name for constraint in SourceLeadTargetObservation.__table__.constraints
        }
        assert "ck_source_lead_target_provisional_identity" in constraints

    def test_qualification_is_versioned_and_append_only_per_capture(self) -> None:
        assert SourceLeadQualification.__tablename__ == "source_lead_qualifications"
        columns = SourceLeadQualification.__table__.columns
        assert {
            "capture_id",
            "qualification_version",
            "identity_registry_version",
            "identity_registry_fingerprint",
            "venue_selector_version",
            "status",
            "reason",
            "canonical_asset_id",
            "selected_target_exchange",
            "selected_round_trip_impact_bps",
            "details",
        }.issubset({column.name for column in columns})
        indexes = {index.name: index for index in SourceLeadQualification.__table__.indexes}
        assert indexes["ux_source_lead_qualification_capture_version"].unique
        constraints = {
            constraint.name for constraint in SourceLeadQualification.__table__.constraints
        }
        assert "ck_source_lead_qualification_selection" in constraints
        assert "ck_source_lead_qualification_registry_fingerprint" in constraints
        assert "ck_source_lead_qualification_v1_registry_contract" in constraints


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


class TestResearchReportRunModel:
    def test_registry_is_bounded_metadata(self) -> None:
        table = ResearchReportRun.__table__

        assert table.schema == "app"
        assert "summary" in table.columns
        assert "episode_results" not in table.columns
        assert "market_paths" not in table.columns
        assert table.columns["decision_input_fingerprint"].nullable is False
        assert table.columns["market_path_fingerprint"].nullable is False
        assert {index.name for index in table.indexes} == {
            "ix_research_report_runs_contract_generated"
        }


class TestPumpEventModel:
    def test_measurement_and_entry_times_are_distinct(self) -> None:
        columns = PumpEvent.__table__.columns

        assert columns["first_seen_at"].nullable is False
        assert columns["entry_qualified_at"].nullable is True


class TestPumpDerivativesContextModels:
    def test_run_identity_and_provenance(self) -> None:
        table = PumpDerivativesContextRun.__table__

        assert table.schema == "app"
        assert table.columns["event_id"].nullable is False
        assert table.columns["capability"].nullable is False
        assert table.columns["declared_support"].nullable is False
        assert table.columns["resolver_version"].nullable is False
        assert table.columns["ccxt_version"].nullable is False
        assert {constraint.name for constraint in table.constraints} >= {
            "uq_pump_derivatives_context_run"
        }
        assert {fk.target_fullname for fk in table.foreign_keys} == {"app.pump_events.id"}

    def test_sample_is_idempotent_inside_a_run(self) -> None:
        table = PumpDerivativesContextSample.__table__

        assert table.schema == "app"
        assert table.columns["source_at"].nullable is False
        assert table.columns["payload"].nullable is False
        assert {constraint.name for constraint in table.constraints} >= {
            "uq_pump_derivatives_context_sample"
        }
        assert {fk.target_fullname for fk in table.foreign_keys} == {
            "app.pump_derivatives_context_runs.id"
        }


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


class TestNotificationDeliveryModel:
    def test_table_shape(self) -> None:
        assert NotificationDelivery.__tablename__ == "notification_deliveries"
        assert NotificationDelivery.__table__.schema == "app"
        columns = NotificationDelivery.__table__.columns
        assert columns["notification_id"].nullable is False
        assert columns["dedup_key"].nullable is False
        assert columns["payload_hash"].nullable is False
        assert columns["stream_entry_id"].nullable is False
        assert columns["last_attempted_at"].nullable is True
        assert columns["delivered_at"].nullable is True

    def test_idempotency_and_state_constraints(self) -> None:
        constraints = {constraint.name for constraint in NotificationDelivery.__table__.constraints}
        indexes = {index.name for index in NotificationDelivery.__table__.indexes}

        assert constraints >= {
            "uq_notification_deliveries_notification_id",
            "uq_notification_deliveries_producer_dedup",
            "ck_notification_deliveries_severity",
            "ck_notification_deliveries_channel",
            "ck_notification_deliveries_status",
            "ck_notification_deliveries_attempt_state",
            "ck_notification_deliveries_completion",
            "ck_notification_deliveries_payload_hash",
        }
        assert indexes >= {
            "ix_notification_deliveries_status_enqueued",
            "ix_notification_deliveries_kind_created",
        }


class TestFillResolutionIncidentModel:
    def test_table_shape(self) -> None:
        assert FillResolutionIncident.__tablename__ == "fill_resolution_incidents"
        assert FillResolutionIncident.__table__.schema == "app"
        columns = FillResolutionIncident.__table__.columns
        assert columns["exchange"].nullable is False
        assert columns["base"].nullable is False
        assert columns["operation"].nullable is False
        assert columns["order_id"].nullable is False
        assert columns["trade_id"].nullable is True
        assert columns["status"].nullable is False
        assert columns["resolved_price"].nullable is True
        assert columns["context"].nullable is False

    def test_idempotency_key_and_constraints(self) -> None:
        indexes = {index.name: index for index in FillResolutionIncident.__table__.indexes}
        constraints = {
            constraint.name for constraint in FillResolutionIncident.__table__.constraints
        }
        fks = {fk.target_fullname for fk in FillResolutionIncident.__table__.foreign_keys}

        assert indexes["ux_fill_resolution_incidents_exchange_order"].unique
        assert "ck_fill_resolution_incidents_operation" in constraints
        assert "ck_fill_resolution_incidents_status" in constraints
        assert "ck_fill_resolution_incidents_resolution" in constraints
        assert fks == {"app.trades.id"}
