from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from schurfer_analytics import momentum_flow_paper_lev3_worker
from schurfer_analytics.momentum_flow_paper_contract import (
    LEVERAGED_PAPER_CONTRACT,
    LEVERAGED_PAPER_CONTRACT_SHA256,
)

if TYPE_CHECKING:
    import pytest


def test_main_calls_run_paper_worker_with_the_leveraged_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: this module exists purely to select LEVERAGED_PAPER_CONTRACT
    instead of the Bybit default run_paper_worker otherwise falls back to (see
    momentum_flow_paper_worker.run_paper_worker's own doc comment) -- if this
    wiring is ever silently lost, this worker would start running the
    unlevered v1 contract's identity (and double-claiming the SAME live Bybit
    WATCH cohort's decisions the leverage=1 worker already claims) under a
    lev3-labeled process, silently losing the whole sizing experiment."""
    calls: list[dict[str, Any]] = []

    async def fake_run_paper_worker(config: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(momentum_flow_paper_lev3_worker, "run_paper_worker", fake_run_paper_worker)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setattr(sys, "argv", ["momentum-flow-paper-lev3", "--once"])

    momentum_flow_paper_lev3_worker.main()

    assert len(calls) == 1
    assert calls[0]["contract"] is LEVERAGED_PAPER_CONTRACT
    assert calls[0]["contract_sha256"] == LEVERAGED_PAPER_CONTRACT_SHA256
    assert calls[0]["once"] is True
