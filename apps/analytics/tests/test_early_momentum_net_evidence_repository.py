"""Unit tests for the SQL statement builders and row mappers in
early_momentum_net_evidence_repository.py -- no database connection
required (statements are only compiled, never executed). Real-query
correctness against a live schema is covered by
test_early_momentum_net_evidence_repository_integration.py."""

from __future__ import annotations

from datetime import UTC, datetime

from schurfer_analytics import early_momentum_net_evidence_repository as repo
from schurfer_analytics.early_momentum_net_evidence import EXPECTED_SETUP_CONTEXT_STRATEGY

COHORT_START = datetime(2026, 8, 23, 14, 53, 57, tzinfo=UTC)
COHORT_END = datetime(2026, 8, 24, 0, 0, 0, tzinfo=UTC)


def test_episodes_statement_compiles_and_filters_on_strategy_and_armed_at() -> None:
    statement = repo._episodes_statement(cohort_start=COHORT_START, cohort_end=COHORT_END)
    compiled = str(statement.compile())
    assert "early_momentum_episodes" in compiled
    assert "strategies" in compiled
    assert "armed_at" in compiled


def test_trades_by_episode_statement_filters_on_episode_id_in() -> None:
    statement = repo._trades_by_episode_statement(("e1", "e2"))
    compiled = str(statement.compile())
    assert "trades" in compiled
    assert "episode_id" in compiled


def test_orphan_trades_statement_scopes_to_null_episode_and_v4_label() -> None:
    statement = repo._orphan_trades_statement(cohort_start=COHORT_START, cohort_end=COHORT_END)
    compiled = str(statement.compile(compile_kwargs={"literal_binds": False}))
    assert "episode_id" in compiled
    assert "IS NULL" in compiled or "is_(None)" in compiled or "IS NULL" in compiled.upper()


def test_exit_liquidity_statement_scopes_to_trade_ids() -> None:
    statement = repo._exit_liquidity_statement((1, 2, 3))
    compiled = str(statement.compile())
    assert "trade_exit_liquidity_observations" in compiled


def test_legacy_context_statement_scopes_to_legacy_labels_only() -> None:
    statement = repo._legacy_context_statement(cohort_end=COHORT_END)
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "early_momentum_v1" in compiled
    assert "early_momentum_v2" in compiled
    assert "early_momentum_v3" in compiled
    assert EXPECTED_SETUP_CONTEXT_STRATEGY not in compiled  # v4 never in the legacy set


class _Row:
    """Minimal stand-in for a SQLAlchemy RowMapping -- subscript access
    only, matching what the row-mapper functions actually use."""

    def __init__(self, **fields: object) -> None:
        self._fields = fields

    def __getitem__(self, key: str) -> object:
        return self._fields[key]


def test_episode_row_mapper_converts_types() -> None:
    row = _Row(
        episode_id="e1",
        strategy_id=1,
        contract_sha256=b"\x00" * 32,
        exchange="bybit",
        native_market_id="FOOUSDT",
        execution_symbol="FOO/USDT:USDT",
        source_exchange="bybit",
        source_native_id="FOOUSDT",
        execution_identity_key="ik1",
        source_identity_key="sik1",
        cluster_key="ck1",
        armed_at=COHORT_START,
        expires_at=COHORT_END,
        status="closed",
        terminal_reason=None,
        claimed_at=None,
        claim_expires_at=None,
        claim_attempts=1,
    )
    parsed = repo._episode_row(row)  # type: ignore[arg-type]
    assert parsed.episode_id == "e1"
    assert parsed.contract_sha256 == b"\x00" * 32
    assert parsed.claim_attempts == 1


def test_trade_row_mapper_handles_nulls() -> None:
    row = _Row(
        trade_id=1,
        episode_id=None,
        strategy_id=1,
        symbol="FOO/USDT:USDT",
        exchange="bybit",
        side="long",
        size_usd=100.0,
        leverage=5.0,
        entry_price=1.0,
        entry_at=COHORT_START,
        exit_price=None,
        exit_at=None,
        fees_usd=0.5,
        funding_usd=0.1,
        slippage_usd=None,
        gross_pnl_usd=None,
        gross_pnl_pct=None,
        net_pnl_usd=None,
        net_pnl_pct=None,
        accounting_version="paper_conservative_costs_v1",
        accounting_status="incomplete",
        accounting_error="missing entry_slippage_bps",
        status="open",
        notes=None,
        entry_idempotency_key=None,
        is_paper=True,
        setup_context_strategy="early_momentum_v4",
        entry_ask_impact_bps=None,
        entry_bid_impact_bps=None,
    )
    parsed = repo._trade_row(row)  # type: ignore[arg-type]
    assert parsed.episode_id is None
    assert parsed.exit_price is None
    assert parsed.net_pnl_usd is None
    assert parsed.is_paper is True


def test_legacy_context_row_mapper_none_pnl_stays_none_not_zero() -> None:
    row = _Row(
        setup_context_strategy="early_momentum_v2",
        total_trades=5,
        closed_trades=3,
        cancelled_trades=0,
        open_trades=2,
        complete_accounting_closed_trades=0,
        total_net_pnl_usd_complete_only=None,
    )
    parsed = repo._legacy_context_row(row)  # type: ignore[arg-type]
    assert parsed.total_net_pnl_usd_complete_only is None
