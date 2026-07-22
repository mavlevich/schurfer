"""Process lifecycle and CLI for the decision outcome resolver."""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import structlog

from .ohlcv import TIMEFRAME
from .outcome_repository import OutcomeRepository, OutcomeStore
from .outcomes import HORIZONS_MINUTES, OutcomeConfig, resolve_once
from .scanner import EXCHANGE_FACTORIES

log = structlog.get_logger()


async def run_outcome_resolver(
    cfg: OutcomeConfig,
    *,
    once: bool = False,
    store: OutcomeStore | None = None,
) -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
    exchanges: dict[str, Any] = {}
    owned_repository: OutcomeRepository | None = None
    try:
        for name in cfg.exchanges:
            if name in EXCHANGE_FACTORIES:
                exchanges[name] = EXCHANGE_FACTORIES[name]()
        if not exchanges:
            raise ValueError("no supported OUTCOME/PUMP_EXCHANGES configured")
        if store is None:
            owned_repository = OutcomeRepository.from_url(cfg.db_url)
            active_store: OutcomeStore = owned_repository
        else:
            active_store = store
        log.info(
            "outcomes.starting",
            horizons=HORIZONS_MINUTES,
            timeframe=TIMEFRAME,
            exchanges=list(exchanges),
        )
        while True:
            try:
                resolved_count = await resolve_once(cfg, exchanges, active_store)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("outcomes.tick_failed", error=str(exc))
                if once:
                    raise
            else:
                if once:
                    return
                if resolved_count >= cfg.batch_size:
                    log.info(
                        "outcomes.backlog_draining",
                        resolved_count=resolved_count,
                        batch_size=cfg.batch_size,
                    )
                    continue
            await asyncio.sleep(cfg.poll_interval_seconds)
    finally:
        await asyncio.gather(
            *[exchange.close() for exchange in exchanges.values()],
            return_exceptions=True,
        )
        if owned_repository is not None:
            await owned_repository.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve forward outcomes for trade decisions")
    parser.add_argument("--once", action="store_true", help="Resolve one due batch and exit")
    args = parser.parse_args()
    asyncio.run(run_outcome_resolver(OutcomeConfig.from_env(), once=args.once))
