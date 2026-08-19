from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from schurfer_analytics.momentum_flow_paper_contract import FROZEN_PAPER_CONTRACT
from schurfer_analytics.momentum_flow_paper_market import ExecutableQuote
from schurfer_analytics.momentum_flow_paper_repository import (
    PaperProbe,
    evaluate_probe_quote,
    paper_id_for,
)

T0 = datetime(2026, 8, 14, 12, tzinfo=UTC)


def _probe(*, position_status: str = "open") -> PaperProbe:
    return PaperProbe(
        paper_id=UUID("00000000-0000-0000-0000-000000000001"),
        symbol="ERAUSDT",
        entry_at=T0,
        entry_vwap=100.0,
        position_status=position_status,
        max_favorable_return_pct=1.0,
        max_adverse_return_pct=-2.0,
    )


def _quote(*, price: float, minutes: int) -> ExecutableQuote:
    observed_at = T0 + timedelta(minutes=minutes)
    return ExecutableQuote(
        symbol="ERAUSDT",
        unified_symbol="ERA/USDT:USDT",
        market_id="ERAUSDT",
        side="bid",
        requested_at=observed_at - timedelta(milliseconds=50),
        observed_at=observed_at,
        exchange_event_at=observed_at,
        latency_ms=50,
        best_bid=price,
        best_ask=price + 0.1,
        mid=price + 0.05,
        spread_bps=10,
        vwap=price,
        impact_bps=5,
        filled_notional_usd=50,
        contract_size=1,
    )


def test_paper_id_is_deterministic_and_version_scoped() -> None:
    watch_id = UUID("00000000-0000-0000-0000-000000000002")

    first = paper_id_for(FROZEN_PAPER_CONTRACT, watch_id)
    second = paper_id_for(FROZEN_PAPER_CONTRACT, watch_id)

    assert first == second
    assert first != watch_id


def test_probe_quote_triggers_stop_and_preserves_excursions() -> None:
    result = evaluate_probe_quote(
        _probe(),
        _quote(price=94.0, minutes=30),
        contract=FROZEN_PAPER_CONTRACT,
    )

    assert result.exit_reason == "stop_loss"
    assert result.adverse_return_pct == pytest.approx(-6.0)
    assert result.favorable_return_pct == 1.0
    assert result.performance.net_return_pct is not None
    assert result.performance.net_return_pct < result.performance.gross_return_pct


def test_probe_quote_closes_at_max_hold_without_hindsight() -> None:
    result = evaluate_probe_quote(
        _probe(),
        _quote(price=103.0, minutes=240),
        contract=FROZEN_PAPER_CONTRACT,
    )

    assert result.exit_reason == "max_hold"
    assert result.duration_minutes == 240
    assert result.favorable_return_pct == pytest.approx(3.0)


def test_closed_probe_does_not_exit_again() -> None:
    result = evaluate_probe_quote(
        _probe(position_status="closed"),
        _quote(price=90.0, minutes=60),
        contract=FROZEN_PAPER_CONTRACT,
    )

    assert result.exit_reason is None
