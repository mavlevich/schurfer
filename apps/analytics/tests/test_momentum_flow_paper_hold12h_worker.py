from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from schurfer_analytics import momentum_flow_paper_hold12h_worker
from schurfer_analytics.momentum_flow_paper_contract import (
    HOLD12H_PAPER_CONTRACT,
    HOLD12H_PAPER_CONTRACT_SHA256,
)

if TYPE_CHECKING:
    import pytest


def test_main_calls_run_paper_worker_with_the_hold12h_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: this module exists purely to select HOLD12H_PAPER_CONTRACT
    instead of the Bybit default run_paper_worker otherwise falls back to (see
    momentum_flow_paper_worker.run_paper_worker's own doc comment) -- if this
    wiring is ever silently lost, this worker would start running the
    240-minute v1 contract's identity (and double-claiming the SAME live
    Bybit WATCH cohort's decisions the 240-minute worker already claims)
    under a hold12h-labeled process, silently losing the whole hold-duration
    experiment HYP-015 exists to test."""
    calls: list[dict[str, Any]] = []

    async def fake_run_paper_worker(config: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(
        momentum_flow_paper_hold12h_worker, "run_paper_worker", fake_run_paper_worker
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setattr(sys, "argv", ["momentum-flow-paper-hold12h", "--once"])

    momentum_flow_paper_hold12h_worker.main()

    assert len(calls) == 1
    assert calls[0]["contract"] is HOLD12H_PAPER_CONTRACT
    assert calls[0]["contract_sha256"] == HOLD12H_PAPER_CONTRACT_SHA256
    assert calls[0]["once"] is True
