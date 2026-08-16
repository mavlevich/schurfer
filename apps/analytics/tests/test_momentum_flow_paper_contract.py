from dataclasses import replace

import pytest
from schurfer_analytics.momentum_flow_paper_contract import (
    FROZEN_PAPER_CONTRACT,
    HOLD12H_PAPER_CONTRACT,
    HOLD12H_PAPER_CONTRACT_SHA256,
    LEVERAGED_PAPER_CONTRACT,
    LEVERAGED_PAPER_CONTRACT_SHA256,
    MARGIN_USD,
    PAPER_CONTRACT_SHA256,
)


def test_frozen_paper_contract_hash_is_checked() -> None:
    assert FROZEN_PAPER_CONTRACT.sha256_hex() == PAPER_CONTRACT_SHA256
    assert FROZEN_PAPER_CONTRACT.side == "long"
    assert FROZEN_PAPER_CONTRACT.position_notional_usd == 50
    assert FROZEN_PAPER_CONTRACT.leverage == 1
    assert FROZEN_PAPER_CONTRACT.outcome_horizons_minutes == (5, 15, 30, 60, 120, 240)


def test_contract_requires_final_horizon_to_match_max_hold() -> None:
    with pytest.raises(ValueError, match="final outcome horizon"):
        replace(FROZEN_PAPER_CONTRACT, outcome_horizons_minutes=(5, 15, 30))


def test_contract_rejects_notional_leverage_mismatch() -> None:
    # position_notional_usd must equal MARGIN_USD x leverage -- bumping leverage
    # alone (without also resizing the notional) would silently commit MORE real
    # capital than MARGIN_USD, which is exactly the drift this guards against.
    with pytest.raises(ValueError, match="MARGIN_USD"):
        replace(FROZEN_PAPER_CONTRACT, leverage=2)


def test_contract_rejects_non_positive_leverage() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        replace(FROZEN_PAPER_CONTRACT, leverage=-1)


def test_leveraged_paper_contract_hash_is_checked() -> None:
    # LEVERAGED_PAPER_CONTRACT is a sizing variant, not a venue expansion: same
    # live Bybit WATCH signal as FROZEN_PAPER_CONTRACT (watch_version/
    # source_exchange unchanged), only the simulated position size differs.
    assert LEVERAGED_PAPER_CONTRACT.sha256_hex() == LEVERAGED_PAPER_CONTRACT_SHA256
    assert LEVERAGED_PAPER_CONTRACT.watch_version == FROZEN_PAPER_CONTRACT.watch_version
    assert LEVERAGED_PAPER_CONTRACT.source_exchange == FROZEN_PAPER_CONTRACT.source_exchange
    assert LEVERAGED_PAPER_CONTRACT.paper_version != FROZEN_PAPER_CONTRACT.paper_version
    assert LEVERAGED_PAPER_CONTRACT.leverage == 3
    assert LEVERAGED_PAPER_CONTRACT.position_notional_usd == MARGIN_USD * 3


def test_hold12h_paper_contract_hash_is_checked() -> None:
    # HOLD12H_PAPER_CONTRACT is a hold-duration variant, not a venue or sizing
    # expansion: same live Bybit WATCH signal and same real capital/leverage as
    # FROZEN_PAPER_CONTRACT, only max_hold_minutes (and the outcome horizons
    # that must extend to match it) differ.
    assert HOLD12H_PAPER_CONTRACT.sha256_hex() == HOLD12H_PAPER_CONTRACT_SHA256
    assert HOLD12H_PAPER_CONTRACT.watch_version == FROZEN_PAPER_CONTRACT.watch_version
    assert HOLD12H_PAPER_CONTRACT.source_exchange == FROZEN_PAPER_CONTRACT.source_exchange
    assert HOLD12H_PAPER_CONTRACT.paper_version != FROZEN_PAPER_CONTRACT.paper_version
    assert (
        HOLD12H_PAPER_CONTRACT.position_notional_usd == FROZEN_PAPER_CONTRACT.position_notional_usd
    )
    assert HOLD12H_PAPER_CONTRACT.leverage == FROZEN_PAPER_CONTRACT.leverage
    assert HOLD12H_PAPER_CONTRACT.stop_loss_pct == FROZEN_PAPER_CONTRACT.stop_loss_pct
    assert HOLD12H_PAPER_CONTRACT.max_hold_minutes == 720
    assert HOLD12H_PAPER_CONTRACT.outcome_horizons_minutes == (5, 15, 30, 60, 120, 240, 480, 720)
    # Extends, not replaces: every horizon the live baseline already reports
    # stays present, so results remain comparable at each of those points.
    assert set(FROZEN_PAPER_CONTRACT.outcome_horizons_minutes).issubset(
        set(HOLD12H_PAPER_CONTRACT.outcome_horizons_minutes)
    )
