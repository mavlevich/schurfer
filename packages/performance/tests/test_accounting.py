import pytest
from schurfer_performance import CostParameters, calculate_performance


def test_short_accounting_matches_replay_cost_contract() -> None:
    result = calculate_performance(
        position_usd=100,
        entry_price=100,
        exit_price=90,
        side="short",
        duration_minutes=180,
        entry_slippage_bps=3,
        exit_slippage_bps=4,
        costs=CostParameters(taker_fee_bps_per_side=10, funding_cost_bps_per_8h=5),
    )

    assert result.status == "complete"
    assert result.gross_return_pct == pytest.approx(10)
    assert result.fee_cost_bps == pytest.approx(20)
    assert result.funding_cost_bps == pytest.approx(1.875)
    assert result.slippage_cost_bps == pytest.approx(7)
    assert result.gross_pnl_usd == pytest.approx(10)
    assert result.fees_usd == pytest.approx(0.2)
    assert result.funding_usd == pytest.approx(0.01875)
    assert result.slippage_usd == pytest.approx(0.07)
    assert result.net_pnl_usd == pytest.approx(9.71125)
    assert result.net_return_pct == pytest.approx(9.71125)


def test_long_accounting_uses_long_price_direction() -> None:
    result = calculate_performance(
        position_usd=50,
        entry_price=20,
        exit_price=22,
        side="long",
        duration_minutes=0,
        entry_slippage_bps=0,
        exit_slippage_bps=0,
        costs=CostParameters(taker_fee_bps_per_side=0, funding_cost_bps_per_8h=0),
    )

    assert result.gross_return_pct == pytest.approx(10)
    assert result.net_pnl_usd == pytest.approx(5)


def test_missing_slippage_preserves_gross_but_withholds_net() -> None:
    result = calculate_performance(
        position_usd=100,
        entry_price=100,
        exit_price=90,
        side="short",
        duration_minutes=60,
        entry_slippage_bps=None,
        exit_slippage_bps=4,
    )

    assert result.status == "incomplete"
    assert result.gross_pnl_usd == pytest.approx(10)
    assert result.net_pnl_usd is None
    assert result.net_return_pct is None
    assert result.slippage_usd is None
    assert result.error == "missing entry_slippage_bps"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"position_usd": 0}, "position_usd"),
        ({"entry_price": float("nan")}, "entry_price"),
        ({"exit_price": -1}, "exit_price"),
        ({"duration_minutes": -1}, "duration_minutes"),
        ({"side": "flat"}, "side"),
        ({"entry_slippage_bps": -1}, "entry_slippage_bps"),
    ],
)
def test_invalid_accounting_inputs_fail_closed(
    kwargs: dict[str, float | str],
    message: str,
) -> None:
    values: dict[str, float | str | None] = {
        "position_usd": 100,
        "entry_price": 100,
        "exit_price": 90,
        "side": "short",
        "duration_minutes": 60,
        "entry_slippage_bps": 1,
        "exit_slippage_bps": 1,
    }
    values.update(kwargs)

    with pytest.raises(ValueError, match=message):
        calculate_performance(**values)  # type: ignore[arg-type]
