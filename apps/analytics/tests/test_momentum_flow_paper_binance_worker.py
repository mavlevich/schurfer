from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from schurfer_analytics import momentum_flow_paper_binance_worker
from schurfer_analytics.momentum_flow_paper_contract import (
    BINANCE_PAPER_CONTRACT,
    BINANCE_PAPER_CONTRACT_SHA256,
)

if TYPE_CHECKING:
    import pytest


def test_main_calls_run_paper_worker_with_the_binance_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: this module exists purely to select BINANCE_PAPER_CONTRACT
    instead of the Bybit default run_paper_worker otherwise falls back to
    (see momentum_flow_paper_worker.run_paper_worker's own doc comment) --
    if this wiring is ever silently lost, this worker would start running
    the Bybit contract's identity (and claiming the Bybit WATCH cohort's
    own decisions) under a Binance-labeled process."""
    calls: list[dict[str, Any]] = []

    async def fake_run_paper_worker(config: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(
        momentum_flow_paper_binance_worker, "run_paper_worker", fake_run_paper_worker
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setattr(sys, "argv", ["momentum-flow-paper-binance", "--once"])

    momentum_flow_paper_binance_worker.main()

    assert len(calls) == 1
    assert calls[0]["contract"] is BINANCE_PAPER_CONTRACT
    assert calls[0]["contract_sha256"] == BINANCE_PAPER_CONTRACT_SHA256
    assert calls[0]["once"] is True
