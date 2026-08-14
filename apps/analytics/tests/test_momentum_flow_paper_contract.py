from dataclasses import replace

import pytest
from schurfer_analytics.momentum_flow_paper_contract import (
    FROZEN_PAPER_CONTRACT,
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


def test_contract_rejects_leverage() -> None:
    with pytest.raises(ValueError, match="unlevered"):
        replace(FROZEN_PAPER_CONTRACT, leverage=2)
