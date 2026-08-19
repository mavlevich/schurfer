"""Binance's own WATCH worker (ROADMAP phase 3: "Binance WATCH shadow,
frozen v1 logic, own version hash").

A deliberately thin wrapper: momentum_flow_watch_worker.run_watch_worker
was already contract-parameterized for exactly this purpose (see its own
doc comment), so this module owns nothing but its own CLI entrypoint and
which contract it passes in. See momentum_flow_watch_contract.
BINANCE_WATCH_CONTRACT and docs/research/binance-momentum-watch-v1.md for
why that contract's own distinct watch_version keeps this process fully
isolated from the live Bybit worker.
"""

from __future__ import annotations

import argparse
import asyncio

from .momentum_flow_watch_contract import (
    BINANCE_WATCH_CONTRACT,
    BINANCE_WATCH_CONTRACT_SHA256,
)
from .momentum_flow_watch_worker import WatchWorkerConfig, run_watch_worker


def main() -> None:
    parser = argparse.ArgumentParser(description="Run prospective momentum-flow WATCH (Binance)")
    parser.add_argument("--once", action="store_true", help="Process due buckets once")
    args = parser.parse_args()
    asyncio.run(
        run_watch_worker(
            WatchWorkerConfig.from_env(),
            once=args.once,
            contract=BINANCE_WATCH_CONTRACT,
            contract_sha256=BINANCE_WATCH_CONTRACT_SHA256,
        )
    )
