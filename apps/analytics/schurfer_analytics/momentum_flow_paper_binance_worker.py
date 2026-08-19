"""Binance's own paper worker (mirrors momentum_flow_watch_binance_worker.py's
own precedent: ROADMAP's Foundation-then-Resolution venue-expansion
pattern applied to the paper probe, not just capture/WATCH).

A deliberately thin wrapper: momentum_flow_paper_worker.run_paper_worker
was already contract-parameterized for exactly this purpose (see its own
doc comment), so this module owns nothing but its own CLI entrypoint and
which contract it passes in. See momentum_flow_paper_contract.
BINANCE_PAPER_CONTRACT for why that contract's own distinct paper_version
keeps this process fully isolated from the live Bybit worker (own
Postgres advisory lock, own _runs row, own Redis health key).
"""

from __future__ import annotations

import argparse
import asyncio

from .momentum_flow_paper_contract import (
    BINANCE_PAPER_CONTRACT,
    BINANCE_PAPER_CONTRACT_SHA256,
)
from .momentum_flow_paper_worker import PaperWorkerConfig, run_paper_worker


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run prospective momentum-flow paper probe (Binance)"
    )
    parser.add_argument("--once", action="store_true", help="Process one paper tick")
    args = parser.parse_args()
    asyncio.run(
        run_paper_worker(
            PaperWorkerConfig.from_env(),
            once=args.once,
            contract=BINANCE_PAPER_CONTRACT,
            contract_sha256=BINANCE_PAPER_CONTRACT_SHA256,
        )
    )
