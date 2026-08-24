from __future__ import annotations

from datetime import timedelta

from schurfer_analytics.exit_liquidity_calibration_report import (
    EXIT_LIQUIDITY_COHORT_START,
    ExitLiquidityFilters,
)
from schurfer_analytics.exit_liquidity_net_economics_repository import (
    map_net_economics_row,
    net_economics_statement,
)
from sqlalchemy.dialects import postgresql


def _filters() -> ExitLiquidityFilters:
    return ExitLiquidityFilters(
        since=EXIT_LIQUIDITY_COHORT_START, until=EXIT_LIQUIDITY_COHORT_START + timedelta(days=1)
    )


def test_statement_joins_strategy_and_observation_and_filters_paper_short_closed() -> None:
    statement = str(
        net_economics_statement(_filters()).compile(
            dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "JOIN app.strategies" in statement
    assert "LEFT OUTER JOIN app.trade_exit_liquidity_observations" in statement
    assert "app.trades.side = 'short'" in statement
    assert "app.trades.status = 'closed'" in statement
    assert "ask_vwap" in statement
    # Frozen strategy allowlist (colleague review, 2026-08-25): the query
    # itself must scope to pump_short v1, not just segment by strategy
    # after the fact.
    assert "app.strategies.name, app.strategies.version) IN" in statement
    assert "'pump_short'" in statement
    assert "jsonb_extract_path_text" in statement


def test_map_net_economics_row_handles_missing_observation() -> None:
    source = {
        "trade_id": 1,
        "episode_id": None,
        "strategy_name": "pump_short",
        "strategy_version": "1",
        "symbol": "COTI/USDT:USDT",
        "exchange": "binance",
        "side": "short",
        "entry_at": EXIT_LIQUIDITY_COHORT_START,
        "exit_at": EXIT_LIQUIDITY_COHORT_START + timedelta(hours=1),
        "exit_reason": "max_hold age=60min",
        "size_usd": 50,
        "leverage": 5,
        "entry_price": 1.0,
        "exit_price": 0.99,
        "gross_pnl_usd": 0.5,
        "recorded_net_pnl_usd": 0.2,
        "fees_usd": 0.05,
        "funding_usd": 0.01,
        "entry_slippage_bps": 2.0,
        "modeled_exit_bps": 5.0,
        "accounting_version": "paper_conservative_costs_v1",
        "accounting_status": "complete",
        "accounting_error": None,
        "observation_id": None,
        "observed_at": None,
        "observation_exchange": None,
        "observation_symbol": None,
        "observation_status": None,
        "requested_notional_usd": None,
        "filled_notional_usd": None,
        "observed_mid": None,
        "observed_spread_bps": None,
        "observed_exit_bps": None,
        "observed_ask_vwap": None,
        "latency_ms": None,
        "error": None,
    }
    row = map_net_economics_row(source)
    assert row.trade_id == 1
    assert row.observation_id is None
    assert row.entry_slippage_bps == 2.0
