"""Unit tests for the pure funnel/integrity/economics/robustness/verdict
logic in early_momentum_net_evidence.py -- no database involved."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from schurfer_analytics import early_momentum_net_evidence as m

CS = m.FORMAL_COHORT_START
EXPECTED_HASH = bytes.fromhex(m.EXPECTED_CONTRACT_SHA256_HEX)
OTHER_HASH = bytes.fromhex("00" * 32)


def _episode(
    *,
    episode_id: str = "11111111-1111-1111-1111-111111111111",
    strategy_id: int = 1,
    contract_sha256: bytes = EXPECTED_HASH,
    exchange: str = "bybit",
    native_market_id: str = "FOOUSDT",
    execution_symbol: str | None = "FOO/USDT:USDT",
    source_exchange: str = "bybit",
    source_native_id: str = "FOOUSDT",
    execution_identity_key: str = "ik1",
    source_identity_key: str = "sik1",
    cluster_key: str = "ck1",
    armed_at: datetime = CS + timedelta(minutes=5),
    expires_at: datetime | None = None,
    status: str = "closed",
    terminal_reason: str | None = None,
    claimed_at: datetime | None = None,
    claim_expires_at: datetime | None = None,
    claim_attempts: int = 1,
) -> m.EpisodeRow:
    armed = armed_at
    return m.EpisodeRow(
        episode_id=episode_id,
        strategy_id=strategy_id,
        contract_sha256=contract_sha256,
        exchange=exchange,
        native_market_id=native_market_id,
        execution_symbol=execution_symbol,
        source_exchange=source_exchange,
        source_native_id=source_native_id,
        execution_identity_key=execution_identity_key,
        source_identity_key=source_identity_key,
        cluster_key=cluster_key,
        armed_at=armed,
        expires_at=expires_at or (armed + timedelta(hours=1)),
        status=status,
        terminal_reason=terminal_reason,
        claimed_at=claimed_at if claimed_at is not None else armed + timedelta(seconds=1),
        claim_expires_at=(
            claim_expires_at if claim_expires_at is not None else armed + timedelta(seconds=31)
        ),
        claim_attempts=claim_attempts,
    )


def _trade(
    *,
    trade_id: int = 1,
    episode_id: str | None = "11111111-1111-1111-1111-111111111111",
    strategy_id: int = 1,
    symbol: str = "FOO/USDT:USDT",
    exchange: str = "bybit",
    side: str = "long",
    size_usd: float = 100.0,
    leverage: float = 5.0,
    entry_price: float = 1.0,
    entry_at: datetime = CS + timedelta(minutes=5, seconds=2),
    exit_price: float | None = 1.05,
    exit_at: datetime | None = None,
    fees_usd: float = 0.5,
    funding_usd: float = 0.1,
    slippage_usd: float | None = 0.2,
    gross_pnl_usd: float | None = 25.0,
    gross_pnl_pct: float | None = 25.0,
    net_pnl_usd: float | None = 24.2,
    net_pnl_pct: float | None = 24.2,
    accounting_version: str = m.ACCOUNTING_VERSION,
    accounting_status: str = "complete",
    accounting_error: str | None = None,
    status: str = "closed",
    notes: str | None = "take_profit move=5.0%",
    entry_idempotency_key: str | None = None,
    is_paper: bool = True,
    setup_context_strategy: str | None = "early_momentum_v4",
    entry_ask_impact_bps: float | None = 3.0,
    entry_bid_impact_bps: float | None = 2.0,
) -> m.TradeRow:
    # Defaults to the real production format (see early_momentum.py's own
    # f"{episode_id}:entry:base") derived from THIS call's episode_id, not a
    # fixed literal -- otherwise every test overriding episode_id without
    # also overriding the idempotency key would silently fail the identity
    # check added for the colleague-review finding below.
    if entry_idempotency_key is None and episode_id is not None:
        entry_idempotency_key = f"{episode_id}:entry:base"
    return m.TradeRow(
        trade_id=trade_id,
        episode_id=episode_id,
        strategy_id=strategy_id,
        symbol=symbol,
        exchange=exchange,
        side=side,
        size_usd=size_usd,
        leverage=leverage,
        entry_price=entry_price,
        entry_at=entry_at,
        exit_price=exit_price,
        exit_at=exit_at or (entry_at + timedelta(hours=1)),
        fees_usd=fees_usd,
        funding_usd=funding_usd,
        slippage_usd=slippage_usd,
        gross_pnl_usd=gross_pnl_usd,
        gross_pnl_pct=gross_pnl_pct,
        net_pnl_usd=net_pnl_usd,
        net_pnl_pct=net_pnl_pct,
        accounting_version=accounting_version,
        accounting_status=accounting_status,
        accounting_error=accounting_error,
        status=status,
        notes=notes,
        entry_idempotency_key=entry_idempotency_key,
        is_paper=is_paper,
        setup_context_strategy=setup_context_strategy,
        entry_ask_impact_bps=entry_ask_impact_bps,
        entry_bid_impact_bps=entry_bid_impact_bps,
    )


def _dataset(
    *,
    episodes: tuple[m.EpisodeRow, ...] = (),
    trades: tuple[m.TradeRow, ...] = (),
    exit_liquidity: tuple[m.ExitLiquidityRow, ...] = (),
    cohort_end: datetime = CS + timedelta(days=1),
    db_snapshot_at: datetime | None = None,
) -> m.RawDataset:
    return m.RawDataset(
        cohort_start=CS,
        cohort_end=cohort_end,
        db_snapshot_at=db_snapshot_at or (cohort_end + timedelta(hours=7)),
        episodes=episodes,
        trades=trades,
        exit_liquidity=exit_liquidity,
    )


def _clean_pair(**trade_overrides: object) -> tuple[m.EpisodeRow, m.TradeRow]:
    ep = _episode()
    tr = _trade(**trade_overrides)  # type: ignore[arg-type]
    return ep, tr


# --- exit reason parsing ---


def test_parse_exit_reason_recognizes_all_five_categories() -> None:
    assert m.parse_exit_reason("take_profit move=4.2%") == "take_profit"
    assert m.parse_exit_reason("max_hold age=238min") == "max_hold"
    assert m.parse_exit_reason("no_progress age=60min") == "no_progress"
    assert m.parse_exit_reason("initial_sl move=-2.0%") == "initial_sl"
    assert m.parse_exit_reason("trailing_stop trail=8% profit=12.3%") == "trailing_stop"


def test_parse_exit_reason_unrecognized_token_is_unknown_not_invented() -> None:
    assert m.parse_exit_reason("some_future_reason x=1") == "unknown"


def test_parse_exit_reason_none_or_empty_is_unknown() -> None:
    assert m.parse_exit_reason(None) == "unknown"
    assert m.parse_exit_reason("") == "unknown"
    assert m.parse_exit_reason("   ") == "unknown"


# --- funnel: happy path ---


def test_funnel_clean_trade_reaches_comparable_set() -> None:
    ep, tr = _clean_pair()
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    assert len(result.comparable) == 1
    assert result.cohort_violations == ()
    assert result.row_violations == ()
    assert result.steps[0].label == "all_formal_v4_episodes"
    assert result.steps[-1].label == "final_comparable_set"
    assert result.steps[-1].remaining == 1


# --- cohort anchoring on armed_at, not entry_at ---


def test_episode_armed_before_cohort_start_is_never_in_the_dataset_to_begin_with() -> None:
    """The repository itself scopes episodes by armed_at >= cohort_start --
    this test documents that the pure funnel has nothing to filter here
    because such an episode would never reach it. The real "armed before
    cutoff, entry after cutoff" exclusion is exercised at the repository
    integration-test level (see test_early_momentum_net_evidence_repository_
    integration.py)."""
    ep = _episode(armed_at=CS - timedelta(hours=1))
    tr = _trade(entry_at=CS + timedelta(minutes=5))
    # A dataset containing this episode is already a contract violation of
    # what the repository promises to fetch -- the funnel does not
    # second-guess armed_at against cohort_start again, it trusts the
    # repository's own WHERE clause. Confirms the funnel does not crash or
    # silently misclassify such a row if it somehow appeared.
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    assert len(result.comparable) == 1  # trusts the (here, synthetic) input as given


# --- cohort-level integrity: contract hash ---


def test_unexpected_contract_hash_is_a_cohort_violation() -> None:
    ep = _episode(contract_sha256=OTHER_HASH)
    tr = _trade()
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.cohort_violations}
    assert "unexpected_contract_hash" in codes
    assert result.comparable == ()


def test_multiple_contract_hashes_in_cohort_is_a_cohort_violation() -> None:
    ep1 = _episode(episode_id="e1", contract_sha256=EXPECTED_HASH)
    ep2 = _episode(episode_id="e2", contract_sha256=OTHER_HASH, native_market_id="BARUSDT")
    tr1 = _trade(trade_id=1, episode_id="e1")
    tr2 = _trade(trade_id=2, episode_id="e2")
    result = m.build_funnel(_dataset(episodes=(ep1, ep2), trades=(tr1, tr2)))
    codes = {v.code for v in result.cohort_violations}
    assert "multiple_contract_hashes_in_cohort" in codes


# --- row-level integrity ---


def test_episode_opened_without_trade_is_a_row_violation() -> None:
    ep = _episode(status="closed")
    result = m.build_funnel(_dataset(episodes=(ep,), trades=()))
    codes = {v.code for v in result.row_violations}
    assert "episode_opened_without_trade" in codes
    assert result.comparable == ()


def test_multiple_trades_per_episode_is_a_row_violation() -> None:
    ep = _episode()
    tr1 = _trade(trade_id=1, entry_idempotency_key="k1")
    tr2 = _trade(trade_id=2, entry_idempotency_key="k2")
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr1, tr2)))
    codes = {v.code for v in result.row_violations}
    assert "multiple_trades_per_episode" in codes
    assert result.comparable == ()


def test_orphan_v4_trade_without_episode_is_detected() -> None:
    tr = _trade(episode_id=None, setup_context_strategy="early_momentum_v4")
    result = m.build_funnel(_dataset(episodes=(), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "v4_trade_without_episode" in codes
    assert result.comparable == ()


def test_not_paper_trade_is_a_row_violation() -> None:
    ep, tr = _clean_pair(is_paper=False)
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "not_paper" in codes


def test_short_side_is_a_row_violation() -> None:
    ep, tr = _clean_pair(side="short")
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "unexpected_side" in codes


def test_strategy_identity_mismatch_is_a_row_violation() -> None:
    ep, tr = _clean_pair(strategy_id=999)
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "strategy_identity_mismatch" in codes


def test_incomplete_accounting_on_closed_trade_is_a_row_violation_never_a_zero() -> None:
    """A trade with incomplete accounting must never silently become a
    zero-PnL comparable row -- it must be excluded and flagged, not
    counted as a wash."""
    ep, tr = _clean_pair(
        accounting_status="incomplete",
        net_pnl_usd=None,
        net_pnl_pct=None,
        gross_pnl_usd=None,
        gross_pnl_pct=None,
        slippage_usd=None,
    )
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "incomplete_accounting_on_closed_trade" in codes
    assert result.comparable == ()


def test_pnl_present_despite_incomplete_accounting_is_flagged() -> None:
    ep, tr = _clean_pair(accounting_status="incomplete", net_pnl_usd=5.0)
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "pnl_present_despite_incomplete_accounting" in codes


def test_missing_required_accounting_field_is_a_row_violation() -> None:
    ep, tr = _clean_pair(funding_usd=0.0, slippage_usd=None)
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "missing_required_accounting_field" in codes


def test_accounting_version_mismatch_is_a_row_violation() -> None:
    ep, tr = _clean_pair(accounting_version="legacy_price_only_v1")
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "accounting_version_mismatch" in codes


# --- route/strategy identity consistency (colleague review) ---


def test_wrong_trade_exchange_is_a_route_identity_violation() -> None:
    ep, tr = _clean_pair(exchange="binance")  # episode default exchange is "bybit"
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "route_or_strategy_identity_mismatch" in codes
    assert result.comparable == ()


def test_wrong_trade_symbol_is_a_route_identity_violation() -> None:
    ep, tr = _clean_pair(symbol="BAR/USDT:USDT")  # episode's execution_symbol is "FOO/USDT:USDT"
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "route_or_strategy_identity_mismatch" in codes


def test_missing_execution_symbol_is_a_route_identity_violation() -> None:
    ep = _episode(execution_symbol=None)
    tr = _trade()
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "route_or_strategy_identity_mismatch" in codes


def test_wrong_setup_context_strategy_label_is_a_route_identity_violation() -> None:
    ep, tr = _clean_pair(setup_context_strategy="early_momentum_v3")
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "route_or_strategy_identity_mismatch" in codes


def test_wrong_idempotency_key_is_a_route_identity_violation() -> None:
    ep, tr = _clean_pair(entry_idempotency_key="something-else")
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "route_or_strategy_identity_mismatch" in codes


def test_correct_identity_on_every_field_produces_no_violation() -> None:
    ep, tr = _clean_pair()
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "route_or_strategy_identity_mismatch" not in codes
    assert len(result.comparable) == 1


# --- accounting arithmetic reconciliation (colleague review) ---


def test_gross_pnl_usd_inconsistent_with_percent_is_flagged() -> None:
    """size=100, gross_pnl_pct=25% implies gross_pnl_usd=25.0 -- 1000.0 is
    an impossible number for this trade and must never reach economics."""
    ep, tr = _clean_pair(gross_pnl_usd=1000.0, gross_pnl_pct=25.0)
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "accounting_arithmetic_inconsistent" in codes
    assert result.comparable == ()


def test_net_pnl_usd_inconsistent_with_costs_is_flagged() -> None:
    """gross(25.0) - fees(0.5) - funding(0.1) - slippage(0.2) = 24.2, not 5.0."""
    ep, tr = _clean_pair(net_pnl_usd=5.0, net_pnl_pct=5.0)
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "accounting_arithmetic_inconsistent" in codes


def test_net_pnl_pct_inconsistent_with_net_pnl_usd_is_flagged() -> None:
    """net_pnl_usd=24.2 on size=100 implies net_pnl_pct=24.2%, not 90%."""
    ep, tr = _clean_pair(net_pnl_pct=90.0)
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "accounting_arithmetic_inconsistent" in codes


def test_non_positive_size_is_flagged() -> None:
    ep, tr = _clean_pair(size_usd=0.0)
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "accounting_arithmetic_inconsistent" in codes


def test_non_finite_accounting_value_is_flagged() -> None:
    ep, tr = _clean_pair(net_pnl_usd=float("nan"))
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "accounting_arithmetic_inconsistent" in codes


def test_reconciled_accounting_within_rounding_tolerance_is_not_flagged() -> None:
    """A cent of DB-storage rounding must not itself be a violation."""
    ep, tr = _clean_pair(net_pnl_usd=24.21, net_pnl_pct=24.21)
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "accounting_arithmetic_inconsistent" not in codes
    assert len(result.comparable) == 1


# --- open / right-censored trades: stay in funnel, not economics ---


def test_open_trade_within_maturity_horizon_is_excluded_but_not_a_violation() -> None:
    entry = CS + timedelta(minutes=5, seconds=2)
    ep = _episode(status="opened")
    tr = _trade(status="open", exit_at=None, exit_price=None, entry_at=entry)
    snapshot = entry + timedelta(hours=1)  # well within the 6h maturity buffer
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,), db_snapshot_at=snapshot))
    assert result.comparable == ()
    assert result.row_violations == ()  # not a violation -- still legitimately running


def test_open_trade_past_maturity_horizon_is_a_row_violation() -> None:
    entry = CS + timedelta(minutes=5, seconds=2)
    ep = _episode(status="opened")
    tr = _trade(status="open", exit_at=None, exit_price=None, entry_at=entry)
    snapshot = entry + timedelta(hours=7)  # past the 6h maturity buffer
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,), db_snapshot_at=snapshot))
    codes = {v.code for v in result.row_violations}
    assert "open_past_maturity_horizon" in codes


def test_cancelled_trade_is_excluded_without_a_violation() -> None:
    ep = _episode(status="opened")
    tr = _trade(status="cancelled", exit_at=None, exit_price=None)
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    assert result.comparable == ()
    assert result.row_violations == ()


def test_normal_rejected_episode_without_a_trade_is_not_a_violation() -> None:
    ep = _episode(status="rejected", terminal_reason="quote_timeout")
    result = m.build_funnel(_dataset(episodes=(ep,), trades=()))
    assert result.row_violations == ()
    assert result.comparable == ()


def test_episode_stuck_armed_past_maturity_is_a_row_violation() -> None:
    """colleague review: a mature cohort run can only happen once
    db_snapshot_at is already well past the whole cohort's own maturity
    buffer, so a still-armed episode at that point means the reaper never
    resolved it -- a lifecycle failure, not a silent exclusion (which
    would otherwise let an unexecuted signal quietly disappear from the
    funnel and bias the verdict toward PASS)."""
    ep = _episode(status="armed", terminal_reason=None)
    result = m.build_funnel(_dataset(episodes=(ep,), trades=()))
    assert result.comparable == ()
    codes = {v.code for v in result.row_violations}
    assert "episode_stuck_unresolved_past_maturity" in codes
    step = next(s for s in result.steps if s.label == "reached_claim_open_or_explained_terminal")
    assert step.excluded == 1


def test_episode_stuck_claimed_past_maturity_is_a_row_violation() -> None:
    ep = _episode(status="claimed", terminal_reason=None)
    result = m.build_funnel(_dataset(episodes=(ep,), trades=()))
    codes = {v.code for v in result.row_violations}
    assert "episode_stuck_unresolved_past_maturity" in codes


def test_episode_still_armed_within_maturity_horizon_is_excluded_without_a_violation() -> None:
    """A right-censored case: the episode simply hasn't had time to
    resolve yet -- not a lifecycle failure."""
    armed = CS + timedelta(minutes=5)
    ep = _episode(status="armed", terminal_reason=None, armed_at=armed)
    snapshot = armed + timedelta(hours=1)  # well within COHORT_MATURITY_BUFFER_SECONDS
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(), db_snapshot_at=snapshot))
    assert result.comparable == ()
    assert result.row_violations == ()


# --- temporal sanity (colleague correction #2) ---


def test_entry_before_armed_beyond_tolerance_is_a_temporal_violation() -> None:
    armed = CS + timedelta(minutes=5)
    ep = _episode(armed_at=armed)
    tr = _trade(entry_at=armed - timedelta(seconds=10))
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "temporal_inconsistency" in codes


def test_entry_before_armed_within_tolerance_is_not_a_violation() -> None:
    armed = CS + timedelta(minutes=5)
    ep = _episode(armed_at=armed)
    tr = _trade(entry_at=armed - timedelta(seconds=3))
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    assert result.row_violations == ()
    assert len(result.comparable) == 1


def test_exit_before_entry_is_a_temporal_violation() -> None:
    entry = CS + timedelta(minutes=5, seconds=2)
    ep, tr = _clean_pair(entry_at=entry, exit_at=entry - timedelta(minutes=1))
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "temporal_inconsistency" in codes


def test_expires_at_not_after_armed_at_is_a_temporal_violation() -> None:
    armed = CS + timedelta(minutes=5)
    ep = _episode(armed_at=armed, expires_at=armed)
    tr = _trade(entry_at=armed + timedelta(seconds=1))
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "temporal_inconsistency" in codes


def test_closed_trade_with_zero_claim_attempts_is_a_temporal_violation() -> None:
    ep, tr = _clean_pair()
    ep_bad = m.EpisodeRow(**{**ep.__dict__, "claim_attempts": 0})
    result = m.build_funnel(_dataset(episodes=(ep_bad,), trades=(tr,)))
    codes = {v.code for v in result.row_violations}
    assert "temporal_inconsistency" in codes


# --- economics ---


def test_economics_win_rate_and_totals() -> None:
    ep1, tr1 = _clean_pair()
    ep2 = _episode(episode_id="e2", native_market_id="BARUSDT", cluster_key="ck2")
    # fees(0.5) + funding(0.1) + slippage(0.2) = 0.8 default cost, so
    # net = gross - 0.8 must actually reconcile (colleague review: the
    # funnel now checks this arithmetic, not just field presence).
    tr2 = _trade(
        trade_id=2,
        episode_id="e2",
        net_pnl_usd=-9.8,
        net_pnl_pct=-9.8,
        gross_pnl_usd=-9.0,
        gross_pnl_pct=-9.0,
    )
    result = m.build_funnel(_dataset(episodes=(ep1, ep2), trades=(tr1, tr2)))
    econ = m.compute_economics(result.comparable)
    assert econ.closed_trades == 2
    assert econ.wins == 1
    assert econ.losses == 1
    assert econ.win_rate_pct == 50.0
    assert econ.total_net_pnl_usd == tr1.net_pnl_usd + tr2.net_pnl_usd  # type: ignore[operator]


def test_return_on_margin_scales_with_leverage() -> None:
    # gross(10.8) - fees(0.5) - funding(0.1) - slippage(0.2) = net(10.0),
    # reconciling with the funnel's own new arithmetic check.
    ep, tr = _clean_pair(
        size_usd=100.0,
        leverage=5.0,
        gross_pnl_usd=10.8,
        gross_pnl_pct=10.8,
        net_pnl_usd=10.0,
        net_pnl_pct=10.0,
    )
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    econ = m.compute_economics(result.comparable)
    # margin = 100/5 = 20; 10 / 20 * 100 = 50% return on margin
    assert econ.net_return_on_margin.mean_pct == 50.0
    assert econ.net_return_on_notional.mean_pct == 10.0


def test_equity_curve_and_drawdown_ordered_by_exit_at_not_insertion_order() -> None:
    base = CS + timedelta(minutes=5)
    ep1 = _episode(episode_id="e1", native_market_id="A", cluster_key="c1")
    ep2 = _episode(episode_id="e2", native_market_id="B", cluster_key="c2")
    # Inserted out of chronological order on purpose. gross = net + the
    # default 0.8 total cost (fees 0.5 + funding 0.1 + slippage 0.2), so
    # the funnel's own accounting-reconciliation check still passes.
    tr_later = _trade(
        trade_id=2,
        episode_id="e2",
        entry_at=base + timedelta(minutes=1),
        exit_at=base + timedelta(hours=2),
        gross_pnl_usd=-49.2,
        gross_pnl_pct=-49.2,
        net_pnl_usd=-50.0,
        net_pnl_pct=-50.0,
    )
    tr_earlier = _trade(
        trade_id=1,
        episode_id="e1",
        entry_at=base,
        exit_at=base + timedelta(hours=1),
        gross_pnl_usd=30.8,
        gross_pnl_pct=30.8,
        net_pnl_usd=30.0,
        net_pnl_pct=30.0,
    )
    result = m.build_funnel(_dataset(episodes=(ep1, ep2), trades=(tr_later, tr_earlier)))
    econ = m.compute_economics(result.comparable)
    assert [p.trade_id for p in econ.equity_curve] == [1, 2]
    assert econ.equity_curve[0].cumulative_net_pnl_usd == 30.0
    assert econ.equity_curve[1].cumulative_net_pnl_usd == -20.0
    assert econ.max_drawdown_usd == 50.0  # peak 30 -> trough -20
    assert econ.worst_losing_streak == 1


def test_exit_reason_grouping_uses_parsed_category() -> None:
    ep, tr = _clean_pair(notes="max_hold age=240min")
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    econ = m.compute_economics(result.comparable)
    assert econ.by_exit_reason[0].key == "max_hold"


# --- concurrency ---


def test_concurrency_handles_simultaneous_exit_and_entry_without_double_counting() -> None:
    """One position's exit and another's entry at the exact same instant
    must never read as 2 concurrent positions -- the close is processed
    first."""
    t0 = CS + timedelta(minutes=5)
    ep1 = _episode(episode_id="e1", native_market_id="A", cluster_key="c1")
    ep2 = _episode(episode_id="e2", native_market_id="B", cluster_key="c2", armed_at=t0)
    tr1 = _trade(trade_id=1, episode_id="e1", entry_at=t0, exit_at=t0 + timedelta(hours=1))
    tr2 = _trade(
        trade_id=2,
        episode_id="e2",
        entry_at=t0 + timedelta(hours=1),  # exactly when tr1 exits
        exit_at=t0 + timedelta(hours=2),
    )
    result = m.build_funnel(_dataset(episodes=(ep1, ep2), trades=(tr1, tr2)))
    conc = m.compute_concurrency(result.comparable)
    assert conc.max_concurrent_positions == 1


def test_concurrency_counts_genuine_overlap() -> None:
    t0 = CS + timedelta(minutes=5)
    ep1 = _episode(episode_id="e1", native_market_id="A", cluster_key="c1")
    ep2 = _episode(episode_id="e2", native_market_id="B", cluster_key="c2", armed_at=t0)
    tr1 = _trade(trade_id=1, episode_id="e1", entry_at=t0, exit_at=t0 + timedelta(hours=2))
    tr2 = _trade(
        trade_id=2,
        episode_id="e2",
        entry_at=t0 + timedelta(minutes=30),
        exit_at=t0 + timedelta(hours=1),
    )
    result = m.build_funnel(_dataset(episodes=(ep1, ep2), trades=(tr1, tr2)))
    conc = m.compute_concurrency(result.comparable)
    assert conc.max_concurrent_positions == 2
    assert conc.max_required_margin_usd == (100.0 / 5.0) * 2


def test_entry_waves_group_by_the_fixed_gap_and_never_change() -> None:
    t0 = CS + timedelta(minutes=5)
    ep1 = _episode(episode_id="e1", native_market_id="A", cluster_key="c1")
    ep2 = _episode(episode_id="e2", native_market_id="B", cluster_key="c2", armed_at=t0)
    ep3 = _episode(
        episode_id="e3",
        native_market_id="C",
        cluster_key="c3",
        armed_at=t0 + timedelta(hours=3),
    )
    tr1 = _trade(trade_id=1, episode_id="e1", entry_at=t0)
    tr2 = _trade(
        trade_id=2, episode_id="e2", entry_at=t0 + timedelta(minutes=30)
    )  # same wave (<=60min gap)
    tr3 = _trade(
        trade_id=3, episode_id="e3", entry_at=t0 + timedelta(hours=3)
    )  # new wave (>60min gap)
    result = m.build_funnel(_dataset(episodes=(ep1, ep2, ep3), trades=(tr1, tr2, tr3)))
    conc = m.compute_concurrency(result.comparable)
    assert len(conc.waves) == 2
    assert conc.waves[0].trades == 2
    assert conc.waves[1].trades == 1


# --- robustness ---


def test_block_bootstrap_is_deterministic_across_runs() -> None:
    ep, tr = _clean_pair()
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    r1 = m.compute_robustness(result.comparable)
    r2 = m.compute_robustness(result.comparable)
    assert r1.block_bootstrap == r2.block_bootstrap


def test_leave_one_week_out_excludes_the_named_week() -> None:
    week1 = CS + timedelta(minutes=5)
    week2 = CS + timedelta(days=8)
    ep1 = _episode(episode_id="e1", native_market_id="A", cluster_key="c1", armed_at=week1)
    ep2 = _episode(episode_id="e2", native_market_id="B", cluster_key="c2", armed_at=week2)
    tr1 = _trade(
        trade_id=1,
        episode_id="e1",
        entry_at=week1,
        exit_at=week1 + timedelta(hours=1),
        gross_pnl_usd=10.8,
        gross_pnl_pct=10.8,
        net_pnl_usd=10.0,
        net_pnl_pct=10.0,
    )
    tr2 = _trade(
        trade_id=2,
        episode_id="e2",
        entry_at=week2,
        exit_at=week2 + timedelta(hours=1),
        gross_pnl_usd=30.8,
        gross_pnl_pct=30.8,
        net_pnl_usd=30.0,
        net_pnl_pct=30.0,
    )
    dataset = _dataset(episodes=(ep1, ep2), trades=(tr1, tr2), cohort_end=CS + timedelta(days=20))
    result = m.build_funnel(dataset)
    rob = m.compute_robustness(result.comparable)
    week_keys = {k for k, _ in rob.leave_one_week_out}
    assert m.utc_week_key(week1) in week_keys
    assert m.utc_week_key(week2) in week_keys
    # excluding week1's trade (10%) leaves only week2's trade (30%) as the mean
    excl_week1 = dict(rob.leave_one_week_out)[m.utc_week_key(week1)]
    assert excl_week1 == 30.0


# --- fingerprint ---


def test_fingerprint_changes_when_any_pre_funnel_row_changes() -> None:
    ep, tr = _clean_pair()
    d1 = _dataset(episodes=(ep,), trades=(tr,))
    fp1 = m.dataset_fingerprint(d1)

    # Change a field that never survives into the comparable set either --
    # a rejected trade's accounting_error -- the fingerprint must still move.
    tr_changed = _trade(accounting_error="a_new_error_message")
    d2 = _dataset(episodes=(ep,), trades=(tr_changed,))
    fp2 = m.dataset_fingerprint(d2)
    assert fp1 != fp2


def test_fingerprint_is_stable_for_identical_input() -> None:
    ep, tr = _clean_pair()
    d1 = _dataset(episodes=(ep,), trades=(tr,))
    d2 = _dataset(episodes=(ep,), trades=(tr,))
    assert m.dataset_fingerprint(d1) == m.dataset_fingerprint(d2)


@pytest.mark.parametrize(
    "trade_override",
    [
        {"entry_price": 1.23},
        {"exit_price": 1.99},
        {"size_usd": 250.0},
        {"leverage": 3.0},
        {"gross_pnl_pct": 99.0},
        {"net_pnl_pct": 1.0},
        {"entry_ask_impact_bps": 42.0},
        {"entry_bid_impact_bps": 42.0},
    ],
)
def test_fingerprint_changes_when_a_previously_omitted_trade_field_changes(
    trade_override: dict[str, object],
) -> None:
    """colleague review: prices, size/leverage, PnL percentages, and impact
    fields were all missing from the old hand-picked fingerprint payload --
    two runs with different values for any of these could report the same
    fingerprint. Generic per-dataclass-field hashing (see
    dataset_fingerprint's own docstring) closes that gap for good, not just
    for the specific fields caught here."""
    ep = _episode()
    baseline = m.dataset_fingerprint(_dataset(episodes=(ep,), trades=(_trade(),)))
    changed = m.dataset_fingerprint(
        _dataset(episodes=(ep,), trades=(_trade(**trade_override),))  # type: ignore[arg-type]
    )
    assert baseline != changed


@pytest.mark.parametrize(
    "episode_override",
    [
        {"claim_expires_at": CS + timedelta(minutes=5, seconds=99)},
        {"execution_symbol": "DIFFERENT/USDT:USDT"},
        {"source_identity_key": "a-different-source-identity-key"},
    ],
)
def test_fingerprint_changes_when_a_previously_omitted_episode_field_changes(
    episode_override: dict[str, object],
) -> None:
    tr = _trade()
    baseline = m.dataset_fingerprint(_dataset(episodes=(_episode(),), trades=(tr,)))
    changed = m.dataset_fingerprint(
        _dataset(episodes=(_episode(**episode_override),), trades=(tr,))  # type: ignore[arg-type]
    )
    assert baseline != changed


@pytest.mark.parametrize(
    "liquidity_override",
    [
        {"spread_bps": 12.5},
        {"ask_impact_bps": 7.0},
        {"bid_impact_bps": 7.0},
        {"latency_ms": 999},
        {"error": "timeout"},
    ],
)
def test_fingerprint_changes_when_an_exit_liquidity_field_changes(
    liquidity_override: dict[str, object],
) -> None:
    ep, tr = _clean_pair()
    base_row = m.ExitLiquidityRow(
        trade_id=tr.trade_id,
        requested_notional_usd=100.0,
        filled_notional_usd=100.0,
        spread_bps=1.0,
        ask_impact_bps=1.0,
        bid_impact_bps=1.0,
        latency_ms=50,
        status="ok",
        error=None,
    )
    changed_row = m.ExitLiquidityRow(**{**base_row.__dict__, **liquidity_override})
    baseline = m.dataset_fingerprint(
        _dataset(episodes=(ep,), trades=(tr,), exit_liquidity=(base_row,))
    )
    changed = m.dataset_fingerprint(
        _dataset(episodes=(ep,), trades=(tr,), exit_liquidity=(changed_row,))
    )
    assert baseline != changed


# --- verdict / sample floor boundaries ---


def _n_clean_comparables(
    n: int, *, clusters: int, weeks: int
) -> tuple[m.FunnelResult, m.EconomicsSummary, m.RobustnessSummary]:
    episodes: list[m.EpisodeRow] = []
    trades: list[m.TradeRow] = []
    for i in range(n):
        cluster = f"c{i % clusters}"
        week_offset = timedelta(days=7 * (i % weeks))
        armed = CS + timedelta(minutes=5) + week_offset
        eid = f"e{i}"
        episodes.append(
            _episode(
                episode_id=eid,
                native_market_id=f"SYM{i}",
                cluster_key=cluster,
                armed_at=armed,
            )
        )
        trades.append(
            _trade(
                trade_id=i,
                episode_id=eid,
                entry_at=armed + timedelta(seconds=2),
                exit_at=armed + timedelta(hours=1),
                entry_idempotency_key=f"{eid}:entry:base",
            )
        )
    dataset = _dataset(
        episodes=tuple(episodes), trades=tuple(trades), cohort_end=CS + timedelta(days=60)
    )
    result = m.build_funnel(dataset)
    econ = m.compute_economics(result.comparable)
    rob = m.compute_robustness(result.comparable)
    return result, econ, rob


def test_verdict_insufficient_data_at_99_closed_trades() -> None:
    result, econ, rob = _n_clean_comparables(99, clusters=30, weeks=4)
    verdict = m.evaluate_verdict(funnel=result, economics=econ, robustness=rob)
    assert verdict.verdict == m.VERDICT_INSUFFICIENT_DATA
    assert any("closed_trades" in r for r in verdict.reasons)


def test_verdict_passes_sample_floor_at_100_closed_trades_with_enough_diversity() -> None:
    result, econ, rob = _n_clean_comparables(100, clusters=30, weeks=4)
    verdict = m.evaluate_verdict(funnel=result, economics=econ, robustness=rob)
    assert verdict.verdict != m.VERDICT_INSUFFICIENT_DATA


def test_verdict_insufficient_data_at_29_clusters() -> None:
    result, econ, rob = _n_clean_comparables(100, clusters=29, weeks=4)
    verdict = m.evaluate_verdict(funnel=result, economics=econ, robustness=rob)
    assert verdict.verdict == m.VERDICT_INSUFFICIENT_DATA
    assert any("distinct_clusters" in r for r in verdict.reasons)


def test_verdict_passes_diversity_floor_at_30_clusters() -> None:
    result, econ, rob = _n_clean_comparables(100, clusters=30, weeks=4)
    verdict = m.evaluate_verdict(funnel=result, economics=econ, robustness=rob)
    assert verdict.verdict != m.VERDICT_INSUFFICIENT_DATA


def test_verdict_insufficient_data_at_3_distinct_weeks() -> None:
    result, econ, rob = _n_clean_comparables(100, clusters=30, weeks=3)
    verdict = m.evaluate_verdict(funnel=result, economics=econ, robustness=rob)
    assert verdict.verdict == m.VERDICT_INSUFFICIENT_DATA
    assert any("distinct_utc_weeks" in r for r in verdict.reasons)


def test_verdict_passes_week_floor_at_4_distinct_weeks() -> None:
    result, econ, rob = _n_clean_comparables(100, clusters=30, weeks=4)
    verdict = m.evaluate_verdict(funnel=result, economics=econ, robustness=rob)
    assert verdict.verdict != m.VERDICT_INSUFFICIENT_DATA


def test_verdict_interim_checkpoint_flag_between_50_and_99() -> None:
    result, econ, rob = _n_clean_comparables(50, clusters=30, weeks=4)
    verdict = m.evaluate_verdict(funnel=result, economics=econ, robustness=rob)
    assert verdict.verdict == m.VERDICT_INSUFFICIENT_DATA
    assert verdict.is_interim_checkpoint is True


def test_verdict_not_interim_below_50() -> None:
    result, econ, rob = _n_clean_comparables(49, clusters=30, weeks=4)
    verdict = m.evaluate_verdict(funnel=result, economics=econ, robustness=rob)
    assert verdict.is_interim_checkpoint is False


def test_verdict_invalid_integrity_wins_over_insufficient_data() -> None:
    ep = _episode(contract_sha256=OTHER_HASH)
    tr = _trade()
    result = m.build_funnel(_dataset(episodes=(ep,), trades=(tr,)))
    econ = m.compute_economics(result.comparable)
    rob = m.compute_robustness(result.comparable)
    verdict = m.evaluate_verdict(funnel=result, economics=econ, robustness=rob)
    assert verdict.verdict == m.VERDICT_INVALID_INTEGRITY


def test_verdict_invalid_integrity_when_a_row_violation_exists_even_with_floor_met() -> None:
    """A single unexplained row-level anomaly blocks formal PASS even when
    every sample-floor number is otherwise comfortably cleared (colleague
    correction #3) -- descriptive economics is still computed on the clean
    subset, but the verdict itself must not read as an authorization."""
    # Inject one more episode/trade pair with a violation alongside an
    # otherwise-clean, floor-clearing set.
    bad_ep, bad_tr = _clean_pair(episode_id="bad", side="short")
    episodes = []
    trades = []
    for i in range(120):
        cluster = f"c{i % 30}"
        week_offset = timedelta(days=7 * (i % 4))
        armed = CS + timedelta(minutes=5) + week_offset
        eid = f"e{i}"
        episodes.append(
            _episode(
                episode_id=eid, native_market_id=f"SYM{i}", cluster_key=cluster, armed_at=armed
            )
        )
        trades.append(
            _trade(
                trade_id=i,
                episode_id=eid,
                entry_at=armed + timedelta(seconds=2),
                exit_at=armed + timedelta(hours=1),
                entry_idempotency_key=f"{eid}:entry:base",
            )
        )
    episodes.append(bad_ep)
    trades.append(bad_tr)
    dataset = _dataset(
        episodes=tuple(episodes), trades=tuple(trades), cohort_end=CS + timedelta(days=60)
    )
    full_result = m.build_funnel(dataset)
    full_econ = m.compute_economics(full_result.comparable)
    full_rob = m.compute_robustness(full_result.comparable)
    verdict = m.evaluate_verdict(funnel=full_result, economics=full_econ, robustness=full_rob)
    assert verdict.verdict == m.VERDICT_INVALID_INTEGRITY
    # descriptive economics is still computed on the clean subset
    assert full_econ.closed_trades == 120


# --- report never writes to the DB (module-level static check) ---


def test_pure_module_never_imports_a_db_driver() -> None:
    import schurfer_analytics.early_momentum_net_evidence as pure_module

    source = pure_module.__file__
    assert source is not None
    content = Path(source).read_text(encoding="utf-8")
    assert "psycopg" not in content
    assert "sqlalchemy" not in content.lower()
